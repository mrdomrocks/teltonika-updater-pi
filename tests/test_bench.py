import os
from pathlib import Path

from rutkeeper.bench import FIXED_ADMIN_PASSWORD


def _drop(client, folder: str, name: str, content: bytes) -> None:
    path = client.app.state.bench.directory / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(content)


def _csrf(page) -> str:
    marker = 'name="csrf" value="'
    start = page.text.index(marker) + len(marker)
    end = page.text.index('"', start)
    return page.text[start:end]


def test_access_token_folder_is_not_created(client):
    root = client.app.state.bench.directory
    assert not (root / "Access Token").exists()
    assert not (root / "Access Token.txt").exists()
    page = client.get("/")
    assert "Access token" not in page.text
    assert "Access Token" not in page.text
    assert 'data-start-on-lan="1"' in page.text


def test_newest_file_in_a_folder_is_used(client):
    _drop(client, "Firmware", "old.bin", b"old")
    newer = client.app.state.bench.directory / "Firmware" / "new.bin"
    newer.write_bytes(b"new")
    os.utime(client.app.state.bench.directory / "Firmware" / "old.bin", (1, 1))
    os.utime(newer, (2, 2))
    status = client.get("/bench/status").json()
    assert status["firmware_name"] == "new.bin"
    assert status["firmware_ready"] is True
    assert status["config1_ready"] is False


def test_bench_is_the_front_page(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Install" in page.text
    assert "Config 1" in page.text
    assert "Config 2" in page.text
    assert "Sign in" in page.text
    assert "Set the admin password" in page.text
    assert "Connect at the new address" in page.text
    assert "Address after firmware" in page.text
    assert "Scan the label" in page.text
    assert 'id="label-photo"' in page.text
    assert 'name="label-photo"' not in page.text
    assert 'href="/files"' not in page.text
    assert "Firmware folder" in page.text
    assert client.get("/files").status_code == 404
    assert "North gate" not in page.text
    status = client.get("/bench/status").json()
    assert status["host"] == "192.168.1.1"
    assert status["http"] is False


def test_config_replaces_the_router_password_with_the_shared_one(client):
    page = client.get("/")
    token = _csrf(page)
    saved = client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.1.1",
            "username": "admin",
            "target_password": "shared-secret",
            "simulate": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    _drop(client, "Firmware", "RUT2_R_00.07.18.bin", b"firmware-bytes")
    _drop(client, "Config 1", "config-1.tar.gz", b"config-bytes")
    bench = client.app.state.bench
    bench.save_config_password("config1", "from-the-file")
    bench.run_inline("firmware", "label-password")
    assert bench.sim.firmware == "RUT2_R_00.07.18.bin"
    assert bench.sim.admin_password == "label-password"

    bench.run_inline("config1", "")
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.sim.admin_password == FIXED_ADMIN_PASSWORD
    assert bench._session_password == ""

    raw = Path(client.app.state.db.path).read_bytes()
    assert b"label-password" not in raw
    assert b"from-the-file" not in raw
    assert FIXED_ADMIN_PASSWORD.encode() not in raw
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "Admin password replaced" in messages
    assert "label-password" not in messages


def test_install_writes_firmware_then_config_and_the_shared_password(client):
    page = client.get("/")
    token = _csrf(page)
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.1.1",
            "username": "admin",
            "target_password": "shared-secret",
            "simulate": "1",
        },
    )
    _drop(client, "Firmware", "RUT2_R_00.07.18.bin", b"firmware-bytes")
    _drop(client, "Config 1", "config-1.tar.gz", b"config-bytes")
    bench = client.app.state.bench
    bench.save_config_password("config1", "from-the-file")
    bench.db.set_settings({"bench_next_host": "10.20.30.40"})
    bench.run_inline("install", "label-password", "config1")
    assert bench.sim.actions == [
        ("password", FIXED_ADMIN_PASSWORD),
        ("firmware", FIXED_ADMIN_PASSWORD),
        ("config", FIXED_ADMIN_PASSWORD),
        ("password", FIXED_ADMIN_PASSWORD),
    ]
    assert bench.sim.firmware == "RUT2_R_00.07.18.bin"
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.sim.admin_password == FIXED_ADMIN_PASSWORD
    assert bench.db.setting("bench_host") == "10.20.30.40"
    assert bench._session_password == ""
    assert bench.status["step"] == "done"
    assert bench.status["phase"] == "ready"

    raw = Path(client.app.state.db.path).read_bytes()
    assert b"label-password" not in raw
    assert b"from-the-file" not in raw
    assert FIXED_ADMIN_PASSWORD.encode() not in raw
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "Admin password replaced" in messages
    assert "label-password" not in messages


def test_install_without_the_config_file_does_not_flash(client):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.1.1",
            "username": "admin",
            "target_password": "shared-secret",
            "simulate": "1",
        },
    )
    _drop(client, "Firmware", "rut.bin", b"firmware-bytes")
    bench = client.app.state.bench
    bench.run_inline("install", "label-password", "config1")
    assert bench.sim.firmware == ""
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "config" in messages.lower()


def test_install_form_requires_a_config_choice(client):
    token = _csrf(client.get("/"))
    page = client.post(
        "/bench/run",
        data={"csrf": token, "action": "install", "current_password": "label-password"},
        follow_redirects=True,
    )
    assert page.status_code == 200
    assert "Select Config 1 or Config 2." in page.text
    assert client.app.state.bench.status["phase"] != "busy"


def test_install_waits_until_the_router_answers_on_the_lan(client, monkeypatch):
    monkeypatch.setattr("rutkeeper.bench.lan_ready", lambda host: False)
    token = _csrf(client.get("/"))
    _drop(client, "Firmware", "rut.bin", b"firmware-bytes")
    _drop(client, "Config 1", "config.tar.gz", b"config-bytes")
    page = client.post(
        "/bench/run",
        data={
            "csrf": token,
            "action": "install",
            "config": "config1",
            "current_password": "label-password",
        },
        follow_redirects=True,
    )
    assert page.status_code == 200
    assert "answers on the LAN" in page.text
    assert client.app.state.bench.status["phase"] != "busy"
    assert client.app.state.bench.sim.firmware == ""


def test_check_is_not_taken_when_the_router_is_not_on_the_lan(client, monkeypatch):
    monkeypatch.setattr("rutkeeper.worker.lan_ready", lambda host: False)
    device_id = client.app.state.db.insert_device(
        {"name": "Cable", "host": "192.168.1.1", "username": "admin", "is_demo": 0}
    )
    client.app.state.runner.run_inline(device_id, "check")
    fresh = client.app.state.db.device(device_id)
    assert fresh["last_seen"] is None
    messages = " ".join(event["message"] for event in client.app.state.db.events(device_id=device_id))
    assert "not plugged in on the LAN" in messages
    assert "This action was not started." in messages


def test_flash_without_a_password_is_refused(client):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={"csrf": token, "host": "192.168.1.1", "username": "admin", "simulate": "1"},
    )
    _drop(client, "Firmware", "rut.bin", b"firmware-bytes")
    client.app.state.bench.run_inline("firmware", "")
    assert client.app.state.bench.sim.firmware == ""
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "password" in messages.lower()
