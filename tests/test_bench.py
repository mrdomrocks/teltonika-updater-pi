import os
import threading
import time
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


def _arm_rms(client) -> tuple[str, str]:
    bench = client.app.state.bench
    api = bench.directory / "api"
    api.mkdir(parents=True, exist_ok=True)
    (api / "rms.txt").write_text("rms-test-token\n", encoding="utf-8")
    bench.db.set_settings({"rms_company_id": "4242"})
    return "1234567890", "AABBCCDDEEFF"


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
    assert 'id="login-button"' in page.text
    assert 'id="config-button"' in page.text
    assert "Apply config" in page.text
    assert "Set the admin password" in page.text
    assert "Confirm at the new address" in page.text
    assert "Address after config" in page.text
    assert "Scan the label" in page.text
    assert 'id="scan-overlay"' in page.text
    assert 'id="scan-video"' in page.text
    assert 'id="label-photo"' not in page.text
    assert "192.168.1.1" in page.text
    assert "192.168.100.1" in page.text
    assert "8742" not in page.text
    assert 'href="/files"' not in page.text
    assert "Firmware folder" in page.text
    assert client.get("/files").status_code == 404
    assert "North gate" not in page.text
    status = client.get("/bench/status").json()
    assert status["host"] == "192.168.1.1"
    assert status["next_host"] == "192.168.100.1"
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

    assert bench.db.setting("bench_host") == "192.168.1.1"
    serial, mac = _arm_rms(client)
    bench.run_inline("config1", "", serial=serial, mac=mac)
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.sim.admin_password == FIXED_ADMIN_PASSWORD
    assert bench._session_password == ""
    assert bench.db.setting("bench_host") == "192.168.100.1"

    raw = Path(client.app.state.db.path).read_bytes()
    assert b"label-password" not in raw
    assert b"from-the-file" not in raw
    assert FIXED_ADMIN_PASSWORD.encode() not in raw
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "Admin password replaced" in messages
    assert "Upload confirmed" in messages
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
    assert bench.db.setting("bench_host") == "192.168.1.1"
    serial, mac = _arm_rms(client)
    bench.run_inline("install", "label-password", "config1", serial, mac)
    assert bench.sim.actions == [
        ("password", FIXED_ADMIN_PASSWORD),
        ("firmware", FIXED_ADMIN_PASSWORD),
        ("config", FIXED_ADMIN_PASSWORD),
        ("password", FIXED_ADMIN_PASSWORD),
        ("wireless", "AQC Q1/1"),
        ("rms", "AQC Q1/1"),
    ]
    assert bench.sim.firmware == "RUT2_R_00.07.18.bin"
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.sim.admin_password == FIXED_ADMIN_PASSWORD
    assert bench.sim.wireless_key == FIXED_ADMIN_PASSWORD
    # Address stays on the cable IP until config upload; then next_host confirms it.
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
    assert "Upload confirmed" in messages
    assert "label-password" not in messages
    log = (Path(client.app.state.db.path).parent / "configured-devices.txt").read_text(encoding="utf-8")
    assert "AQC Q1/1,1234567890,AA:BB:CC:DD:EE:FF" in log
    assert FIXED_ADMIN_PASSWORD not in log
    assert "rms-test-token" not in log
    assert "rms-test-token" not in messages


def test_config2_uses_its_own_q2_name(client):
    page = client.get("/")
    client.post(
        "/bench/setup",
        data={
            "csrf": _csrf(page),
            "host": "192.168.1.1",
            "username": "admin",
            "simulate": "1",
        },
    )
    _drop(client, "Firmware", "RUT2_R_00.07.18.bin", b"firmware-bytes")
    _drop(client, "Config 2", "config-2.tar.gz", b"config-bytes")
    bench = client.app.state.bench
    serial, mac = _arm_rms(client)
    bench.run_inline("install", "label-password", "config2", serial, mac)
    assert ("wireless", "AQC Q2/1") in bench.sim.actions
    assert ("rms", "AQC Q2/1") in bench.sim.actions
    log = (Path(client.app.state.db.path).parent / "configured-devices.txt").read_text(encoding="utf-8")
    assert "1,AQC Q2/1,1234567890,AA:BB:CC:DD:EE:FF" in log


def test_install_confirms_config_on_default_next_host(client):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.1.1",
            "username": "admin",
            "simulate": "1",
        },
    )
    _drop(client, "Firmware", "RUT2_R_00.07.18.bin", b"firmware-bytes")
    _drop(client, "Config 1", "config-1.tar.gz", b"config-bytes")
    bench = client.app.state.bench
    assert bench.db.setting("bench_next_host") == "192.168.100.1"
    serial, mac = _arm_rms(client)
    bench.run_inline("install", "label-password", "config1", serial, mac)
    assert bench.sim.firmware == "RUT2_R_00.07.18.bin"
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.db.setting("bench_host") == "192.168.100.1"
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "192.168.100.1" in messages
    assert "Upload confirmed" in messages


def test_install_resets_leftover_post_config_host_before_config(client):
    """A finished unit left Address now at 192.168.100.1; the next unit is still on cable."""
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.100.1",
            "next_host": "192.168.100.1",
            "username": "admin",
            "simulate": "1",
        },
    )
    _drop(client, "Firmware", "RUT2_R_00.07.18.bin", b"firmware-bytes")
    _drop(client, "Config 1", "config-1.tar.gz", b"config-bytes")
    bench = client.app.state.bench
    assert bench.db.setting("bench_host") == "192.168.100.1"
    hosts_during: list[str] = []

    original_upload = bench.sim.upload_backup

    def track_upload(filename: str, content: bytes) -> None:
        hosts_during.append(bench.db.setting("bench_host") or "")
        original_upload(filename, content)

    bench.sim.upload_backup = track_upload  # type: ignore[method-assign]
    serial, mac = _arm_rms(client)
    bench.run_inline("install", "label-password", "config1", serial, mac)
    assert hosts_during == ["192.168.1.1"]
    assert bench.sim.firmware == "RUT2_R_00.07.18.bin"
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.db.setting("bench_host") == "192.168.100.1"
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "192.168.1.1" in messages
    assert "Upload confirmed" in messages


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


def test_manual_sign_in_recovers_after_an_interrupted_install(client):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.100.1",
            "next_host": "192.168.100.1",
            "username": "admin",
            "simulate": "1",
        },
    )
    bench = client.app.state.bench
    bench.sim.admin_password = FIXED_ADMIN_PASSWORD
    bench.run_inline("login", "")
    assert bench.status["phase"] == "ready"
    assert bench.status["step"] == "signed-in"
    assert bench.status["http"] is True
    assert bench._session_password == FIXED_ADMIN_PASSWORD
    assert "Signed in" in str(bench.status["detail"])
    messages = " ".join(event["message"] for event in client.app.state.db.events())
    assert "Signed in" in messages


def test_manual_sign_in_works_when_lan_probe_is_down(client, monkeypatch):
    monkeypatch.setattr("rutkeeper.bench.lan_ready", lambda host: False)
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={"csrf": token, "host": "192.168.1.1", "username": "admin", "simulate": "1"},
    )
    bench = client.app.state.bench
    # Sign in skips the LAN gate so an interrupted unit can still be recovered.
    bench.run_inline("login", "label-password")
    assert bench.status["phase"] == "ready"
    assert bench.status["step"] == "signed-in"
    assert bench._session_password == "label-password"


def test_apply_config_resumes_after_firmware(client):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.1.1",
            "username": "admin",
            "simulate": "1",
        },
    )
    _drop(client, "Config 1", "config-1.tar.gz", b"config-bytes")
    bench = client.app.state.bench
    bench.sim.admin_password = FIXED_ADMIN_PASSWORD
    serial, mac = _arm_rms(client)
    page = client.post(
        "/bench/run",
        data={
            "csrf": token,
            "action": "config",
            "config": "config1",
            "current_password": FIXED_ADMIN_PASSWORD,
            "device_serial": serial,
            "device_mac": mac,
        },
        follow_redirects=True,
    )
    assert page.status_code == 200
    for _ in range(80):
        if bench.status.get("step") == "done" and bench.sim.config:
            break
        time.sleep(0.05)
    assert bench.sim.config == "config-1.tar.gz"
    assert bench.db.setting("bench_host") == "192.168.100.1"
    assert bench.status["step"] == "done"


def test_probe_keeps_a_successful_sign_in_off_the_wait_step(client, monkeypatch):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={"csrf": token, "host": "192.168.1.1", "username": "admin", "simulate": "1"},
    )
    bench = client.app.state.bench
    bench.run_inline("login", "label-password")
    bench.db.set_settings({"bench_simulate": "0"})
    monkeypatch.setattr("rutkeeper.bench.probe", lambda host: (True, True, 4))
    bench._probe_once()
    assert bench.status["step"] == "signed-in"
    assert bench.status["phase"] == "ready"
    assert "Signed in" in str(bench.status["detail"])
    assert "Wait for the router" not in str(bench.status["detail"])


def test_probe_updates_address_when_router_answers_elsewhere(client, monkeypatch):
    token = _csrf(client.get("/"))
    client.post(
        "/bench/setup",
        data={
            "csrf": token,
            "host": "192.168.100.1",
            "next_host": "192.168.100.1",
            "username": "admin",
        },
    )
    bench = client.app.state.bench

    def fake_probe(host):
        if host == "192.168.1.1":
            return True, True, 2
        return False, False, None

    monkeypatch.setattr("rutkeeper.bench.probe", fake_probe)
    bench._probe_once()
    assert bench.db.setting("bench_host") == "192.168.1.1"
    assert bench.status["http"] is True
    assert "192.168.1.1" in str(bench.status["detail"])


def test_reconnect_still_tries_the_label_password(client):
    bench = client.app.state.bench
    bench._factory_password = "from-the-label"
    bench._session_password = FIXED_ADMIN_PASSWORD
    assert bench._candidates(FIXED_ADMIN_PASSWORD) == [FIXED_ADMIN_PASSWORD, "from-the-label"]


def test_new_address_wait_stops_when_the_factory_address_answers(client, monkeypatch):
    monkeypatch.setattr("rutkeeper.bench.CABLE_AFTER_CONFIG_SECONDS", 0)
    bench = client.app.state.bench
    bench.db.set_settings({"bench_simulate": "0", "bench_next_host": "192.168.100.1"})

    def fake_probe(host):
        if host == "192.168.1.1":
            return True, True, 1
        return False, False, None

    monkeypatch.setattr("rutkeeper.bench.probe", fake_probe)
    try:
        bench._await_new_address()
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("the wait should stop")
    assert "192.168.1.1" in message
    assert "label password" in message


def test_sign_in_can_stop_the_wait(client, monkeypatch):
    bench = client.app.state.bench
    bench.db.set_settings({"bench_simulate": "0", "bench_next_host": "192.168.100.1"})
    monkeypatch.setattr("rutkeeper.bench.probe", lambda host: (False, False, None))
    monkeypatch.setattr("rutkeeper.bench.CABLE_AFTER_CONFIG_SECONDS", 3600)
    seen: list[str] = []

    def runner():
        try:
            bench._await_new_address()
        except Exception as exc:
            seen.append(str(exc))

    thread = threading.Thread(target=runner)
    bench._job_thread = thread
    thread.start()
    for _ in range(50):
        if bench.status.get("phase") == "await-address":
            break
        time.sleep(0.02)
    bench._cancel.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert seen
    assert "Sign in" in seen[0]
