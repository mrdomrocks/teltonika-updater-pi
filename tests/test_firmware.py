from datetime import datetime

from rutkeeper.firmware import (
    backup_compatibility,
    firmware_tuple,
    fota_body,
    parse_firmware,
    progress_phase,
    schedule_due,
    upgrade_body,
)


def test_newest_stable_means_current():
    report = parse_firmware(
        {"success": True, "data": {"version": "RUTX_R_00.07.17.3"}},
        {"success": True, "data": {"stable_version": "newest"}},
    )
    assert report.state == "current"
    assert report.current == "RUTX_R_00.07.17.3"
    assert report.available == ""


def test_device_version_means_update_waiting():
    report = parse_firmware(
        {"success": True, "data": {"version": "RUTX_R_00.07.10.2", "kernel_version": "5.10.224"}},
        {"success": True, "data": {"device": {"version": "RUTX_R_00.07.11.3", "size": "25266645"}}},
    )
    assert report.state == "available"
    assert report.available == "RUTX_R_00.07.11.3"
    assert report.size_bytes == 25266645


def test_same_offered_version_is_current():
    report = parse_firmware(
        {"data": {"version": "RUT2_R_00.07.15.4"}},
        {"data": {"device": {"version": "RUT2_R_00.07.15.4"}}},
    )
    assert report.state == "current"


def test_firmware_tuple_ignores_series_prefix():
    assert firmware_tuple("RUTX_R_00.07.10.2") < firmware_tuple("RUTX_R_00.07.10.10")
    assert firmware_tuple("RUT2_R_00.07.18") < firmware_tuple("RUT2_R_00.07.18.1")


def test_backup_rejects_different_product_code_and_newer_firmware():
    ok, reason = backup_compatibility("RUT2_R_00.07.15.4", "RUT2_R_00.07.15.4", "RUT241-A", "RUT241-B")
    assert not ok
    assert "product code" in reason.lower()

    ok, reason = backup_compatibility("RUT2_R_00.07.18.1", "RUT2_R_00.07.15.4", "RUT241-A", "RUT241-A")
    assert not ok
    assert "newer" in reason.lower()

    ok, reason = backup_compatibility("RUT2_R_00.07.14.0", "RUT2_R_00.07.15.4", "RUT241-A", "RUT241-A")
    assert ok


def test_schedule_is_first_monday_not_every_monday():
    first = datetime(2026, 10, 5, 4, 0)
    later = datetime(2026, 10, 12, 4, 0)
    assert schedule_due(first, "first_monday", 4, 0)
    assert not schedule_due(later, "first_monday", 4, 0)
    assert schedule_due(later, "mondays", 4, 0)
    assert not schedule_due(first, "manual", 4, 0)


def test_upgrade_body_matches_teltonika_recipe():
    assert upgrade_body(True) == {"data": {"suppress_validation": 1, "keep_settings": 1}}
    assert fota_body() == {"data": {"type": "stable"}}


def test_progress_phase():
    assert progress_phase({"data": {"percent": 40}}) == "downloading"
    assert progress_phase({"data": {"status": "downloaded"}}) == "ready"
    assert progress_phase({"_http_status": 404}) == "missing"
    assert progress_phase({"data": {"status": "failed"}}) == "error"

