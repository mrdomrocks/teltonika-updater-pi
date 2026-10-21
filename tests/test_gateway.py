import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from rutkeeper.gateway import start_gateway
from rutkeeper.tls import ensure_tls


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"ok-workbench"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A003
        return


def test_gateway_https_and_http_redirect(tmp_path):
    backend = HTTPServer(("127.0.0.1", 0), _Ok)
    backend_port = backend.server_address[1]
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()

    cert, key = ensure_tls(tmp_path)
    public = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    public.bind(("127.0.0.1", 0))
    public_port = public.getsockname()[1]
    public.close()

    gateway = start_gateway("127.0.0.1", public_port, "127.0.0.1", backend_port, cert, key)
    time.sleep(0.2)

    http = socket.create_connection(("127.0.0.1", public_port), timeout=3)
    http.sendall(b"GET /bench HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % public_port)
    response = http.recv(4096).decode("iso-8859-1", errors="replace")
    http.close()
    assert response.startswith("HTTP/1.1 308")
    assert f"Location: https://127.0.0.1:{public_port}/bench" in response

    import ssl
    import urllib.request

    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(f"https://127.0.0.1:{public_port}/", context=ctx, timeout=3) as resp:
        assert resp.status == 200
        assert resp.read() == b"ok-workbench"

    gateway.stop_event.set()
    backend.shutdown()
