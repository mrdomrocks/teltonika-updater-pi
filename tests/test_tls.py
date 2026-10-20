from pathlib import Path

from cryptography import x509

from rutkeeper import __main__ as main
from rutkeeper.tls import ensure_tls


def test_ensure_tls_writes_cert_and_key(tmp_path: Path, monkeypatch):
    cert, key = ensure_tls(tmp_path)
    assert cert == tmp_path / "tls.crt"
    assert key == tmp_path / "tls.key"
    assert cert.is_file()
    assert key.is_file()
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = {name.value for name in san if isinstance(name, x509.DNSName)}
    ips = {str(name.value) for name in san if isinstance(name, x509.IPAddress)}
    assert "localhost" in dns
    assert "127.0.0.1" in ips
    again_cert, again_key = ensure_tls(tmp_path)
    assert again_cert == cert
    assert again_key == key

    monkeypatch.delenv("RUTKEEPER_TLS", raising=False)
    assert main.use_tls("127.0.0.1") is False
    assert main.use_tls("0.0.0.0") is True
    monkeypatch.setenv("RUTKEEPER_TLS", "0")
    assert main.use_tls("0.0.0.0") is False
    monkeypatch.setenv("RUTKEEPER_TLS", "1")
    assert main.use_tls("127.0.0.1") is True
