"""首次登录并列出账号已加入的群/频道，用来填 config.toml 的 collector.watch。

用法：
    python -m tools.login

会依次要：Telegram 发到 App 的登录码、二级密码（如果你开了）。
登录态保存在 config.toml 里 telegram.session 指定的文件里，之后不用再登录。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as config_module  # noqa: E402
from app.listener import build_client  # noqa: E402
from app.proxy import proxy_from_account  # noqa: E402
from app.store import Store  # noqa: E402
from app.tg import missing_credentials, resolve_credentials  # noqa: E402

async def main_async() -> None:
    cfg = config_module.load()
    store = Store(cfg.db_path())
    store.init()

    # 凭据来源和管理页一致：数据库优先，config.toml 兜底
    cred = resolve_credentials(cfg, store)
    need = missing_credentials(cred) + ([] if cred["phone"] else ["phone"])
    if need:
        raise SystemExit(
            "缺少 Telegram 凭据：" + "、".join(need)
            + "。在管理页「账号 → 应用凭据」里填，或写进 config.toml 的 [telegram] 段。"
        )

    client = build_client(cfg, store)
    proxy = proxy_from_account(store, store.active_account())
    print(f"连接方式：{'代理 ' + proxy.masked() if proxy else '直连'}")
    await client.start(phone=cred["phone"])
    me = await client.get_me()
    print(f"\n已登录：{getattr(me, 'first_name', '')} "
          f"@{getattr(me, 'username', None) or '-'} (id={getattr(me, 'id', '')})\n")

    print(f"{'类型':<8}{'id':<18}{'username':<24}标题")
    print("-" * 90)
    async for dialog in client.iter_dialogs():
        if not dialog.is_group and not dialog.is_channel:
            continue
        ent = dialog.entity
        kind = "频道" if getattr(ent, "broadcast", False) else ("超级群" if dialog.is_channel else "群")
        print(f"{kind:<8}{str(dialog.id):<18}{('@' + ent.username) if getattr(ent, 'username', None) else '-':<24}{dialog.name}")

    print("\n把要监听的群写进 config.toml 的 collector.watch（@username 或数字 id 都行）。")
    await client.disconnect()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
