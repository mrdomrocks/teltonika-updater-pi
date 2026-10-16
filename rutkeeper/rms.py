"""Thin client for the Teltonika RMS API.

The community workaround for a monthly stable update is a task group in RMS,
triggered with:

    POST /devices/tasks/groups/{task_group_id}
    Authorization: Bearer <token>
    {"device_id":[...], "tasks":[{"task_id": ..., "variables": []}, ...]}

TUA runs that call from the Pi instead of from a crontab on each router.
"""

from __future__ import annotations

from typing import Any

import httpx


class RmsError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def parse_id_list(text: str) -> list[int]:
    parts = [piece for piece in (text or "").replace(";", ",").replace("\n", ",").split(",")]
    ids: list[int] = []
    for piece in parts:
        item = piece.strip()
        if not item:
            continue
        ids.append(int(item))
    return ids


def task_group_body(device_ids: list[int], task_ids: list[int]) -> dict[str, Any]:
    return {
        "device_id": device_ids,
        "tasks": [{"task_id": task_id, "variables": []} for task_id in task_ids],
    }


def summarize_devices(payload: object) -> tuple[int | None, list[str]]:
    data: object = payload
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            data = data.get("devices") or data.get("items") or data.get("results") or []
    if not isinstance(data, list):
        return None, []
    names: list[str] = []
    for item in data[:8]:
        if isinstance(item, dict):
            names.append(
                str(item.get("name") or item.get("device_name") or item.get("model") or item.get("id") or "device")
            )
        else:
            names.append(str(item))
    return len(data), names


class RmsClient:
    def __init__(self, token: str, base: str = "https://api.rms.teltonika-networks.com", timeout: float = 30):
        self.base = base.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RmsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, json: dict | None = None) -> tuple[int, object, str]:
        try:
            response = self._client.request(method, f"{self.base}{path}", json=json)
        except httpx.HTTPError as exc:
            raise RmsError(f"Could not reach RMS: {exc.__class__.__name__}.") from exc
        text = response.text[:4000]
        try:
            payload: object = response.json()
        except ValueError:
            payload = {"raw": text}
        if response.status_code == 401:
            raise RmsError("RMS rejected the token.", status=401, body=text)
        if response.status_code >= 400:
            message = "RMS request failed."
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("error") or message)
            raise RmsError(f"{message} ({response.status_code}).", status=response.status_code, body=text)
        return response.status_code, payload, text

    def list_devices(self) -> object:
        _status, payload, _text = self._request("GET", "/devices")
        return payload

    def list_tasks(self) -> object:
        _status, payload, _text = self._request("GET", "/devices/tasks")
        return payload

    def run_group(self, group_id: str, device_ids: list[int], task_ids: list[int]) -> object:
        if not str(group_id).strip():
            raise RmsError("Task group id is missing.")
        if not device_ids:
            raise RmsError("Add at least one RMS device id.")
        if not task_ids:
            raise RmsError("Add at least one task id.")
        _status, payload, _text = self._request(
            "POST",
            f"/devices/tasks/groups/{str(group_id).strip()}",
            json=task_group_body(device_ids, task_ids),
        )
        return payload
