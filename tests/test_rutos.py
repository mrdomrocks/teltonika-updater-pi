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


def test_login_accepts_the_factory_firmware_session():
    """Pre-7.06 images accept the label password and return ubus_rpc_session, not token."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        body = request.read()
        assert b'"data"' not in body
        return httpx.Response(
            200,
            json={
                "success": True,
                "ubus_rpc_session": "factory-session",
                "expires": 299,
                "data": {"username": "admin"},
            },
        )

    transport = httpx.MockTransport(handler)
    with RutosClient("192.168.1.1", "admin", "label-password", transport=transport) as client:
        client.login()
    assert client.token == "factory-session"
    assert client.password_change_required is False


def test_login_keeps_a_session_when_the_factory_password_must_be_changed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "errors": [{"error": "Password change required"}],
                "data": {"ubus_rpc_session": "temp-session"},
            },
        )

    transport = httpx.MockTransport(handler)
    with RutosClient("192.168.1.1", "admin", "label-password", transport=transport) as client:
        client.login()
    assert client.token == "temp-session"
    assert client.password_change_required is True


def test_login_uses_http_when_https_does_not_connect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            raise httpx.ConnectError("tls failed")
        assert request.url.scheme == "http"
        return httpx.Response(200, json={"success": True, "data": {"token": "from-http"}})

    transport = httpx.MockTransport(handler)
    with RutosClient("192.168.1.1", "admin", "label-password", transport=transport) as client:
        client.login()
    assert client.token == "from-http"
    assert client.base == "http://192.168.1.1/api"


def test_login_does_not_retry_http_when_the_password_is_refused():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.scheme)
        return httpx.Response(401, json={"success": False, "errors": [{"error": "Unauthorized"}]})

    transport = httpx.MockTransport(handler)
    with RutosClient("192.168.1.1", "admin", "wrong", transport=transport) as client:
        try:
            client.login()
        except RutosError as exc:
            assert exc.status == 401
        else:
            raise AssertionError("refused password should fail")
    assert seen == ["https", "https"]


def test_password_change_uses_ubus_when_users_lua_rejects_a_string():
    """Firmware 07.24 crashes PUT /users/config once the current password is right."""
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            body = json.loads(request.content.decode())
            sent = body.get("password") or (body.get("data") or {}).get("password")
            if sent == "Enter@qua1!":
                return httpx.Response(200, json={"success": True, "data": {"token": "after"}})
            return httpx.Response(200, json={"success": True, "data": {"token": "before"}})
        if request.url.path == "/api/system/actions/change_password_firstlogin":
            return httpx.Response(404, json={"success": False, "errors": [{"error": "missing"}]})
        if request.url.path == "/api/users/config":
            return httpx.Response(
                200,
                json={"success": True, "data": [{"id": "login1", "username": "admin", ".type": "login"}]},
            )
        if request.url.path == "/api/users/config/login1":
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "errors": [
                        {
                            "source": "Lua",
                            "error": "/usr/lib/lua/api/system/users.lua: bad argument #2 to '?' (number expected, got string)",
                        }
                    ],
                },
            )
        if request.url.path == "/ubus":
            body = json.loads(request.content.decode())
            params = body["params"]
            seen.append({"obj": params[1], "method": params[2], "args": params[3]})
            if params[2] == "login":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [0, {"ubus_rpc_session": "ubus-1"}]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [0, {}]})
        return httpx.Response(404, json={"success": False})

    transport = httpx.MockTransport(handler)
    with RutosClient("192.168.100.1", "admin", "from-config", transport=transport) as client:
        how = client.set_admin_password("from-config", "Enter@qua1!")
    assert how == "system"
    assert seen[0]["method"] == "login"
    assert seen[0]["args"]["password"] == "from-config"
    assert seen[1]["obj"] == "rpc-sys"
    assert seen[1]["method"] == "password_set"
    assert seen[1]["args"] == {"user": "admin", "password": "Enter@qua1!"}


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
