"""HTTP client for the RutOS web API on a router you administer.

Login is POST /api/login. Later calls send Authorization: Bearer. Router
certificates are usually self-signed, so TLS verification is off unless the
device record turns it on.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx


class RutosError(Exception):
    def __init__(self, message: str, *, body: str = "", status: int | None = None):
        super().__init__(message)
        self.body = body
        self.status = status


def api_base(host: str) -> str:
    text = (host or "").strip().rstrip("/")
    if not text:
        raise RutosError("Router address is empty.")
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    if text.endswith("/api"):
        text = text[: -len("/api")]
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RutosError("Router address must be a host or http(s) URL.")
    return text + "/api"


def _wireless_rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        if data.get("id") or "ssid" in data:
            return [data]
        rows: list[dict] = []
        for key, value in data.items():
            if isinstance(value, dict):
                value.setdefault("id", str(key))
                rows.append(value)
        return rows
    return []


def _wifi_iface(row: dict) -> bool:
    kind = str(row.get(".type") or row.get("type") or "")
    if kind == "wifi-device":
        return False
    return "ssid" in row or kind == "wifi-iface"


def _error_message(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("error") or first.get("message") or fallback)
            return str(first)
        if payload.get("error"):
            return str(payload["error"])
    return fallback


def _token(payload: object) -> str:
    """Session id from a RutOS login.

    Current firmware returns data.token. Factory images from before the 7.06
    API return ubus_rpc_session at the top of the same response, with the
    password accepted and no token field.
    """
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (data, payload):
        for key in ("token", "ubus_rpc_session", "session"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _truthy(value: object) -> bool:
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def _password_change_required(payload: object) -> bool:
    """Factory-reset units accept the label password, then demand a new one."""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (data, payload):
        if _truthy(source.get("password_required")) or _truthy(source.get("must_change")):
            return True
    text = _error_message(payload, "").lower()
    return "password" in text and "chang" in text


def _lua_number_string_error(message: str) -> bool:
    """RutOS 07.24 users.lua crashes once the current password is accepted."""
    text = message.lower()
    return "number expected" in text and "string" in text


def _try_plain_http(exc: RutosError) -> bool:
    """Retry over HTTP only when HTTPS never produced an authentication answer."""
    if exc.status is None or exc.status in {301, 302, 303, 307, 308}:
        return True
    if exc.status != 200:
        return False
    text = str(exc).lower()
    return not any(word in text for word in ("password", "unauthorized", "denied", "invalid"))


class RutosClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base = api_base(host)
        self.username = username or "admin"
        self.password = password
        self.token = ""
        self.password_change_required = False
        self._client = httpx.Client(
            verify=verify_tls,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RutosClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def login(self) -> None:
        self.password_change_required = False
        try:
            self._login_at(self.base)
            return
        except RutosError as exc:
            if not self.base.startswith("https://") or not _try_plain_http(exc):
                raise
        self._login_at("http://" + self.base.removeprefix("https://"))

    def _login_at(self, base: str) -> None:
        last: RutosError | None = None
        bodies = (
            {"username": self.username, "password": self.password},
            {"data": {"username": self.username, "password": self.password}},
        )
        for body in bodies:
            try:
                response = self._client.post(f"{base}/login", json=body)
            except httpx.HTTPError as exc:
                raise RutosError(f"Could not reach the router: {exc.__class__.__name__}.") from exc
            payload: object
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            token = _token(payload)
            needs_change = _password_change_required(payload)
            if token and (response.status_code < 400 or needs_change):
                self.token = token
                self.password_change_required = needs_change
                self.base = base
                return
            fallback = f"Login failed ({response.status_code})."
            if response.status_code < 400:
                fallback = "The router answered but did not start a session."
            last = RutosError(
                _error_message(payload, fallback),
                body=response.text[:2000],
                status=response.status_code,
            )
        assert last is not None
        raise last

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.token:
            self.login()
        url = path if path.startswith("http") else f"{self.base}{path}"
        headers = dict(self._headers())
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RutosError(f"Could not reach the router: {exc.__class__.__name__}.") from exc
        if response.status_code == 401:
            self.login()
            headers = dict(self._headers())
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise RutosError(f"Could not reach the router: {exc.__class__.__name__}.") from exc
        return response

    def get_json(self, path: str) -> dict:
        response = self.request("GET", path)
        return self._json_or_error(response, f"GET {path} failed")

    def post_json(self, path: str, body: dict | None) -> dict:
        response = self.request("POST", path, json=body if body is not None else {})
        return self._json_or_error(response, f"POST {path} failed")

    def put_json(self, path: str, body: dict) -> dict:
        response = self.request("PUT", path, json=body)
        return self._json_or_error(response, f"PUT {path} failed")

    def set_wireless_ssids(self, ssid: str, key: str) -> int:
        """Rename every Wi-Fi interface and set its passphrase. The config file is already applied."""
        rows: list[dict] = []
        last: RutosError | None = None
        for path in ("/wireless/interfaces/config", "/wireless/interfaces"):
            try:
                rows = _wireless_rows(self.get_json(path))
            except RutosError as exc:
                last = exc
                continue
            if rows:
                break
        ifaces = [row for row in rows if _wifi_iface(row)]
        if not ifaces:
            raise last or RutosError("The router did not list a wireless name to change.")
        changed = 0
        for row in ifaces:
            iface_id = str(row.get("id") or "").strip()
            if not iface_id:
                continue
            saved = False
            body = {"data": {"ssid": ssid, "key": key}}
            for path in (f"/wireless/interfaces/config/{iface_id}", f"/wireless/interfaces/{iface_id}"):
                try:
                    self.put_json(path, body)
                    saved = True
                    break
                except RutosError as exc:
                    last = exc
            if not saved:
                raise last or RutosError(f"The router did not accept the wireless name {ssid}.")
            changed += 1
        if not changed:
            raise RutosError("The router did not list a wireless name to change.")
        try:
            self.post_json("/wireless/actions/reload", {})
        except RutosError:
            pass
        return changed

    def _json_or_error(self, response: httpx.Response, fallback: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:2000]}
        if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
            raise RutosError(
                _error_message(payload, f"{fallback} ({response.status_code})."),
                body=response.text[:2000],
                status=response.status_code,
            )
        if not isinstance(payload, dict):
            return {"data": payload}
        return payload

    def system_status(self) -> dict:
        return self.get_json("/system/device/status")

    def firmware_status(self) -> dict:
        return self.get_json("/firmware/device/status")

    def updates_status(self) -> dict:
        return self.get_json("/firmware/device/updates/status")

    def fota_download(self, channel: str = "stable") -> dict:
        return self.post_json("/firmware/actions/fota_download", {"data": {"type": channel}})

    def progress(self) -> dict:
        response = self.request("GET", "/firmware/device/progress/status")
        if response.status_code == 404:
            return {"_http_status": 404, "success": False}
        try:
            payload = response.json()
        except ValueError:
            return {"raw": response.text[:2000], "_http_status": response.status_code}
        if isinstance(payload, dict):
            payload["_http_status"] = response.status_code
            return payload
        return {"data": payload, "_http_status": response.status_code}

    def upgrade(self, keep_settings: bool) -> dict:
        from rutkeeper.firmware import upgrade_body

        return self.post_json("/firmware/actions/upgrade", upgrade_body(keep_settings))

    def upload_firmware(self, filename: str, content: bytes) -> dict:
        """Stage a firmware image that is already stored on the Pi."""
        last: RutosError | None = None
        for path in ("/firmware/actions/upload_device_firmware", "/firmware/actions/upload"):
            try:
                response = self.request(
                    "POST",
                    path,
                    files={"file": (filename, content, "application/octet-stream")},
                    timeout=300,
                )
            except RutosError as exc:
                last = exc
                continue
            if response.status_code == 404:
                last = RutosError("This firmware has no local upload endpoint.", status=404)
                continue
            return self._json_or_error(response, "Firmware upload failed")
        raise last or RutosError("Firmware upload failed.")

    def upgrade_staged(self) -> dict:
        """Install the image just uploaded. Settings are kept so we can sign in again."""
        try:
            return self.upgrade(True)
        except RutosError:
            return self.post_json(
                "/firmware/actions/upgrade",
                {"data": {"keep_settings": "1", "suppress_validation": "1"}},
            )

    def set_admin_password(self, current: str, new: str) -> str:
        """Replace the admin password. Tries the first-login action, then the users API."""
        errors: list[str] = []
        first_bodies = (
            {"data": {"current_password": current, "password": new, "password_confirm": new}},
            {"data": {"password": new, "password_confirm": new}},
        )
        for body in first_bodies:
            try:
                self.post_json("/system/actions/change_password_firstlogin", body)
                return "first login"
            except RutosError as exc:
                errors.append(str(exc))
        user_id = "login1"
        try:
            payload = self.get_json("/users/config")
            rows = payload.get("data")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("username", "")).lower() == "admin" and row.get("id"):
                        user_id = str(row["id"])
                        break
        except RutosError as exc:
            errors.append(str(exc))
        put_body = {
            "data": {
                "current_password": current,
                "password": new,
                "password_confirm": new,
                ".type": "login",
            }
        }
        try:
            response = self.request("PUT", f"/users/config/{user_id}", json=put_body)
            self._json_or_error(response, "Password change failed")
            return f"users/{user_id}"
        except RutosError as exc:
            errors.append(str(exc))
            if _lua_number_string_error(str(exc)):
                try:
                    self._set_password_ubus(current, new)
                except RutosError as ubus_exc:
                    if "07.25.1" in str(ubus_exc):
                        raise
                    raise RutosError(
                        "This firmware cannot change the admin password. Put firmware 07.25.1 or newer in the Firmware folder."
                    ) from ubus_exc
                return "system"
        detail = errors[-1] if errors else "The router did not accept a password change."
        raise RutosError(detail)

    def _origin(self) -> str:
        if self.base.endswith("/api"):
            return self.base[: -len("/api")]
        return self.base

    def _ubus_call(self, session: str, obj: str, method: str, params: dict) -> list:
        try:
            response = self._client.post(
                f"{self._origin()}/ubus",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "call",
                    "params": [session, obj, method, params],
                },
            )
        except httpx.HTTPError as exc:
            raise RutosError(f"Could not reach the router: {exc.__class__.__name__}.") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RutosError("The router did not accept the password change.") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, list) or not result or result[0] != 0:
            raise RutosError("The router did not accept the password change.")
        return result

    def _set_password_ubus(self, current: str, new: str) -> None:
        """Set the password when users.lua rejects a string id. Firmware 07.25.1 fixed that."""
        logged = self._ubus_call(
            "00000000000000000000000000000000",
            "session",
            "login",
            {"username": self.username, "password": current},
        )
        info = logged[1] if len(logged) > 1 and isinstance(logged[1], dict) else {}
        session = info.get("ubus_rpc_session")
        if not isinstance(session, str) or not session.strip():
            raise RutosError("The router did not accept the password change.")
        self._ubus_call(
            session,
            "rpc-sys",
            "password_set",
            {"user": self.username, "password": new},
        )
        saved = self.password
        self.password = new
        self.token = ""
        try:
            self.login()
        except RutosError as exc:
            self.password = saved
            self.token = ""
            raise RutosError(
                "This firmware cannot change the admin password. Put firmware 07.25.1 or newer in the Firmware folder."
            ) from exc
        self.password = saved
        self.token = ""

    def download_backup(self, kind: str = "device") -> tuple[bytes, str]:
        attempts: list[dict] = []
        if kind == "configuration":
            attempts.append({"encrypt": "0", "type": "configuration"})
        else:
            attempts.append({"encrypt": "0", "type": "device"})
        attempts.append({"encrypt": "0"})
        errors: list[str] = []
        for body in attempts:
            try:
                response = self.request(
                    "POST",
                    "/backup/actions/download",
                    json={"data": body},
                    timeout=120,
                )
            except RutosError as exc:
                errors.append(str(exc))
                continue
            saved = _backup_bytes(response)
            if saved is not None:
                return saved
            errors.append(_json_error(response) or f"Backup call returned {response.status_code} without a file.")
        detail = errors[-1] if errors else "The router did not return a backup file."
        raise RutosError(detail)

    def upload_backup(self, filename: str, content: bytes) -> dict:
        response = self.request(
            "POST",
            "/backup/actions/upload",
            files={"file": (filename, content, "application/octet-stream")},
            timeout=120,
        )
        return self._json_or_error(response, "Backup upload failed")

    def apply_backup(self) -> dict:
        return self.post_json("/backup/actions/apply", {"data": {"encrypt": "0"}})


def _json_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict) and payload.get("success") is False:
        return _error_message(payload, "")
    return ""


def _backup_bytes(response: httpx.Response) -> tuple[bytes, str] | None:
    content_type = response.headers.get("content-type", "")
    disposition = response.headers.get("content-disposition", "")
    filename = "backup.tar.gz"
    match = re.search(r'filename="?([^";]+)', disposition)
    if match:
        filename = match.group(1)
    if response.status_code >= 400:
        return None
    if "json" in content_type or response.content[:1] in (b"{", b"["):
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("success") is False:
                return None
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            encoded = data.get("content") or data.get("file") or payload.get("content")
            if isinstance(encoded, str) and encoded.startswith(("H4sI", "UEs", "e30")):
                import base64

                try:
                    return base64.b64decode(encoded), filename
                except ValueError:
                    return None
            return None
    if response.content:
        return response.content, filename
    return None
