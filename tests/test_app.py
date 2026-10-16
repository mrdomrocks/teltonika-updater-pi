from datetime import datetime

from rutkeeper.firmware import backup_compatibility


def _csrf(page) -> str:
    marker = 'name="csrf" value="'
    start = page.text.index(marker) + len(marker)
    end = page.text.index('"', start)
    return page.text[start:end]


def test_fleet_shows_simulated_routers(client):
    page = client.get("/fleet")
    assert page.status_code == 200
    assert "North gate" in page.text
    assert "Yard pump" in page.text
    assert "Cold store" in page.text
    assert "Update waiting" in page.text
    assert "On stable" in page.text
    assert "No answer" in page.text


def test_health(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["devices"] == 3


def test_check_and_backup_demo_router(client):
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
    now = datetime(2026, 10, 5, 4, 0)
    client.app.state.runner.tick(now)
    yard = next(device for device in client.app.state.db.devices() if device["name"] == "Yard pump")
    assert yard["firmware"] == "RUTX_R_00.07.17.3"
    assert yard["update_state"] == "available"
    messages = [event["message"] for event in client.app.state.db.events()]
    assert any("was not installed" in message for message in messages)


def test_armed_schedule_installs_only_when_auto_apply_is_on(client):
    yard = next(device for device in client.app.state.db.devices() if device["name"] == "Yard pump")
    client.app.state.db.update_device(yard["id"], {"auto_apply": 1})
    client.app.state.db.set_settings({"armed": "1"})
    client.app.state.runner.tick(datetime(2026, 10, 5, 4, 0))
    fresh = client.app.state.db.device(yard["id"])
    assert fresh["firmware"] == "RUTX_R_00.07.18.1"
    assert fresh["update_state"] == "current"


def test_add_router_requires_password_and_csrf(client):
    page = client.get("/devices/new")
    token = _csrf(page)
    rejected = client.post("/devices", data={"name": "Shed", "host": "10.0.0.8", "username": "admin"})
    assert rejected.status_code == 403

    missing = client.post(
        "/devices",
        data={"csrf": token, "name": "Shed", "host": "10.0.0.8", "username": "admin"},
    )
    assert missing.status_code == 400
    assert "password" in missing.text.lower()

    saved = client.post(
        "/devices",
        data={
            "csrf": token,
            "name": "Shed",
            "host": "10.0.0.8",
            "username": "admin",
            "password": "s3cret-router",
            "keep_settings": "1",
            "schedule_enabled": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    stored = next(device for device in client.app.state.db.devices() if device["name"] == "Shed")
    assert stored["host"] == "https://10.0.0.8"
    assert stored["password_enc"]
    assert client.app.state.secrets.decrypt(stored["password_enc"]) == "s3cret-router"
    assert "s3cret-router" not in client.get(f"/devices/{stored['id']}").text


def test_config_push_is_blocked_when_product_codes_differ(client):
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


def test_manual_update_from_the_form_requires_confirmation(client):
    page = client.get("/fleet")
    token = _csrf(page)
    yard = next(device for device in client.app.state.db.devices() if device["name"] == "Yard pump")
    response = client.post(f"/devices/{yard['id']}/update", data={"csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/devices/{yard['id']}")
    assert client.app.state.db.device(yard["id"])["firmware"] == "RUTX_R_00.07.17.3"


def test_schedule_page_names_the_teltonika_sequence(client):
    page = client.get("/schedule")
    assert page.status_code == 200
    assert "/firmware/actions/fota_download" in page.text
    assert "keep_settings" in page.text
    assert "First Monday" in page.text


def test_rms_preview_matches_the_support_script(client):
    page = client.get("/rms")
    token = _csrf(page)
    saved = client.post(
        "/rms",
        data={
            "csrf": token,
            "action": "save",
            "base": "https://api.rms.teltonika-networks.com",
            "group_id": "task_group_id",
            "device_ids": "2006385",
            "task_ids": "11, 12, 13",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "device_id" in saved.text
    assert "2006385" in saved.text
    assert "Bearer [redacted]" in saved.text
