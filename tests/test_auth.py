import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from conftest import EMAIL, PASSWORD, _app, csrf_from, sign_in
from rutkeeper import auth
from rutkeeper import __main__ as cli

OPEN = {"/login"}


def test_password_hash_round_trip():
    stored = auth.hash_password("a long enough password")
    assert stored.startswith("scrypt$")
    assert auth.verify_password("a long enough password", stored)
    assert not auth.verify_password("a long enough passwore", stored)
    assert not auth.verify_password("anything", "not-a-hash")
    assert auth.hash_password("same") != auth.hash_password("same")


def test_every_route_is_closed_when_signed_out(anon):
    routes = [route for route in anon.app.routes if isinstance(route, APIRoute) and route.path not in OPEN]
    assert routes
    for route in routes:
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        for method in route.methods:
            json_reply = anon.request(method, path, follow_redirects=False)
            assert json_reply.status_code == 401, f"{method} {route.path} answered signed out"
            page = anon.request(method, path, headers={"Accept": "text/html"}, follow_redirects=False)
            assert page.status_code == 303, f"{method} {route.path} answered signed out"
            assert page.headers["location"].startswith("/login")


def test_unknown_paths_do_not_reveal_anything_signed_out(anon):
    assert anon.get("/nope").status_code == 401


def test_static_files_and_login_page_stay_open(anon):
    assert anon.get("/static/app.css").status_code == 200
    page = anon.get("/login")
    assert page.status_code == 200
    assert 'name="password"' in page.text
    assert 'href="/rms"' not in page.text


def test_login_page_explains_how_to_add_the_first_account(tmp_path, monkeypatch):
    monkeypatch.setenv("RUTKEEPER_DISABLE_SCHEDULER", "1")
    from rutkeeper.app import create_app

    with TestClient(create_app(tmp_path)) as fresh:
        page = fresh.get("/login")
        assert "tua-user add" in page.text
        assert 'name="password"' not in page.text


def test_redirect_remembers_the_page(anon):
    reply = anon.get("/log", headers={"Accept": "text/html"}, follow_redirects=False)
    assert reply.headers["location"] == "/login?next=%2Flog"
    signed = sign_in(anon, next="/log")
    assert signed.headers["location"] == "/log"


@pytest.mark.parametrize("target", ["//evil.example/", "https://evil.example/", "/\\evil.example", "/logout"])
def test_next_cannot_leave_the_site(anon, target):
    assert sign_in(anon, next=target).headers["location"] == "/"


def test_sign_in_sets_a_locked_down_cookie(anon):
    reply = sign_in(anon)
    assert reply.status_code == 303
    cookie = reply.headers["set-cookie"]
    assert auth.SESSION_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie
    assert anon.get("/log").status_code == 200


def test_secure_cookie_when_serving_https(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, secure_cookies=True), base_url="https://testserver") as https:
        reply = sign_in(https)
        session = [value for key, value in reply.headers.multi_items() if key == "set-cookie" and auth.SESSION_COOKIE in value]
        assert session and "Secure" in session[0]


def test_wrong_password_and_unknown_email_look_the_same(anon):
    wrong = sign_in(anon, password="not the password")
    unknown = sign_in(anon, email="nobody@workshop.test")
    assert wrong.status_code == unknown.status_code == 400
    assert "do not match" in wrong.text and "do not match" in unknown.text
    assert auth.SESSION_COOKIE not in wrong.headers.get("set-cookie", "")
    assert anon.get("/log").status_code == 401


def test_login_needs_the_form_token(anon):
    reply = anon.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False)
    assert reply.status_code == 403


def test_eight_wrong_passwords_lock_the_email(anon):
    for _ in range(auth.MAX_FAILURES):
        assert sign_in(anon, password="wrong password!").status_code == 400
    locked = sign_in(anon)
    assert locked.status_code == 429
    assert "15 minutes" in locked.text
    assert anon.get("/log").status_code == 401


def test_sign_out_ends_the_session(client):
    token = client.cookies.get(auth.SESSION_COOKIE)
    page = client.get("/log")
    assert "Sign out" in page.text
    reply = client.post("/logout", data={"csrf": csrf_from(page.text)}, follow_redirects=False)
    assert reply.headers["location"] == "/login"
    client.cookies.set(auth.SESSION_COOKIE, token)
    assert client.get("/log").status_code == 401


def test_expired_session_is_refused(client):
    client.app.state.db.conn.execute("UPDATE sessions SET expires_at = 0")
    client.app.state.db.conn.commit()
    assert client.get("/log").status_code == 401


def test_console_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUTKEEPER_DATA", str(tmp_path))
    answers = iter(["short", "first password here", "first password here", "second password here", "second password here"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))

    assert cli.main(["user", "add", "--email", "Sam@Workshop.test", "--name", "Sam"]) == 0
    assert cli.main(["user", "add", "--email", "sam@workshop.test"]) == 1
    assert cli.main(["user", "list"]) == 0
    assert "sam@workshop.test\tSam" in capsys.readouterr().out

    from rutkeeper.db import DB

    db = DB(tmp_path / "rutkeeper.sqlite")
    account = db.user_by_email("sam@workshop.test")
    assert auth.verify_password("first password here", account["password_hash"])
    db.add_session("abc", account["id"], 2**40)
    db.close()

    assert cli.main(["user", "passwd", "--email", "sam@workshop.test"]) == 0
    db = DB(tmp_path / "rutkeeper.sqlite")
    account = db.user_by_email("sam@workshop.test")
    assert auth.verify_password("second password here", account["password_hash"])
    assert db.session_user("abc", 0) is None
    db.close()

    assert cli.main(["user", "remove", "--email", "sam@workshop.test"]) == 0
    assert "No accounts are left" in capsys.readouterr().out
    assert cli.main(["user", "remove", "--email", "sam@workshop.test"]) == 1
    assert cli.main(["user", "add", "--email", "not-an-email"]) == 2
