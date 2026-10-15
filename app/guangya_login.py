"""光鸭网页端手机号验证码登录。

接口流程参考 AlistGo/alist 的 guangyapan 驱动。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import httpx

from app.proxy import ProxyConfig
from app.store import iso, utcnow

STATE_KEY = "integrations.guangya"
ACCOUNT_BASE_URL = "https://account.guangyapan.com"
API_BASE_URL = "https://api.guangyapan.com"
CLIENT_ID = "aMe-8VSlkrbQXpUR"

_API_CLIENTS: dict[str, httpx.Client] = {}
_API_CLIENTS_LOCK = threading.Lock()


def _shared_api_client(proxy_url: str) -> httpx.Client:
    """复用光鸭 API 连接池。秒传每个文件都要调接口，不能每次重建 SSL 连接。"""
    with _API_CLIENTS_LOCK:
        client = _API_CLIENTS.get(proxy_url)
        if client is not None and not client.is_closed:
            return client
        kwargs: dict[str, Any] = {
            "base_url": API_BASE_URL,
            "timeout": 25.0,
            "follow_redirects": True,
            "trust_env": False,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        client = httpx.Client(**kwargs)
        _API_CLIENTS[proxy_url] = client
        return client


class GuangyaLoginError(RuntimeError):
    pass


class GuangyaAuthError(GuangyaLoginError):
    """登录态/权限问题（不是单个文件的问题），调用方应停止整批任务。"""


def normalize_phone(phone: str) -> str:
    value = "".join(ch for ch in (phone or "").strip() if ch.isdigit() or ch == "+")
    if value.startswith("+86") and len(value) > 3:
        return "+86 " + value[3:]
    if len(value) == 11 and value.startswith("1"):
        return "+86 " + value
    if value.startswith("+"):
        return value
    raise GuangyaLoginError("手机号格式不正确，请使用 +86 13xxxxxxxxx")


def mask_phone(phone: str) -> str:
    value = phone or ""
    if len(value) < 7:
        return value
    return value[:4] + "****" + value[-4:]


def _find_value(node: Any, keys: tuple[str, ...]) -> Any:
    """在光鸭返回的 JSON 里按字段名找值：目录 id 藏在哪一层不固定。"""
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if value not in (None, ""):
                return value
        for value in node.values():
            found = _find_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_value(value, keys)
            if found not in (None, ""):
                return found
    return None


class GuangyaAuth:
    def __init__(self, store, proxy: ProxyConfig | None = None):
        self.store = store
        self.proxy = proxy

    def _state(self) -> dict:
        return self.store.get_json(STATE_KEY, {}) or {}

    def _save(self, **changes) -> None:
        state = self._state()
        state.update(changes)
        self.store.set_json(STATE_KEY, state)

    def _device_id(self) -> str:
        state = self._state()
        device_id = str(state.get("device_id") or "")
        if not device_id:
            device_id = uuid.uuid4().hex
            self._save(device_id=device_id)
        return device_id

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "base_url": ACCOUNT_BASE_URL,
            "timeout": 25.0,
            "follow_redirects": True,
            "trust_env": False,
        }
        if self.proxy is not None and getattr(self.proxy, "enabled", False):
            kwargs["proxy"] = self.proxy.to_url()
        return httpx.Client(**kwargs)

    def _api_client(self) -> httpx.Client:
        proxy_url = ""
        if self.proxy is not None and getattr(self.proxy, "enabled", False):
            proxy_url = self.proxy.to_url()
        return _shared_api_client(proxy_url)

    def _headers(self, device_id: str, captcha_token: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Device-Model": "chrome%2F147.0.0.0",
            "X-Device-Name": "PC-Chrome",
            "X-Device-Sign": "wdi10." + device_id + "x" * 32,
            "X-Net-Work-Type": "NONE",
            "X-OS-Version": "MacIntel",
            "X-Platform-Version": "1",
            "X-Protocol-Version": "301",
            "X-Provider-Name": "NONE",
            "X-SDK-Version": "9.0.2",
            "X-Client-Id": CLIENT_ID,
            "X-Client-Version": "0.0.1",
            "X-Device-Id": device_id,
        }
        if captcha_token:
            headers["X-Captcha-Token"] = captcha_token
        return headers

    @staticmethod
    def _error(data: dict, response: httpx.Response) -> str:
        return str(
            data.get("error_description")
            or data.get("error")
            or data.get("message")
            or response.text
            or f"HTTP {response.status_code}"
        )

    def _post(
        self,
        client: httpx.Client,
        path: str,
        payload: dict,
        *,
        device_id: str,
        captcha_token: str = "",
    ) -> dict:
        response = client.post(
            path,
            json=payload,
            headers=self._headers(device_id, captcha_token),
        )
        try:
            data = response.json()
        except ValueError as e:
            raise GuangyaLoginError(f"光鸭接口返回异常：{response.text[:200]}") from e
        if response.is_error or data.get("error"):
            raise GuangyaLoginError(self._error(data, response))
        return data

    def status(self) -> dict:
        state = self._state()
        token = str(state.get("access_token") or "")
        return {
            "connected": bool(token),
            "code_pending": bool(state.get("verification_id")),
            "phone": mask_phone(str(state.get("phone_number") or "")),
            "token_hint": f"***{token[-6:]}" if token else "",
            "updated_at": state.get("updated_at") or "",
            "root_folder_id": state.get("root_folder_id") or "",
            "root_folder_path": state.get("root_folder_path") or "/",
            "storage": None,
        }

    def refresh_access_token(self) -> str:
        state = self._state()
        refresh_token = str(state.get("refresh_token") or "")
        if not refresh_token:
            raise GuangyaAuthError("光鸭登录态已失效，请重新登录")
        device_id = str(state.get("device_id") or self._device_id())
        with self._client() as client:
            response = client.post(
                "/v1/auth/token",
                json={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers=self._headers(device_id),
            )
            try:
                data = response.json()
            except ValueError as e:
                raise GuangyaAuthError(f"光鸭刷新登录态失败：{response.text[:200]}") from e
            if response.is_error or data.get("error"):
                raise GuangyaAuthError(self._error(data, response))
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise GuangyaAuthError("光鸭刷新登录态失败")
        self._save(
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or refresh_token),
            updated_at=iso(utcnow()),
        )
        return access_token

    def _api_call(self, method: str, path: str, payload: dict | None = None) -> dict:
        state = self._state()
        access_token = str(state.get("access_token") or "")
        if not access_token:
            raise GuangyaLoginError("请先登录光鸭账号")
        device_id = str(state.get("device_id") or self._device_id())
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Did": device_id,
            "Dt": "4",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        client = self._api_client()
        request_args = {"headers": headers}
        if payload is not None:
            request_args["json"] = payload
        response = self._request(client, method, path, request_args)
        if response.status_code in (401, 403):
            access_token = self.refresh_access_token()
            headers["Authorization"] = f"Bearer {access_token}"
            request_args["headers"] = headers
            response = self._request(client, method, path, request_args)
        try:
            data = response.json()
        except ValueError as e:
            raise GuangyaLoginError(f"光鸭接口返回异常：{response.text[:200]}") from e
        if response.is_error:
            message = str(data.get("msg") or data.get("message") or f"HTTP {response.status_code}")
            if response.status_code in (401, 403):
                raise GuangyaAuthError(f"光鸭登录态失效：{message}")
            raise GuangyaLoginError(message)
        return data

    @staticmethod
    def _request(client: httpx.Client, method: str, path: str,
                 request_args: dict) -> httpx.Response:
        """网络抖动重试一次：连接超时/握手失败偶发，直接判失败太亏。"""
        for attempt in range(2):
            try:
                return client.request(method, path, **request_args)
            except httpx.TransportError:
                if attempt:
                    raise
                time.sleep(0.8)
        raise GuangyaLoginError("光鸭请求失败")

    def storage(self) -> dict:
        data = self._api_call("POST", "/nd.bizassets.s/v1/get_assets")
        body = data.get("data") or {}
        total = int(body.get("totalSpaceSize") or 0)
        used = int(body.get("usedSpaceSize") or 0)
        return {
            "used": used,
            "total": total,
            "percent": round(used / total * 100, 1) if total else 0,
        }

    def list_folders(self, parent_id: str = "") -> dict:
        data = self._api_call(
            "POST",
            "/nd.bizuserres.s/v1/file/get_file_list",
            {
                "parentId": parent_id or "",
                "page": 0,
                "pageSize": 1000,
                "orderBy": 3,
                "sortType": 1,
                "fileTypes": [],
            },
        )
        items = []
        for row in (data.get("data") or {}).get("list") or []:
            if int(row.get("resType") or 0) != 2:
                continue
            items.append({
                "id": str(row.get("fileId") or ""),
                "parent_id": str(row.get("parentId") or ""),
                "name": str(row.get("fileName") or ""),
            })
        return {"folders": items}

    # 秒传：光鸭的 get_res_center_token 返回这个 code 表示资源库已有该文件，直接入库。
    FLASH_OK_CODE = 156
    # 光鸭空间不足：每个文件都会失败，调用方应停下整批
    SPACE_LIMIT_CODE = 157
    # 建目录时目录已存在，光鸭返回这个 code，同样算成功
    DIR_EXISTS_CODE = 159

    def create_folder(self, name: str, parent_id: str = "") -> str:
        """在光鸭建目录，返回目录 id（秒传要保持分享里的目录层级）。

        字段名是 dirName：传 fileName 光鸭会报「文件夹名不能为空」。
        failIfNameExist=false 时同名目录返回 code 159，同样带目录 id。
        """
        data = self._api_call(
            "POST",
            "/nd.bizuserres.s/v1/file/create_dir",
            {
                "dirName": name,
                "parentId": parent_id or "",
                "failIfNameExist": False,
            },
        )
        try:
            code = int(data.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        if code not in (0, self.DIR_EXISTS_CODE):
            raise GuangyaLoginError(str(data.get("msg") or f"光鸭创建目录失败 code={code}"))
        folder_id = _find_value(
            data,
            ("dirId", "dir_id", "fileId", "file_id", "folderId", "folder_id", "id"),
        )
        if not folder_id:
            raise GuangyaLoginError(str(data.get("msg") or "光鸭创建目录失败"))
        return str(folder_id)

    def flash_upload(self, *, name: str, size: int, gcid: str, cid: str = "",
                     parent_id: str = "") -> dict:
        """用 GCID 秒传一个文件到光鸭。

        返回 {ok, status, error}，status 为 hit（已入库）/ miss（未命中）/
        error（接口异常）。**不做普通上传兜底**：未命中即跳过，不占上传带宽。

        cid 是光鸭真正拿来匹配资源库的内容 ID（前/中/尾各 20KB 的 SHA-1），
        只给 gcid 会一直判未命中。
        """
        gcid = (gcid or "").strip().upper()
        cid = (cid or "").strip().upper()
        res: dict = {"fileSize": int(size or 0)}
        if gcid:
            res["gcid"] = gcid
        if cid:
            res["cid"] = cid
        data = self._api_call(
            "POST",
            "/nd.bizuserres.s/v1/get_res_center_token",
            {
                # capacity=2 是秒传探测模式；=1 是普通上传，缺 md5 会直接报 code=112
                "capacity": 2,
                "res": res,
                "name": name,
                "parentId": parent_id or "",
            },
        )
        try:
            code = int(data.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        if code == self.FLASH_OK_CODE:
            return {"ok": True, "status": "hit", "error": ""}
        if code == self.SPACE_LIMIT_CODE:
            return {
                "ok": False,
                "status": "space",
                "error": str(data.get("msg") or "光鸭空间不足"),
            }

        task_id = str((data.get("data") or {}).get("taskId") or "")
        if not task_id:
            return {
                "ok": False,
                "status": "error",
                "error": f"光鸭返回 code={code}：{data.get('msg') or '未知错误'}",
            }
        if not gcid:
            self.delete_upload_task(task_id)
            return {"ok": False, "status": "miss", "error": "缺少 GCID，无法秒传"}

        check_payload: dict = {
            "taskId": task_id, "gcid": gcid, "fileSize": int(size or 0),
        }
        if cid:
            check_payload["cid"] = cid
        check = self._api_call(
            "POST",
            "/nd.bizuserres.s/v1/check_can_flash_upload",
            check_payload,
        )
        try:
            check_code = int(check.get("code") or 0)
        except (TypeError, ValueError):
            check_code = 0
        if check_code != 0:
            self.delete_upload_task(task_id)
            return {
                "ok": False,
                "status": "error",
                "error": str(check.get("msg") or f"光鸭校验返回 code={check_code}"),
            }
        if bool((check.get("data") or {}).get("canFlashUpload")):
            return {"ok": True, "status": "hit", "error": ""}
        self.delete_upload_task(task_id)
        return {"ok": False, "status": "miss", "error": "光鸭资源库没有该文件（未命中）"}

    def delete_upload_task(self, task_id: str) -> None:
        """清掉没命中的上传任务占位，失败不影响主流程。"""
        try:
            self._api_call(
                "POST",
                "/nd.bizuserres.s/v1/file/delete_upload_task",
                {"taskIds": [str(task_id)]},
            )
        except GuangyaLoginError:
            pass

    def set_folder(self, folder_id: str, folder_path: str = "/") -> dict:
        self._save(
            root_folder_id=folder_id or "",
            root_folder_path=folder_path or "/",
        )
        return self.status()

    def check_folder(self, folder_id: str) -> tuple[bool, str]:
        """目标目录还在不在。空 id 是根目录，永远有效。"""
        if not folder_id:
            return True, ""
        data = self._api_call(
            "POST",
            "/nd.bizuserres.s/v1/file/get_info_by_file_id",
            {"fileId": str(folder_id)},
        )
        if isinstance(data.get("data"), dict) and data.get("data"):
            return True, ""
        return False, str(data.get("msg") or "目录不存在")

    def send_code(self, phone: str) -> dict:
        phone_number = normalize_phone(phone)
        device_id = self._device_id()
        with self._client() as client:
            captcha = self._post(
                client,
                "/v1/shield/captcha/init",
                {
                    "client_id": CLIENT_ID,
                    "action": "POST:/v1/auth/verification",
                    "device_id": device_id,
                    "meta": {
                        "username": phone_number,
                        "phone_number": phone_number,
                        "VERIFICATION_PHONE": phone_number,
                    },
                },
                device_id=device_id,
            )
            captcha_token = str(captcha.get("captcha_token") or "")
            if not captcha_token:
                raise GuangyaLoginError("光鸭没有返回 captcha_token")

            verification = self._post(
                client,
                "/v1/auth/verification",
                {
                    "phone_number": phone_number,
                    "target": "ANY",
                    "client_id": CLIENT_ID,
                },
                device_id=device_id,
                captcha_token=captcha_token,
            )
            verification_id = str(verification.get("verification_id") or "")
            if not verification_id:
                raise GuangyaLoginError("光鸭没有返回 verification_id")

        self._save(
            phone_number=phone_number,
            verification_id=verification_id,
            captcha_token=captcha_token,
            device_id=device_id,
            updated_at=iso(utcnow()),
            access_token="",
            refresh_token="",
        )
        return self.status()

    def login(self, code: str) -> dict:
        code = (code or "").strip()
        if not code:
            raise GuangyaLoginError("请输入短信验证码")
        state = self._state()
        phone = str(state.get("phone_number") or "")
        verification_id = str(state.get("verification_id") or "")
        device_id = str(state.get("device_id") or self._device_id())
        captcha_token = str(state.get("captcha_token") or "")
        if not phone or not verification_id:
            raise GuangyaLoginError("请先发送短信验证码")

        with self._client() as client:
            verified = self._post(
                client,
                "/v1/auth/verification/verify",
                {
                    "verification_id": verification_id,
                    "verification_code": code,
                    "client_id": CLIENT_ID,
                },
                device_id=device_id,
                captcha_token=captcha_token,
            )
            verification_token = str(verified.get("verification_token") or "")
            if not verification_token:
                raise GuangyaLoginError("短信验证码校验失败")

            signed = self._post(
                client,
                "/v1/auth/signin",
                {
                    "verification_code": code,
                    "verification_token": verification_token,
                    "username": phone,
                    "client_id": CLIENT_ID,
                },
                device_id=device_id,
                captcha_token=captcha_token,
            )
            access_token = str(signed.get("access_token") or "")
            refresh_token = str(signed.get("refresh_token") or "")
            if not access_token or not refresh_token:
                raise GuangyaLoginError("登录成功响应缺少 token")

            me_response = client.get(
                "/v1/user/me",
                headers={
                    **self._headers(device_id),
                    "Authorization": f"Bearer {access_token}",
                },
            )
            if me_response.is_error:
                raise GuangyaLoginError(f"登录态校验失败：{me_response.text[:200]}")

        self._save(
            access_token=access_token,
            refresh_token=refresh_token,
            verification_id="",
            captcha_token="",
            updated_at=iso(utcnow()),
        )
        return self.status()

    def connect_token(self, raw: str) -> dict:
        raw = (raw or "").strip()
        if not raw:
            raise GuangyaLoginError("请粘贴 access token 或 token JSON")
        token = raw
        refresh_token = ""
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                token = self._find_token(data, ("access_token", "accessToken", "token"))
                refresh_token = self._find_token(data, ("refresh_token", "refreshToken"))
            except json.JSONDecodeError as e:
                raise GuangyaLoginError("光鸭 Token JSON 格式不正确") from e
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if len(token) < 20:
            raise GuangyaLoginError("光鸭 access token 长度不正确")

        device_id = self._device_id()
        with self._client() as client:
            response = client.get(
                "/v1/user/me",
                headers={
                    **self._headers(device_id),
                    "Authorization": f"Bearer {token}",
                },
            )
            if response.is_error:
                try:
                    detail = self._error(response.json(), response)
                except ValueError:
                    detail = response.text[:200]
                raise GuangyaLoginError(f"光鸭 Token 无效：{detail}")

        self._save(
            access_token=token,
            refresh_token=refresh_token,
            verification_id="",
            captcha_token="",
            updated_at=iso(utcnow()),
        )
        return self.status()

    def logout(self) -> dict:
        state = self._state()
        self.store.set_json(
            STATE_KEY,
            {
                "device_id": state.get("device_id") or uuid.uuid4().hex,
            },
        )
        return self.status()

    @staticmethod
    def _find_token(data: Any, keys: tuple[str, ...]) -> str:
        if not isinstance(data, dict):
            return ""
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = GuangyaAuth._find_token(value, keys)
            if found:
                return found
        return ""
