"""Run the TUA dashboard, or manage its accounts from the console."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from rutkeeper import auth
from rutkeeper.app import create_app, default_data_dir
from rutkeeper.db import DB


def _truthy(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def use_tls(host: str) -> bool:
    """HTTPS on the workbench port so phones can live-scan on the same URL."""
    flag = _truthy(os.environ.get("RUTKEEPER_TLS"))
    if flag is not None:
        return flag
    return host not in {"127.0.0.1", "localhost", "::1"}


def backend_port() -> int:
    return int(os.environ.get("RUTKEEPER_BACKEND_PORT", "18741"))


def serve() -> None:
    import uvicorn

    from rutkeeper.gateway import start_gateway
    from rutkeeper.tls import ensure_tls

    host = os.environ.get("RUTKEEPER_HOST", "127.0.0.1")
    port = int(os.environ.get("RUTKEEPER_PORT", "8741"))
    data = Path(os.environ.get("RUTKEEPER_DATA") or default_data_dir())
    tls = use_tls(host)
    app = create_app(data, secure_cookies=tls)
    display = "127.0.0.1" if host in {"0.0.0.0", "::"} else host

    if not tls:
        print(f"TUA listening on http://{display}:{port}", flush=True)
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    cert, key = ensure_tls(data)
    inner = backend_port()
    start_gateway(host, port, "127.0.0.1", inner, cert, key)
    print(f"TUA listening on https://{display}:{port}", flush=True)
    print("http:// on this port redirects to https:// (accept the certificate once).", flush=True)
    # App stays on loopback HTTP; the gateway terminates TLS on the public port.
    uvicorn.run(app, host="127.0.0.1", port=inner, log_level="info")


def _ask_password() -> str:
    while True:
        first = getpass.getpass("New password: ")
        problem = auth.password_problem(first)
        if problem:
            print(problem, file=sys.stderr)
            continue
        if getpass.getpass("Type it again: ") != first:
            print("Those did not match.", file=sys.stderr)
            continue
        return first


def _open_db() -> DB:
    data = Path(os.environ.get("RUTKEEPER_DATA") or default_data_dir())
    if os.name == "posix" and os.geteuid() == 0 and data.exists() and data.stat().st_uid != 0:
        # Root-owned SQLite journal files would stop the service from writing.
        raise SystemExit(f"Run this as the owner of {data}, for example: sudo tua-user ...")
    return DB(data / "rutkeeper.sqlite")


def users_command(args: argparse.Namespace) -> int:
    db = _open_db()
    try:
        if args.action == "list":
            rows = db.users()
            if not rows:
                print("No accounts yet.")
            for row in rows:
                print(f"{row['email']}\t{row['name']}\t{row['created_at']}")
            return 0

        email = auth.normalize_email(args.email or "")
        if not email:
            print("Give a valid --email.", file=sys.stderr)
            return 2
        account = db.user_by_email(email)

        if args.action == "add":
            if account:
                print(f"{email} already has an account. Use passwd to change its password.", file=sys.stderr)
                return 1
            db.add_user(email, (args.name or "").strip()[:80], auth.hash_password(_ask_password()))
            db.add_event("info", "account", f"Added account {email} from the console.")
            print(f"Added {email}.")
            return 0

        if not account:
            print(f"There is no account for {email}.", file=sys.stderr)
            return 1
        if args.action == "passwd":
            db.set_password(account["id"], auth.hash_password(_ask_password()))
            db.clear_login_failures(email)
            db.add_event("info", "account", f"Changed the password for {email} from the console.")
            print(f"Changed the password for {email}. Its open sessions are signed out.")
            return 0
        if args.action == "remove":
            db.delete_user(account["id"])
            db.add_event("info", "account", f"Removed account {email} from the console.")
            print(f"Removed {email}.")
            if not db.user_count():
                print("No accounts are left. Nobody can sign in until you add one.")
            return 0
    finally:
        db.close()
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rutkeeper")
    commands = parser.add_subparsers(dest="command")
    user = commands.add_parser("user", help="add, list, change, or remove dashboard accounts")
    actions = user.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="add an account; the password is asked for at the prompt")
    add.add_argument("--email", required=True)
    add.add_argument("--name", default="")
    passwd = actions.add_parser("passwd", help="set a new password and sign out that account's sessions")
    passwd.add_argument("--email", required=True)
    remove = actions.add_parser("remove", help="delete an account")
    remove.add_argument("--email", required=True)
    actions.add_parser("list", help="show every account")

    args = parser.parse_args(argv)
    if args.command == "user":
        return users_command(args)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
