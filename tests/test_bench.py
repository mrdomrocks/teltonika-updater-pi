from pathlib import Path

from rutkeeper.bench import FIXED_ADMIN_PASSWORD


def _csrf(page) -> str:
    marker = 'name="csrf" value="'
    start = page.text.index(marker) + len(marker)
    end = page.text.index('"', start)
    return page.text[start:end]


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
    assert "Files" in page.text
    assert ">Backups<" not in page.text
    assert "Store firmware" not in page.text
    files = client.get("/backups")
    assert files.status_code == 200
    assert "Files" in files.text
    assert "Store firmware" in files.text
    assert "Store Config 1" in files.text
    assert "Store Config 2" in files.text
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
    client.post(
        "/bench/file",
        data={"csrf": token, "slot": "firmware"},
        files={"file": ("RUT2_R_00.07.18.bin", b"firmware-bytes", "application/octet-stream")},
    )
    client.post(
        "/bench/file",
        data={"csrf": token, "slot": "config1", "inside_password": "from-the-file"},
        files={"file": ("config-1.tar.gz", b"config-bytes", "application/octet-stream")},
    )
    bench = client.app.state.bench
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
    client.post(
        "/bench/file",
        data={"csrf": token, "slot": "firmware"},
        files={"file": ("RUT2_R_00.07.18.bin", b"firmware-bytes", "application/octet-stream")},
    )
    client.post(
        "/bench/file",
        data={"csrf": token, "slot": "config1", "inside_password": "from-the-file"},
        files={"file": ("config-1.tar.gz", b"config-bytes", "application/octet-stream")},
    )
    bench = client.app.state.bench
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
    client.post(
        "/bench/file",
        data={"csrf": token, "slot": "firmware"},
        files={"file": ("rut.bin", b"firmware-bytes", "application/octet-stream")},
    )
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


def test_flash_without_a_password_is_refused(client):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={"csrf": token, "host": "192.168.1.1", "username": "admin", "simulate": "1"},
    )
    client.post(
        "/bench/file",
        data={"csrf": token, "slot": "firmware"},
        files={"file": ("rut.bin", b"firmware-bytes", "application/octet-stream")},
    )
    client.app.state.bench.run_inline("firmware", "")
    assert client.app.state.bench.sim.firmware == ""
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "password" in messages.lower()
