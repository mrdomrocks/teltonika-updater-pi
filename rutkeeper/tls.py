"""Self-signed TLS for the workbench camera.

Browsers only allow live camera access in a secure context. On a phone that
means HTTPS. TUA mints a local certificate in the data directory so Scan the
label can open the camera instead of falling back to a photo.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def tls_paths(data_dir: Path) -> tuple[Path, Path]:
    root = Path(data_dir)
    return root / "tls.crt", root / "tls.key"


def ensure_tls(data_dir: Path) -> tuple[Path, Path]:
    """Return certificate and key paths, creating them on first use."""
    cert_path, key_path = tls_paths(data_dir)
    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostnames = _hostnames()
    names: list[x509.GeneralName] = []
    for host in hostnames:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)
    return cert_path, key_path


def _hostnames() -> list[str]:
    names = ["localhost", "tua.local"]
    try:
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    if host and host not in names:
        names.append(host)
        if "." not in host:
            names.append(f"{host}.local")
    names.append("127.0.0.1")
    names.append("::1")
    for ip in _local_ips():
        if ip not in names:
            names.append(ip)
    return names


def _local_ips() -> list[str]:
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr and addr not in found and not addr.startswith("127."):
                found.append(addr)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("1.1.1.1", 80))
            addr = probe.getsockname()[0]
            if addr and addr not in found:
                found.append(addr)
        finally:
            probe.close()
    except OSError:
        pass
    return found
