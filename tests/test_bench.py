from pathlib import Path


def _csrf(page) -> str:
    marker = 'name="csrf" value="'
    start = page.text.index(marker) + len(marker)
    end = page.text.index('"', start)
    return page.text[start:end]


def test_bench_is_the_front_page(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Flash firmware" in page.text
    assert "Apply config 1" in page.text
    assert "Apply config 2" in page.text
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
    assert bench.sim.admin_password == "shared-secret"
    assert bench._session_password == ""

    raw = Path(client.app.state.db.path).read_bytes()
    assert b"label-password" not in raw
    assert b"from-the-file" not in raw
    assert b"shared-secret" not in raw
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "Admin password replaced" in messages
    assert "label-password" not in messages


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
