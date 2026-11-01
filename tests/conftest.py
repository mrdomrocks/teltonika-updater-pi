import os

import pytest
from fastapi.testclient import TestClient

os.environ["RUTKEEPER_DISABLE_SCHEDULER"] = "1"
os.environ["RUTKEEPER_DEMO_DELAY"] = "0"

EMAIL = "tech@workshop.test"
PASSWORD = "correct horse battery"


def csrf_from(html: str) -> str:
    return html.split('name="csrf" value="', 1)[1].split('"', 1)[0]


def sign_in(client: TestClient, email: str = EMAIL, password: str = PASSWORD, **extra: str):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={"csrf": token, "email": email, "password": password, **extra},
        follow_redirects=False,
    )


def _app(tmp_path, monkeypatch, **options):
    monkeypatch.setenv("RUTKEEPER_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("RUTKEEPER_DEMO_DELAY", "0")
    from rutkeeper import auth
    from rutkeeper.app import create_app

    app = create_app(tmp_path, **options)
    app.state.db.add_user(EMAIL, "Tech", auth.hash_password(PASSWORD))
    return app


@pytest.fixture
def anon(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as test_client:
        yield test_client


@pytest.fixture
def client(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as test_client:
        assert sign_in(test_client).status_code == 303
        yield test_client
