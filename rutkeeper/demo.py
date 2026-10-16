"""Simulated routers so the dashboard is usable before any hardware is added."""

from __future__ import annotations

from rutkeeper.rutos import RutosError


class DemoSession:
    def __init__(self, device: dict):
        self.device = device

    def close(self) -> None:
        return None

    def system_status(self) -> dict:
        self._online()
        return {
            "success": True,
            "data": {
                "static": {
                    "fw_version": self.device.get("firmware") or "",
                    "device_name": self.device.get("model") or "",
                    "model": self.device.get("model") or "",
                    "hostname": self.device.get("name") or "",
                },
                "mnfinfo": {
                    "name": self.device.get("product_code") or "",
                    "serial": self.device.get("serial") or "",
                },
            },
        }

    def firmware_status(self) -> dict:
        self._online()
        return {
            "success": True,
            "data": {
                "version": self.device.get("firmware") or "",
                "kernel_version": "5.15.170",
            },
        }

    def updates_status(self) -> dict:
        self._online()
        offered = self.device.get("update_version") or ""
        if offered:
            return {
                "success": True,
                "data": {"device": {"version": offered, "size": "25165824"}},
            }
        return {"success": True, "data": {"stable_version": "newest"}}

    def download_backup(self, kind: str = "device") -> tuple[bytes, str]:
        self._online()
        name = self.device.get("name", "demo")
        payload = (
            f"rutkeeper demo {kind} backup\n"
            f"device={name}\n"
            f"product={self.device.get('product_code', '')}\n"
            f"firmware={self.device.get('firmware', '')}\n"
        ).encode("utf-8")
        return payload, f"{name.replace(' ', '_').lower()}-{kind}.tar.gz"

    def fota_download(self, channel: str = "stable") -> dict:
        self._online()
        if channel != "stable":
            raise RutosError("Rutkeeper only requests the stable channel.")
        if not self.device.get("update_version"):
            raise RutosError("No stable package is waiting on this router.")
        return {"success": True, "data": {"type": "stable"}}

    def progress(self) -> dict:
        self._online()
        return {"success": True, "data": {"status": "downloaded", "percent": 100}}

    def upgrade(self, keep_settings: bool) -> dict:
        self._online()
        return {"success": True, "data": {"keep_settings": int(keep_settings), "started": True}}

    def upload_backup(self, filename: str, content: bytes) -> dict:
        self._online()
        if not content:
            raise RutosError("Backup file is empty.")
        return {"success": True, "data": {"file": filename}}

    def apply_backup(self) -> dict:
        self._online()
        return {"success": True, "data": {"applied": True}}

    def _online(self) -> None:
        host = self.device.get("host") or ""
        if host.endswith(".offline"):
            raise RutosError("No route to host. The router did not answer on the LAN.")
