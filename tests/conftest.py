import os

import pytest
from fastapi.testclient import TestClient

os.environ["RUTKEEPER_DISABLE_SCHEDULER"] = "1"
os.environ["RUTKEEPER_DEMO_DELAY"] = "0"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RUTKEEPER_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("RUTKEEPER_DEMO_DELAY", "0")
    from rutkeeper.app import create_app

    app = create_app(tmp_path)
    with TestClient(app) as test_client:
        yield test_client
