import json

import httpx

from rutkeeper.rms import add_rut, format_mac, name_for, read_access_token, remember_device
from rutkeeper.rutos import RutosError


def test_token_file_uses_the_first_line(tmp_path):
    folder = tmp_path / "api"
    folder.mkdir()
    (folder / "rms.txt").write_text("\ufeff\n# note\n  line-token  \n", encoding="utf-8")
    assert read_access_token(tmp_path) == "line-token"


def test_missing_token_file_names_the_folder(tmp_path):
    try:
        read_access_token(tmp_path)
    except RutosError as exc:
        assert "api" in str(exc)
        assert "rms.txt" in str(exc)
    else:
        raise AssertionError("missing token file should fail")


def test_next_name_increments_and_a_serial_keeps_its_number(tmp_path):
    log = tmp_path / "configured-devices.txt"
    number, name, known = name_for(log, "111")
    assert (number, name, known) == (1, "AQC Q1/1", False)
    remember_device(log, number, name, "111", "AA:BB:CC:DD:EE:FF")
    number, name, known = name_for(log, "222")
    assert (number, name, known) == (2, "AQC Q1/2", False)
    remember_device(log, number, name, "222", "11:22:33:44:55:66")
    again, again_name, known = name_for(log, "111")
    assert (again, again_name, known) == (1, "AQC Q1/1", True)


def test_mac_is_grouped_for_rms():
    assert format_mac("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"
    assert format_mac("AA-BB-CC-DD-EE-FF") == "AA:BB:CC:DD:EE:FF"


def test_add_rut_sends_serial_mac_name_and_password():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": []})

    add_rut(
        token="rms-token",
        company_id="4242",
        name="AQC Q1/3",
        password="Enter@qua1!",
        serial="1234567890",
        mac="aabbccddeeff",
        transport=httpx.MockTransport(handler),
    )
    assert seen["url"] == "https://api.rms.teltonika-networks.com/devices"
    assert seen["auth"] == "Bearer rms-token"
    row = seen["body"]["data"][0]
    assert row["company_id"] == 4242
    assert row["device_series"] == "rut"
    assert row["serial"] == "1234567890"
    assert row["mac"] == "AA:BB:CC:DD:EE:FF"
    assert row["name"] == "AQC Q1/3"
    assert row["password_confirmation"] == "Enter@qua1!"
    assert row["auto_credit_enable"] is True
