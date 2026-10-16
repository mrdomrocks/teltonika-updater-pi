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
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(data.get("token") or data.get("session") or payload.get("token") or "")


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
        last: RutosError | None = None
        bodies = (
            {"username": self.username, "password": self.password},
            {"data": {"username": self.username, "password": self.password}},
        )
        for body in bodies:
            try:
                response = self._client.post(f"{self.base}/login", json=body)
            except httpx.HTTPError as exc:
                raise RutosError(f"Could not reach the router: {exc.__class__.__name__}.") from exc
            payload: object
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            token = _token(payload)
            if response.status_code < 400 and token:
                self.token = token
                return
            last = RutosError(
                _error_message(payload, f"Login failed ({response.status_code})."),
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
