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


def test_rms_page_is_gone(client):
    assert client.get("/rms").status_code == 404
    home = client.get("/")
    assert "RMS" not in home.text
    assert 'href="/rms"' not in home.text
    log = client.get("/log")
    assert "RMS" not in log.text
