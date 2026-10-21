import json
import subprocess
from pathlib import Path

import pytest

from rutkeeper.label import LabelError, parse_label

ROOT = Path(__file__).resolve().parents[1]
LABEL_JS = ROOT / "rutkeeper" / "static" / "label.js"

SAMPLE = (
    "WIFI:T:WPA;S:RUT_BBAA_2G;P:wifi-secret;SN:1234567890;"
    "I:123456789012345;M:AABBCCDDEEFF;U:admin;PW:device-secret;B:12;"
)

CASES = [
    {"text": SAMPLE, "password": "device-secret", "username": "admin", "serial": "1234567890", "mac": "AABBCCDDEEFF"},
    {
        "text": "WIFI:T:WPA;S:XX9999_9999;P:99999999;SN:9999999999;I:999999999999999; M:AABBCCDDEEFF;U:admin;PW:99999999;B:999;",
        "password": "99999999",
        "username": "admin",
        "serial": "9999999999",
        "mac": "AABBCCDDEEFF",
    },
    {"text": "U:root;PW:from-the-label", "password": "from-the-label", "username": "root"},
    {"text": "pw:lower-key", "password": "lower-key", "username": ""},
    {"text": r"WIFI:T:WPA;P:wifi-only;PW:a\;b\\c\:d;U:admin;", "password": r"a;b\c:d", "username": "admin"},
    {"text": "admin01", "password": "admin01", "username": ""},
    {"text": "WIFI:T:WPA;S:RUT_1;P:wifi-secret;U:admin;", "error": "no-password"},
    {"text": "https://192.168.1.1", "error": "unrecognized"},
    {"text": "   ", "error": "empty"},
]


@pytest.mark.parametrize("case", CASES)
def test_label_parser(case):
    if case.get("error"):
        with pytest.raises(LabelError) as caught:
            parse_label(case["text"])
        assert caught.value.code == case["error"]
        return
    parsed = parse_label(case["text"])
    assert parsed["password"] == case["password"]
    assert parsed["username"] == case["username"]
    assert parsed["serial"] == case.get("serial", "")
    assert parsed["mac"] == case.get("mac", "")
    assert "wifi-secret" not in parsed["password"]


def test_javascript_parser_matches():
    script = """
const api = require(process.argv[1]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
for (const item of cases) {
  try {
    const got = api.parseLabel(item.text);
    if (item.error) {
      console.error("expected " + item.error);
      process.exit(1);
    }
    if (got.password !== item.password || (got.username || "") !== (item.username || "")) {
      console.error(JSON.stringify({ item: item, got: got }));
      process.exit(1);
    }
    if ((got.serial || "") !== (item.serial || "") || (got.mac || "") !== (item.mac || "")) {
      console.error(JSON.stringify({ item: item, got: got }));
      process.exit(1);
    }
  } catch (error) {
    if (!item.error || (error.code || error.message) !== item.error) {
      console.error(String(error && (error.code || error.message)));
      process.exit(1);
    }
  }
}
"""
    completed = subprocess.run(
        ["node", "-e", script, str(LABEL_JS)],
        input=json.dumps(CASES),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
