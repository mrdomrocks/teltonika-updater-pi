from datetime import datetime

from rutkeeper.firmware import backup_compatibility


def test_fleet_is_gone(client):
    page = client.get("/fleet")
    assert page.status_code == 404
    home = client.get("/")
    assert "Fleet" not in home.text
    assert "North gate" not in home.text


def test_health(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["devices"] == 0


def test_check_and_backup_demo_router(client):
    client.app.state.db.seed_demos()
    devices = client.app.state.db.devices()
    gate = next(device for device in devices if device["name"] == "North gate")
    client.app.state.runner.run_inline(gate["id"], "check")
    fresh = client.app.state.db.device(gate["id"])
    assert fresh["update_state"] == "current"
    assert fresh["firmware"] == "RUT2_R_00.07.15.4"

    client.app.state.runner.run_inline(gate["id"], "backup")
    backups = client.app.state.db.backups()
    assert backups
    assert backups[0]["product_code"] == "RUT241-DEMO"
    assert (client.app.state.runner.backup_dir / backups[0]["stored_name"]).exists()


def test_disarmed_schedule_does_not_install(client):
    client.app.state.db.seed_demos()
    now = datetime(2026, 10, 5, 4, 0)
    client.app.state.runner.tick(now)
    yard = next(device for device in client.app.state.db.devices() if device["name"] == "Yard pump")
    assert yard["firmware"] == "RUTX_R_00.07.17.3"
    assert yard["update_state"] == "available"
    messages = [event["message"] for event in client.app.state.db.events()]
    assert any("was not installed" in message for message in messages)


def test_armed_schedule_installs_only_when_auto_apply_is_on(client):
    client.app.state.db.seed_demos()
    yard = next(device for device in client.app.state.db.devices() if device["name"] == "Yard pump")
    client.app.state.db.update_device(yard["id"], {"auto_apply": 1})
    client.app.state.db.set_settings({"armed": "1"})
    client.app.state.runner.tick(datetime(2026, 10, 5, 4, 0))
    fresh = client.app.state.db.device(yard["id"])
    assert fresh["firmware"] == "RUTX_R_00.07.18.1"
    assert fresh["update_state"] == "current"


def test_config_push_is_blocked_when_product_codes_differ(client):
    client.app.state.db.seed_demos()
    yard = next(device for device in client.app.state.db.devices() if device["name"] == "Yard pump")
    gate = next(device for device in client.app.state.db.devices() if device["name"] == "North gate")
    client.app.state.runner.run_inline(gate["id"], "backup")
    backup = client.app.state.db.backups()[0]
    client.app.state.db.update_backup(backup["id"], {"is_profile": 1, "profile_name": "Gate standard"})
    ok, _reason = backup_compatibility(
        backup["firmware"],
        yard["firmware"],
        backup["product_code"],
        yard["product_code"],
    )
    assert not ok
    try:
        client.app.state.runner.run_inline(yard["id"], "push", backup_id=backup["id"])
    except Exception as exc:  # run_inline swallows into the log
        raise AssertionError(str(exc)) from exc
    events = client.app.state.db.events(device_id=yard["id"])
    assert any(event["level"] == "error" and "product code" in event["message"].lower() for event in events)


def test_schedule_page_is_gone(client):
    page = client.get("/schedule")
    assert page.status_code == 404
    assert "Schedule" not in client.get("/").text


def test_rms_page_records_an_existing_device(client):
    from pathlib import Path

    page = client.get("/rms")
    assert page.status_code == 200
    assert "Record device" in page.text
    assert "AQC Q1/1" in page.text
    assert "AQC Q2/1" in page.text
    marker = 'name="csrf" value="'
    token = page.text.split(marker, 1)[1].split('"', 1)[0]
    saved = client.post(
        "/rms/devices",
        data={"csrf": token, "serial": "1234567890", "mac": "aa-bb-cc-dd-ee-ff"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "AQC%20Q1/1" in saved.headers["location"] or "AQC Q1/1" in saved.headers["location"]
    log = Path(client.app.state.db.path).parent / "configured-devices.txt"
    text = log.read_text(encoding="utf-8")
    assert "1,AQC Q1/1,1234567890,AA:BB:CC:DD:EE:FF" in text
    again = client.post(
        "/rms/devices",
        data={"csrf": token, "serial": "1234567890", "mac": "AABBCCDDEEFF"},
        follow_redirects=True,
    )
    assert "already in the configured devices log" in again.text
    assert text.count("1234567890") == log.read_text(encoding="utf-8").count("1234567890")
    shown = client.get("/rms")
    assert "1234567890" in shown.text
    assert "AA:BB:CC:DD:EE:FF" in shown.text
    assert "AQC Q1/2" in shown.text
    assert "AQC Q2/1" in shown.text
    q2 = client.post(
        "/rms/devices",
        data={"csrf": token, "config": "config2", "serial": "9988776655", "mac": "AABBCCDDEE11"},
        follow_redirects=True,
    )
    assert "AQC Q2/1" in q2.text
    log_text = log.read_text(encoding="utf-8")
    assert "1,AQC Q2/1,9988776655,AA:BB:CC:DD:EE:11" in log_text


def test_rms_page_keeps_the_token_off_the_page(client):
    root = client.app.state.bench.directory
    (root / "api").mkdir(parents=True, exist_ok=True)
    (root / "api" / "rms.txt").write_text("secret-token\n", encoding="utf-8")
    page = client.get("/rms")
    assert page.status_code == 200
    assert "rms.txt" in page.text
    assert "Token file found." in page.text
    assert "secret-token" not in page.text
    home = client.get("/")
    assert 'href="/rms"' in home.text
    log = client.get("/log")
    assert 'href="/rms"' in log.text
