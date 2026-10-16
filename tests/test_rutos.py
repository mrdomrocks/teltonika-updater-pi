import httpx

from rutkeeper.rutos import RutosClient, RutosError, api_base


def test_api_base_normalizes_host():
    assert api_base("192.168.1.1") == "https://192.168.1.1/api"
    assert api_base("https://10.0.0.5/api") == "https://10.0.0.5/api"
    try:
        api_base("")
    except RutosError:
        pass
    else:
        raise AssertionError("empty host should fail")


def test_login_and_firmware_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"success": True, "data": {"token": "abc", "expires": 299}})
        if request.url.path == "/api/firmware/device/updates/status":
            assert request.headers["authorization"] == "Bearer abc"
            return httpx.Response(200, json={"success": True, "data": {"stable_version": "newest"}})
        return httpx.Response(404, json={"success": False})

    transport = httpx.MockTransport(handler)
    with RutosClient("https://192.168.8.1", "admin", "secret", verify_tls=False, transport=transport) as client:
        payload = client.updates_status()
    assert payload["data"]["stable_version"] == "newest"


def test_firmware_upload_uses_the_local_file_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"data": {"token": "abc"}})
        if request.url.path == "/api/firmware/actions/upload_device_firmware":
            seen["upload"] = request.content
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/api/firmware/actions/upgrade":
            return httpx.Response(200, json={"success": True, "data": {"keep_settings": 1}})
        return httpx.Response(404, json={"success": False})

    transport = httpx.MockTransport(handler)
    with RutosClient("192.168.1.1", "admin", "secret", transport=transport) as client:
        client.upload_firmware("rut.bin", b"image-bytes")
        client.upgrade_staged()
    assert b"image-bytes" in seen["upload"]


def test_backup_download_accepts_a_file_body():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"data": {"token": "abc"}})
        if request.url.path == "/api/backup/actions/download":
            return httpx.Response(
                200,
                content=b"backup-bytes",
                headers={"content-type": "application/gzip", "content-disposition": 'attachment; filename="gate.tar.gz"'},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with RutosClient("10.1.1.1", "admin", "secret", transport=transport) as client:
        content, name = client.download_backup("device")
    assert content == b"backup-bytes"
    assert name == "gate.tar.gz"
