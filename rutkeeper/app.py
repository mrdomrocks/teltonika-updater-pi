"""Web app for the Rutkeeper Pi appliance."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from rutkeeper import __version__
from rutkeeper.crypto import SecretBox
from rutkeeper.db import DB
from rutkeeper.firmware import backup_compatibility, human_size, schedule_due
from rutkeeper.rms import parse_id_list, summarize_devices, task_group_body
from rutkeeper.rms import RmsClient, RmsError
from rutkeeper.rutos import RutosError, api_base
from rutkeeper.bench import Bench, connect_host
from rutkeeper.bench import Busy as BenchBusy
from rutkeeper.worker import Busy, Runner

PACKAGE = Path(__file__).resolve().parent
BUSY_LABELS = {
    "check": "Checking firmware",
    "backup": "Saving a backup",
    "update": "Installing firmware",
    "push": "Applying configuration",
}
WHEN_LABELS = {
    "first_monday": "First Monday of the month",
    "mondays": "Every Monday",
    "daily": "Every day",
    "manual": "Manual only",
}


def default_data_dir() -> Path:
    env = os.environ.get("RUTKEEPER_DATA")
    if env:
        return Path(env)
    return Path.cwd() / "data"


def format_local(value: object) -> str:
    if not value:
        return "—"
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().strftime("%d %b %Y %H:%M")


def next_window(settings: dict, now: datetime) -> str:
    mode = settings.get("when") or "manual"
    if mode == "manual":
        return "Nothing is scheduled."
    try:
        hour = int(settings.get("hour") or 4)
        minute = int(settings.get("minute") or 0)
    except ValueError:
        hour, minute = 4, 0
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    for _ in range(370):
        if schedule_due(candidate, mode, hour, minute):
            zone = candidate.tzname() or "local"
            return candidate.strftime("%A %d %b %Y %H:%M ") + zone
        candidate += timedelta(days=1)
    return "No upcoming window."


def pretty_json(text: str) -> str:
    if not text:
        return ""
    try:
        return json.dumps(json.loads(text), indent=2)[:8000]
    except (ValueError, TypeError):
        return text[:8000]


def create_app(data_dir: Path | None = None) -> FastAPI:
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / "backups").mkdir(parents=True, exist_ok=True)
    db = DB(data / "rutkeeper.sqlite")
    db.seed_demos()
    box = SecretBox(data / "secret.key")
    runner = Runner(db, box, data / "backups")
    runner.demo_delay = float(os.environ.get("RUTKEEPER_DEMO_DELAY", "0.5"))
    bench = Bench(db, box, data / "bench")
    schedule = os.environ.get("RUTKEEPER_DISABLE_SCHEDULER") != "1"

    env = Environment(
        loader=FileSystemLoader(PACKAGE / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["when"] = format_local
    env.filters["size"] = lambda value: human_size(int(value)) if value else "—"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if schedule:
            scheduler = BackgroundScheduler()
            scheduler.add_job(runner.tick, "cron", id="rutkeeper-tick", misfire_grace_time=30)
            scheduler.start()
            runner.tick()
            bench.start()
        yield
        bench.stop()
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        db.close()

    app = FastAPI(title="Rutkeeper", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.db = db
    app.state.runner = runner
    app.state.bench = bench
    app.state.secrets = box
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    def render(request: Request, template: str, status: int = 200, **ctx: object) -> HTMLResponse:
        settings = db.settings()
        now = datetime.now().astimezone()
        html = env.get_template(template).render(
            csrf=request.state.csrf,
            settings=settings,
            site_name=settings.get("site_name") or "Rutkeeper",
            armed=settings.get("armed") == "1",
            notice=request.query_params.get("notice", ""),
            version=__version__,
            any_busy=db.any_busy(),
            busy_labels=BUSY_LABELS,
            when_labels=WHEN_LABELS,
            now_label=now.strftime("%H:%M"),
            zone_label=now.tzname() or "local",
            now_epoch=int(now.timestamp() * 1000),
            path=request.url.path,
            **ctx,
        )
        return HTMLResponse(html, status_code=status)

    def redirect(url: str, notice: str = "") -> RedirectResponse:
        if notice:
            join = "&" if "?" in url else "?"
            url = f"{url}{join}notice={notice}"
        return RedirectResponse(url, status_code=303)

    def require_device(device_id: int) -> dict:
        device = db.device(device_id)
        if not device:
            raise HTTPException(404, "Router not found.")
        return device

    async def posted(request: Request):
        form = await request.form()
        sent = form.get("csrf") or request.headers.get("x-csrf")
        if sent != request.state.csrf:
            raise HTTPException(403, "The form expired. Reload the page and try again.")
        return form

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        token = request.cookies.get("csrf") or secrets.token_urlsafe(32)
        request.state.csrf = token
        response = await call_next(request)
        if request.cookies.get("csrf") != token:
            response.set_cookie("csrf", token, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "devices": len(db.devices())})

    @app.get("/")
    def bench_page(request: Request) -> HTMLResponse:
        return render(request, "bench.html", bench=bench.snapshot())

    @app.get("/bench/status")
    def bench_status() -> JSONResponse:
        return JSONResponse(bench.snapshot())

    @app.post("/bench/setup")
    async def bench_setup(request: Request):
        form = await posted(request)
        try:
            host = connect_host(str(form.get("host") or "192.168.1.1"))
        except RutosError as exc:
            return redirect("/", str(exc))
        bench.save_setup(
            host,
            str(form.get("username") or "admin"),
            str(form.get("target_password") or ""),
            bool(form.get("simulate")),
        )
        db.add_event("info", "bench", "Bench setup saved.")
        return redirect("/", "Bench setup saved.")

    @app.post("/bench/file")
    async def bench_file(request: Request):
        form = await posted(request)
        slot = str(form.get("slot") or "")
        upload = form.get("file")
        filename = getattr(upload, "filename", "") or ""
        content = await upload.read() if upload is not None and hasattr(upload, "read") else b""
        try:
            bench.store_file(slot, filename, content)
            if slot in {"config1", "config2"} and str(form.get("inside_password") or ""):
                bench.save_config_password(slot, str(form.get("inside_password") or ""))
        except RutosError as exc:
            return redirect("/", str(exc))
        return redirect("/", "File stored on this Pi.")

    @app.post("/bench/run")
    async def bench_run(request: Request):
        form = await posted(request)
        action = str(form.get("action") or "install")
        slot = str(form.get("config") or "")
        if action == "install":
            if slot not in {"config1", "config2"}:
                return redirect("/", "Select Config 1 or Config 2.")
        elif action not in {"firmware", "config1", "config2"}:
            return redirect("/", "Choose firmware or a config.")
        try:
            bench.start_job(action, str(form.get("current_password") or ""), slot)
        except BenchBusy:
            return redirect("/", "The bench is already working on this router.")
        return redirect("/", "Job started.")

    @app.get("/fleet")
    def fleet(request: Request) -> HTMLResponse:
        devices = db.devices()
        return render(
            request,
            "fleet.html",
            devices=devices,
            demo_count=sum(1 for device in devices if device["is_demo"]),
        )

    @app.post("/demos/remove")
    async def remove_demos(request: Request) -> RedirectResponse:
        await posted(request)
        db.delete_demos()
        db.add_event("info", "fleet", "Removed the simulated routers.")
        return redirect("/fleet", "Simulated routers removed.")

    @app.get("/devices/new")
    def new_device(request: Request) -> HTMLResponse:
        return render(request, "device_edit.html", device=_blank_device(), error="", creating=True)

    @app.post("/devices")
    async def create_device(request: Request):
        form = await posted(request)
        fields, error = _device_fields(form, box, existing=None)
        if error:
            draft = _blank_device()
            draft.update({key: fields.get(key, draft.get(key)) for key in draft if key in fields})
            draft["host"] = str(form.get("host") or "")
            draft["name"] = str(form.get("name") or "")
            return render(request, "device_edit.html", device=draft, error=error, creating=True, status=400)
        device_id = db.insert_device(fields)
        db.add_event("info", "fleet", f"Added {fields['name']}.", device_id=device_id)
        if form.get("check_after"):
            try:
                runner.start(device_id, "check")
            except Busy:
                pass
        return redirect(f"/devices/{device_id}", "Router saved.")

    @app.get("/devices/{device_id}")
    def device_page(request: Request, device_id: int) -> HTMLResponse:
        device = require_device(device_id)
        backups = [row for row in db.backups() if row.get("device_id") == device_id][:8]
        pushable = []
        blocked_profiles = []
        for row in db.backups():
            if not row.get("is_profile"):
                continue
            ok, reason = backup_compatibility(
                row.get("firmware") or "",
                device.get("firmware") or "",
                row.get("product_code") or "",
                device.get("product_code") or "",
            )
            if ok:
                pushable.append(row)
            else:
                item = dict(row)
                item["blocked"] = reason
                blocked_profiles.append(item)
        return render(
            request,
            "device.html",
            device=device,
            error="",
            backups=backups,
            pushable=pushable,
            blocked_profiles=blocked_profiles,
            events=db.events(limit=12, device_id=device_id),
            detail=pretty_json(device.get("last_detail") or ""),
            backup_kind=db.setting("backup_kind", "device"),
        )

    @app.post("/devices/{device_id}")
    async def save_device(request: Request, device_id: int):
        device = require_device(device_id)
        form = await posted(request)
        fields, error = _device_fields(form, box, existing=device)
        if error:
            merged = dict(device)
            merged.update(fields)
            merged["host"] = str(form.get("host") or device["host"])
            return render(
                request,
                "device.html",
                device=merged,
                error=error,
                backups=[],
                pushable=[],
                blocked_profiles=[],
                events=db.events(limit=12, device_id=device_id),
                detail=pretty_json(device.get("last_detail") or ""),
                backup_kind=db.setting("backup_kind", "device"),
                status=400,
            )
        db.update_device(device_id, fields)
        return redirect(f"/devices/{device_id}", "Router saved.")

    @app.post("/devices/{device_id}/delete")
    async def delete_device(request: Request, device_id: int) -> RedirectResponse:
        device = require_device(device_id)
        await posted(request)
        db.delete_device(device_id)
        db.add_event("info", "fleet", f"Removed {device['name']}.")
        return redirect("/fleet", "Router removed.")

    @app.post("/devices/{device_id}/check")
    async def check_device(request: Request, device_id: int) -> RedirectResponse:
        require_device(device_id)
        await posted(request)
        return _queue(device_id, "check")

    @app.post("/devices/{device_id}/backup")
    async def backup_device(request: Request, device_id: int) -> RedirectResponse:
        require_device(device_id)
        form = await posted(request)
        kind = str(form.get("kind") or "device")
        if kind not in {"device", "configuration"}:
            kind = "device"
        return _queue(device_id, "backup", kind=kind)

    @app.post("/devices/{device_id}/update")
    async def update_device(request: Request, device_id: int) -> RedirectResponse:
        device = require_device(device_id)
        form = await posted(request)
        if form.get("confirm") != "1":
            return redirect(f"/devices/{device_id}", "Confirm the firmware install before starting it.")
        if device.get("update_state") != "available":
            return redirect(f"/devices/{device_id}", "Check the router first. No stable package is waiting.")
        return _queue(device_id, "update")

    @app.post("/devices/{device_id}/push")
    async def push_device(request: Request, device_id: int) -> RedirectResponse:
        require_device(device_id)
        form = await posted(request)
        if form.get("confirm") != "1":
            return redirect(
                f"/devices/{device_id}",
                "Confirm the config push. It replaces settings, including the admin password.",
            )
        try:
            backup_id = int(form.get("backup_id") or 0)
        except ValueError:
            return redirect(f"/devices/{device_id}", "Choose a backup to push.")
        return _queue(device_id, "push", backup_id=backup_id)

    def _queue(device_id: int, action: str, **kwargs: object) -> RedirectResponse:
        try:
            runner.start(device_id, action, **kwargs)
        except Busy:
            return redirect(f"/devices/{device_id}", "This router already has a job running.")
        return redirect(f"/devices/{device_id}", "Job started.")

    @app.get("/backups")
    def backups_page(request: Request) -> HTMLResponse:
        rows = []
        devices = db.devices()
        for row in db.backups():
            item = dict(row)
            compatible = []
            for device in devices:
                ok, _reason = backup_compatibility(
                    item.get("firmware") or "",
                    device.get("firmware") or "",
                    item.get("product_code") or "",
                    device.get("product_code") or "",
                )
                if ok:
                    compatible.append(device)
            item["compatible"] = compatible
            rows.append(item)
        return render(request, "backups.html", backups=rows, error="")

    @app.post("/backups")
    async def upload_backup(request: Request):
        form = await posted(request)
        upload = form.get("file")
        filename = getattr(upload, "filename", "") or ""
        content = await upload.read() if upload is not None and hasattr(upload, "read") else b""
        product = str(form.get("product_code") or "").strip()
        firmware = str(form.get("firmware") or "").strip()
        profile_name = str(form.get("profile_name") or "").strip()
        if not content:
            return render(request, "backups.html", backups=_annotated_backups(db), error="Choose a backup file.", status=400)
        if len(content) > 32 * 1024 * 1024:
            return render(request, "backups.html", backups=_annotated_backups(db), error="Backup files must be 32 MB or smaller.", status=400)
        if not product or not firmware:
            return render(
                request,
                "backups.html",
                backups=_annotated_backups(db),
                error="Product code and firmware version are required so a push can be checked.",
                status=400,
            )
        import hashlib

        digest = hashlib.sha256(content).hexdigest()
        stored = f"{digest[:16]}-{_safe_upload(filename)}"
        path = data / "backups" / stored
        path.write_bytes(content)
        path.chmod(0o600)
        db.insert_backup(
            {
                "device_id": None,
                "filename": _safe_upload(filename),
                "stored_name": stored,
                "sha256": digest,
                "product_code": product,
                "firmware": firmware,
                "kind": str(form.get("kind") or "device"),
                "size_bytes": len(content),
                "is_profile": 1 if profile_name else 0,
                "profile_name": profile_name,
                "note": "Uploaded from this browser.",
            }
        )
        db.add_event("info", "backup", f"Uploaded backup {profile_name or filename} for {product}.")
        return redirect("/backups", "Backup stored on this Pi.")

    @app.post("/backups/{backup_id}/profile")
    async def mark_profile(request: Request, backup_id: int) -> RedirectResponse:
        row = db.backup(backup_id)
        if not row:
            raise HTTPException(404, "Backup not found.")
        form = await posted(request)
        name = str(form.get("profile_name") or row.get("filename") or "Profile").strip()
        enabled = 1 if form.get("is_profile") else 0
        db.update_backup(backup_id, {"is_profile": enabled, "profile_name": name if enabled else row.get("profile_name") or ""})
        return redirect("/backups", "Backup updated.")

    @app.post("/backups/{backup_id}/delete")
    async def delete_backup(request: Request, backup_id: int) -> RedirectResponse:
        await posted(request)
        row = db.delete_backup(backup_id)
        if row:
            path = data / "backups" / row["stored_name"]
            if path.exists():
                path.unlink()
        return redirect("/backups", "Backup deleted.")

    @app.post("/backups/{backup_id}/push")
    async def push_from_library(request: Request, backup_id: int) -> RedirectResponse:
        if not db.backup(backup_id):
            raise HTTPException(404, "Backup not found.")
        form = await posted(request)
        if form.get("confirm") != "1":
            return redirect("/backups", "Confirm the config push. It replaces settings, including the admin password.")
        try:
            device_id = int(form.get("device_id") or 0)
        except ValueError:
            return redirect("/backups", "Choose a router.")
        require_device(device_id)
        return _queue(device_id, "push", backup_id=backup_id)

    @app.get("/backups/{backup_id}/download")
    def download_backup(backup_id: int) -> FileResponse:
        row = db.backup(backup_id)
        if not row:
            raise HTTPException(404, "Backup not found.")
        path = data / "backups" / row["stored_name"]
        if not path.exists():
            raise HTTPException(404, "Backup file is missing.")
        return FileResponse(path, filename=row["filename"], media_type="application/octet-stream")

    @app.get("/schedule")
    def schedule_page(request: Request) -> HTMLResponse:
        settings = db.settings()
        now = datetime.now().astimezone()
        return render(
            request,
            "schedule.html",
            error="",
            upcoming=next_window(settings, now),
        )

    @app.post("/schedule")
    async def save_schedule(request: Request):
        form = await posted(request)
        when = str(form.get("when") or "manual")
        if when not in WHEN_LABELS:
            when = "manual"
        try:
            hour = int(form.get("hour") or 4)
            minute = int(form.get("minute") or 0)
        except ValueError:
            hour, minute = -1, -1
        site = str(form.get("site_name") or "Rutkeeper").strip()[:60] or "Rutkeeper"
        kind = str(form.get("backup_kind") or "device")
        if kind not in {"device", "configuration"}:
            kind = "device"
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return render(request, "schedule.html", error="Use an hour from 0 to 23 and a minute from 0 to 59.", upcoming="", status=400)
        db.set_settings(
            {
                "site_name": site,
                "when": when,
                "hour": str(hour),
                "minute": str(minute),
                "armed": "1" if form.get("armed") else "0",
                "backup_kind": kind,
            }
        )
        db.add_event(
            "info",
            "schedule",
            f"Schedule saved. {WHEN_LABELS[when]} at {hour:02d}:{minute:02d}. "
            + ("Updates are armed." if form.get("armed") else "Updates are disarmed."),
        )
        return redirect("/schedule", "Schedule saved.")

    @app.get("/rms")
    def rms_page(request: Request) -> HTMLResponse:
        return render(request, "rms.html", error="", result="", tasks="", preview=_rms_preview(db))

    @app.post("/rms")
    async def rms_action(request: Request):
        form = await posted(request)
        action = str(form.get("action") or "save")
        error = _save_rms(form, db, box)
        if error:
            return render(request, "rms.html", error=error, result="", tasks="", preview=_rms_preview(db), status=400)
        if action == "save":
            db.add_event("info", "rms", "Saved RMS settings.")
            return redirect("/rms", "RMS settings saved.")
        token = box.decrypt(db.setting("rms_token_enc"))
        if not token:
            return render(
                request,
                "rms.html",
                error="Add an RMS access token first.",
                result="",
                tasks="",
                preview=_rms_preview(db),
                status=400,
            )
        client = RmsClient(token, db.setting("rms_base"))
        try:
            if action == "test":
                payload = client.list_devices()
                count, names = summarize_devices(payload)
                if count is None:
                    result = "Token accepted. RMS did not return a device list this call understands."
                else:
                    shown = ", ".join(names) if names else "no names in the first page"
                    result = f"Token accepted. {count} device{'s' if count != 1 else ''} visible. {shown}."
                db.add_event("info", "rms", "RMS token accepted.")
                return render(request, "rms.html", error="", result=result, tasks="", preview=_rms_preview(db))
            if action == "tasks":
                payload = client.list_tasks()
                return render(
                    request,
                    "rms.html",
                    error="",
                    result="",
                    tasks=json.dumps(payload, indent=2)[:12000],
                    preview=_rms_preview(db),
                )
            if action == "run":
                if form.get("confirm") != "1":
                    return render(
                        request,
                        "rms.html",
                        error="Confirm the task group before running it. It can install firmware.",
                        result="",
                        tasks="",
                        preview=_rms_preview(db),
                        status=400,
                    )
                runner.run_rms(scheduled=False)
                return redirect("/rms", "RMS task group sent.")
        except (RmsError, ValueError) as exc:
            return render(
                request,
                "rms.html",
                error=str(exc),
                result="",
                tasks="",
                preview=_rms_preview(db),
                status=400,
            )
        finally:
            client.close()
        return redirect("/rms")

    @app.get("/log")
    def log_page(request: Request) -> HTMLResponse:
        return render(request, "log.html", events=db.events(limit=200))

    async def http_error(request: Request, exc: Exception) -> HTMLResponse:
        status = getattr(exc, "status_code", 500)
        detail = getattr(exc, "detail", "Something went wrong.")
        if status == 404:
            if not hasattr(request.state, "csrf"):
                request.state.csrf = request.cookies.get("csrf") or ""
            return render(request, "error.html", status=404, message="That page is not on this Pi.")
        return HTMLResponse(str(detail), status_code=status)

    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(404, http_error)

    return app


def _blank_device() -> dict:
    return {
        "id": None,
        "name": "",
        "host": "",
        "username": "admin",
        "verify_tls": 0,
        "auto_apply": 0,
        "keep_settings": 1,
        "schedule_enabled": 1,
        "notes": "",
        "is_demo": 0,
        "firmware": "",
        "product_code": "",
        "model": "",
        "serial": "",
        "update_state": "unknown",
        "update_version": "",
        "busy": 0,
        "busy_action": "",
        "last_error": "",
        "last_seen": None,
        "last_backup_at": None,
    }


def _device_fields(form, box: SecretBox, existing: dict | None) -> tuple[dict, str]:
    name = str(form.get("name") or "").strip()
    host_raw = str(form.get("host") or "").strip()
    username = str(form.get("username") or "admin").strip() or "admin"
    if not name:
        return {}, "Name the router."
    if not host_raw:
        return {}, "Add the router address."
    if host_raw.startswith("demo://"):
        host = host_raw
    else:
        try:
            host = api_base(host_raw).removesuffix("/api")
        except RutosError as exc:
            return {}, str(exc)
    password = str(form.get("password") or "")
    is_demo = host.startswith("demo://")
    if not is_demo and not password and not (existing and existing.get("password_enc")):
        return {}, "Add the admin password. It is encrypted and stays on this Pi."
    fields: dict = {
        "name": name[:80],
        "host": host,
        "username": username[:80],
        "verify_tls": 1 if form.get("verify_tls") else 0,
        "auto_apply": 1 if form.get("auto_apply") else 0,
        "keep_settings": 1 if form.get("keep_settings") else 0,
        "schedule_enabled": 1 if form.get("schedule_enabled") else 0,
        "is_demo": 1 if is_demo else 0,
        "notes": str(form.get("notes") or "")[:500],
    }
    if password:
        fields["password_enc"] = box.encrypt(password)
    elif existing is None:
        fields["password_enc"] = ""
    return fields, ""


def _save_rms(form, db: DB, box: SecretBox) -> str:
    base = str(form.get("base") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return "The RMS API host must be an https URL."
    group = str(form.get("group_id") or "").strip()
    if group and not group.replace("-", "").replace("_", "").isalnum():
        return "The task group id should be the id from the RMS task group URL."
    try:
        parse_id_list(str(form.get("device_ids") or ""))
        parse_id_list(str(form.get("task_ids") or ""))
    except ValueError:
        return "RMS device ids and task ids must be numbers, separated by commas."
    values = {
        "rms_base": base,
        "rms_group_id": group,
        "rms_device_ids": str(form.get("device_ids") or "").strip(),
        "rms_task_ids": str(form.get("task_ids") or "").strip(),
        "rms_in_schedule": "1" if form.get("rms_in_schedule") else "0",
    }
    if form.get("clear_token"):
        values["rms_token_enc"] = ""
    else:
        token = str(form.get("token") or "").strip()
        if token:
            values["rms_token_enc"] = box.encrypt(token)
    db.set_settings(values)
    return ""


def _rms_preview(db: DB) -> str:
    settings = db.settings()
    try:
        body = task_group_body(
            parse_id_list(settings.get("rms_device_ids") or ""),
            parse_id_list(settings.get("rms_task_ids") or ""),
        )
    except ValueError:
        return ""
    group = settings.get("rms_group_id") or "{task_group_id}"
    base = settings.get("rms_base") or "https://api.rms.teltonika-networks.com"
    return (
        f"POST {base}/devices/tasks/groups/{group}\n"
        "Authorization: Bearer [redacted]\n"
        "Content-Type: application/json\n\n"
        + json.dumps(body, indent=2)
    )


def _annotated_backups(db: DB) -> list[dict]:
    devices = db.devices()
    rows = []
    for row in db.backups():
        item = dict(row)
        item["compatible"] = []
        for device in devices:
            ok, _reason = backup_compatibility(
                item.get("firmware") or "",
                device.get("firmware") or "",
                item.get("product_code") or "",
                device.get("product_code") or "",
            )
            if ok:
                item["compatible"].append(device)
        rows.append(item)
    return rows


def _safe_upload(name: str) -> str:
    import re
    from pathlib import Path as P

    base = P(name).name or "backup.bin"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (cleaned or "backup.bin")[:80]
