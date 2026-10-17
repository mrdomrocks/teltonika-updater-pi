"""Web app for the TUA Pi appliance."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from rutkeeper import __version__
from rutkeeper.crypto import SecretBox
from rutkeeper.db import DB
from rutkeeper.firmware import human_size
from rutkeeper.rms import parse_id_list, summarize_devices, task_group_body
from rutkeeper.rms import RmsClient, RmsError
from rutkeeper.rutos import RutosError
from rutkeeper.bench import Bench, connect_host
from rutkeeper.bench import Busy as BenchBusy
from rutkeeper.worker import Runner

PACKAGE = Path(__file__).resolve().parent


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
    box = SecretBox(data / "secret.key")
    runner = Runner(db, box, data / "backups")
    runner.demo_delay = float(os.environ.get("RUTKEEPER_DEMO_DELAY", "0.5"))
    bench = Bench(db, box, data / "bench")
    watch = os.environ.get("RUTKEEPER_DISABLE_SCHEDULER") != "1"

    env = Environment(
        loader=FileSystemLoader(PACKAGE / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["when"] = format_local
    env.filters["size"] = lambda value: human_size(int(value)) if value else "—"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if watch:
            bench.start()
        yield
        bench.stop()
        db.close()

    app = FastAPI(title="TUA", docs_url=None, redoc_url=None, lifespan=lifespan)
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
            site_name="TUA" if (settings.get("site_name") or "TUA") in {"", "Rutkeeper", "TUA"} else settings.get("site_name"),
            notice=request.query_params.get("notice", ""),
            version=__version__,
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
        try:
            next_host = str(form.get("next_host") or "")
            if next_host.strip():
                next_host = connect_host(next_host)
        except RutosError as exc:
            return redirect("/", str(exc))
        bench.save_setup(
            host,
            str(form.get("username") or "admin"),
            next_host,
            bool(form.get("simulate")),
        )
        db.add_event("info", "workbench", "Workbench setup saved.")
        return redirect("/", "Workbench setup saved.")

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
            return redirect("/", "The workbench is already working on this router.")
        return redirect("/", "Job started.")

    @app.get("/backups")
    def backups_page(request: Request) -> HTMLResponse:
        return render(request, "backups.html", backups=_annotated_backups(db), error="")

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
                error="Product code and firmware version are required.",
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

    @app.get("/backups/{backup_id}/download")
    def download_backup(backup_id: int) -> FileResponse:
        row = db.backup(backup_id)
        if not row:
            raise HTTPException(404, "Backup not found.")
        path = data / "backups" / row["stored_name"]
        if not path.exists():
            raise HTTPException(404, "Backup file is missing.")
        return FileResponse(path, filename=row["filename"], media_type="application/octet-stream")

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
        "rms_in_schedule": "0",
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
    return [dict(row) for row in db.backups()]


def _safe_upload(name: str) -> str:
    import re
    from pathlib import Path as P

    base = P(name).name or "backup.bin"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (cleaned or "backup.bin")[:80]
