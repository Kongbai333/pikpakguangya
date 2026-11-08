"""Realistic concurrency regressions for the two forwarding hops. No network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from app import bot_api, config, forwarder, telegram_bot as tgb
from app.store import Store

CONF = {"token": "12345:test-token", "user_id": 99}
DEST = -100555


@pytest.fixture()
def store(tmp_path):
    result = Store(tmp_path / "delivery.db")
    result.init()
    forwarder.save_relay_target(result, chat_id=DEST, title="target", link_source=False)
    return result


def bot(store):
    return tgb.TelegramBotRunner(store, config.Config())


def video(mid=5, group="", sender=42, source=7):
    message = {"message_id": mid, "from": {"id": sender},
               "chat": {"id": sender, "type": "private"},
               "video": {"file_id": "video"},
               "forward_origin": {"type": "channel",
                    "chat": {"id": -100777, "title": "source"},
                    "message_id": source}}
    if group:
        message["media_group_id"] = group
    return message


def copied(method, payload):
    if method == "copyMessages":
        return [{"message_id": 1000 + i} for i in payload["message_ids"]]
    return {"message_id": 1000 + payload.get("message_id", 0)}


def test_same_album_update_three_times_only_copies_one_message(store, monkeypatch):
    monkeypatch.setattr(tgb, "ALBUM_WAIT_SECONDS", 0)
    runner = bot(store)
    calls = []

    async def api(client, conf, method, payload):
        calls.append((method, payload))
        await asyncio.sleep(0)
        return copied(method, payload)

    runner._api_post = api

    async def main():
        for _ in range(3):
            await runner._relay_video(video(group="album"), CONF, None)
        await asyncio.gather(*list(runner._album_tasks.values()))

    asyncio.run(main())
    assert len(calls) == 1
    method, payload = calls[0]
    assert payload.get("message_ids", [payload.get("message_id")]) == [5]


def test_two_runners_same_database_only_one_copy(store):
    first, second = bot(store), bot(Store(store.path))
    calls = []

    async def main():
        entered, release = asyncio.Event(), asyncio.Event()

        async def api(client, conf, method, payload):
            calls.append((method, payload))
            entered.set()
            await release.wait()
            return copied(method, payload)

        first._api_post = second._api_post = api
        task = asyncio.create_task(first._relay_video(video(), CONF, None))
        await entered.wait()
        other = asyncio.create_task(second._relay_video(video(mid=6), CONF, None))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, other)

    asyncio.run(main())
    assert len(calls) == 1


def test_duplicate_arriving_while_album_sending_is_not_rescheduled(store, monkeypatch):
    monkeypatch.setattr(tgb, "ALBUM_WAIT_SECONDS", 0)
    monkeypatch.setattr(tgb, "COPY_MIN_INTERVAL", 0)
    runner = bot(store)
    calls = []

    async def main():
        entered, release = asyncio.Event(), asyncio.Event()

        async def api(client, conf, method, payload):
            calls.append((method, payload))
            entered.set()
            await release.wait()
            return copied(method, payload)

        runner._api_post = api
        await runner._relay_video(video(group="album"), CONF, None)
        await entered.wait()
        await runner._relay_video(video(group="album"), CONF, None)
        release.set()
        await asyncio.gather(*list(runner._album_tasks.values()))
        await asyncio.sleep(0)

    asyncio.run(main())
    assert len(calls) == 1


def test_stop_flush_keeps_actual_source_chat_not_admin_id(store, monkeypatch):
    monkeypatch.setattr(tgb, "ALBUM_WAIT_SECONDS", 60)
    runner = bot(store)
    calls = []

    async def api(client, conf, method, payload):
        calls.append(payload)
        return copied(method, payload)

    async def legacy_copy(token, to_chat, from_chat, ids, **kwargs):
        calls.append({"from_chat_id": from_chat})
        return [{"message_id": 1000}]

    runner._api_post = api
    monkeypatch.setattr(bot_api, "copy_messages", legacy_copy)

    async def main():
        await runner._relay_video(video(group="album", sender=42), CONF, None)
        await runner._flush_pending_albums(CONF)

    asyncio.run(main())
    assert len(calls) == 1
    assert calls[0]["from_chat_id"] == 42


def test_real_bot_api_path_raises_typed_429(store):
    runner = bot(store)

    def respond(request):
        return httpx.Response(429, json={"ok": False, "error_code": 429,
            "description": "Too Many Requests: retry after 5",
            "parameters": {"retry_after": 5}})

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(bot_api.BotApiRateLimit) as err:
                await runner._api_post(client, CONF, "copyMessage", {})
            assert err.value.retry_after == 5

    asyncio.run(main())


def test_database_claim_is_atomic_across_stores(store):
    other = Store(store.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(lambda s: s.claim_forward_delivery("destination", ["src:7"]),
                               [store, other]))
    assert len([x for x in owners if x]) == 1
    assert Store(store.path).claim_forward_delivery("destination", ["src:7"]) == ""
    assert store.claim_forward_delivery("different-target", ["src:7"])


def test_database_success_and_uncertain_do_not_auto_resend(store):
    scope = "destination"
    owner = store.claim_forward_delivery(scope, ["src:7", "dm:42:5"])
    store.finish_forward_delivery(scope, owner, "sent", result_ids=[1000])
    assert Store(store.path).claim_forward_delivery(scope, ["src:7"]) == ""
    owner = store.claim_forward_delivery(scope, ["src:8"])
    store.finish_forward_delivery(scope, owner, "uncertain", error="read timed out")
    assert store.claim_forward_delivery(scope, ["src:8"]) == ""


def test_database_known_failure_can_retry(store):
    owner = store.claim_forward_delivery("destination", ["src:7"])
    store.finish_forward_delivery("destination", owner, "failed", error="rejected")
    assert store.claim_forward_delivery("destination", ["src:7"])


def test_bot_timeout_does_not_resend_after_restart(store, monkeypatch):
    monkeypatch.setattr(tgb, "COPY_MIN_INTERVAL", 0)
    calls = []

    async def api(client, conf, method, payload):
        if method.startswith("copy"):
            calls.append(payload)
            raise httpx.ReadTimeout("response lost after send")
        return {"message_id": 1}

    first = bot(store)
    first._api_post = api
    asyncio.run(first._relay_video(video(), CONF, None))
    again = bot(Store(store.path))
    again._api_post = api
    asyncio.run(again._relay_video(video(mid=6), CONF, None))
    assert len(calls) == 1
    with store._read() as c:
        row = c.execute("SELECT status FROM forward_deliveries WHERE source_key='src:-100777:7'").fetchone()
    assert row["status"] == "uncertain"


def test_bot_partial_album_result_is_not_blindly_repeated(store, monkeypatch):
    monkeypatch.setattr(tgb, "ALBUM_WAIT_SECONDS", 0)
    monkeypatch.setattr(tgb, "COPY_MIN_INTERVAL", 0)
    runner = bot(store)
    calls = []

    async def api(client, conf, method, payload):
        if method == "copyMessages":
            calls.append(payload)
            return [{"message_id": 1234}]  # only one of two confirmed
        return {"message_id": 99}

    runner._api_post = api

    async def main():
        await runner._relay_video(video(5, "one", source=7), CONF, None)
        await runner._relay_video(video(6, "one", source=8), CONF, None)
        await asyncio.gather(*list(runner._album_tasks.values()))
        await runner._relay_video(video(15, "again", source=7), CONF, None)
        await runner._relay_video(video(16, "again", source=8), CONF, None)
        await asyncio.gather(*list(runner._album_tasks.values()))

    asyncio.run(main())
    assert len(calls) == 1
    assert "结果待核对" in forwarder.load_relay_stats(store)["last_error"]


def test_same_source_can_be_sent_to_a_different_destination(store, monkeypatch):
    monkeypatch.setattr(tgb, "COPY_MIN_INTERVAL", 0)
    runner = bot(store)
    calls = []

    async def api(client, conf, method, payload):
        calls.append(payload)
        return copied(method, payload)

    runner._api_post = api
    asyncio.run(runner._relay_video(video(), CONF, None))
    forwarder.save_relay_target(store, chat_id=-100556, link_source=False)
    asyncio.run(runner._relay_video(video(mid=6), CONF, None))
    assert [p["chat_id"] for p in calls] == [DEST, -100556]


def test_collector_accounts_share_destination_dedupe(store):
    from app.collector import Collector
    aid1 = store.add_account(session="first")["id"]
    aid2 = store.add_account(session="second")["id"]
    collectors = []
    for aid in (aid1, aid2):
        store.add_watched(-100777, enabled=True, account_id=aid)
        store.set_forward_watched(-100777, True, account_id=aid)
        forwarder.save_target(store, aid, chat_id=DEST, raw_id=555, access_hash=1,
                              title="target", mode="bot", bot_user_id=12345, bot_username="bot")
        collectors.append(Collector(store.get_account(aid), None, store, config.Config(), None))
    calls = []
    chat = SimpleNamespace(id=-100777, title="source")
    message = SimpleNamespace(id=7, grouped_id=None, video=object())

    class Client:
        async def forward_messages(self, entity, messages, **kwargs):
            calls.append(list(messages))
            await asyncio.sleep(0)
            return [SimpleNamespace(id=1000 + mid) for mid in messages]

    async def main():
        await asyncio.gather(*(c._forward_media(Client(), chat, message, set()) for c in collectors))

    asyncio.run(main())
    assert calls == [[7]]


def test_late_album_member_is_sent_without_resending_earlier_member(store):
    from app.collector import Collector
    aid = store.add_account(session="first")["id"]
    store.add_watched(-100777, enabled=True, account_id=aid)
    store.set_forward_watched(-100777, True, account_id=aid)
    forwarder.save_target(store, aid, chat_id=DEST, raw_id=555, access_hash=1,
                          title="target", mode="bot", bot_user_id=12345, bot_username="bot")
    collector = Collector(store.get_account(aid), None, store, config.Config(), None)
    chat = SimpleNamespace(id=-100777, title="source")
    messages = [SimpleNamespace(id=mid, grouped_id=444, video=object()) for mid in (7, 8)]
    calls = []

    class Client:
        visible = 1
        async def get_messages(self, *args, **kwargs):
            return messages[:self.visible]
        async def forward_messages(self, entity, messages, **kwargs):
            calls.append(list(messages))
            return [SimpleNamespace(id=1000 + mid) for mid in messages]

    client = Client()
    async def main():
        albums = set()
        await collector._forward_media(client, chat, messages[0], albums)
        client.visible = 2
        await collector._forward_media(client, chat, messages[1], albums)
    asyncio.run(main())
    assert calls == [[7], [8]]


def test_managed_relay_keeps_original_source_when_bot_origin_is_hidden(store):
    # Private group forwards don't reliably identify the source in Bot API updates.
    store.record_forward_relay_sources("12345", 42, -100777, [7], [5], DEST)
    runner = bot(store)
    calls = []
    async def api(client, conf, method, payload):
        calls.append(payload)
        return copied(method, payload)
    runner._api_post = api
    message = video()
    message.pop("forward_origin")
    asyncio.run(runner._relay_video(message, CONF, None))
    with store._read() as c:
        row = c.execute("SELECT status FROM forward_deliveries WHERE source_key='src:-100777:7'").fetchone()
    assert row["status"] == "sent"
    assert len(calls) == 1


def test_real_http_429_is_waited_then_successfully_retried(store):
    runner = bot(store)
    requests = []
    def response(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                "description": "retry after 1", "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1000}})
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await runner._relay_video(video(), CONF, client)
    asyncio.run(main())
    assert len(requests) == 2
    assert forwarder.load_relay_stats(store)["copied"] == 1


def test_unbounded_delivery_history_does_not_expire_with_json_cache(store, monkeypatch):
    monkeypatch.setattr(forwarder, "DONE_LIMIT", 2)
    owner = store.claim_forward_delivery("destination", ["src:7"])
    store.finish_forward_delivery("destination", owner, "sent", result_ids=[1000])
    forwarder.mark_forwarded(store, 1, -100777, [7, 8, 9, 10])
    assert not forwarder.was_forwarded(store, 1, -100777, 7)
    assert Store(store.path).claim_forward_delivery("destination", ["src:7"]) == ""
