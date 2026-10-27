"""HTTPS for the cluster gateway (#536): the head's own CA and an employee PC's pinned copy.

The head Mac makes a private CA once (OMM_HOME/cluster/tls/ca.key, 0600) and
signs a server certificate for its current office-network addresses, its
.local name and 127.0.0.1. The gateway issues a new server certificate from
the same CA when those addresses change (DHCP), so an employee trusts the CA
once and never again.

An employee PC fetches the CA from the gateway the first time
(`omm rag remote set`), shows its SHA-256 fingerprint for the user to compare
with what the head prints, and pins it. Every later connection, omm's own
and each harness's, trusts that CA file and nothing else.

`cryptography` is imported only where a certificate is made or read, so the
employee side's plain connections never pay for it.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import http.client
import ipaddress
import logging
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from omm import config

CA_ROUTE = "/omm/ca.pem"
CA_FILE, CA_KEY_FILE = "ca.pem", "ca.key"
SERVER_FILE, SERVER_KEY_FILE = "server.pem", "server.key"
# Long enough that the head never has to make a new CA (which would make
# every employee confirm a new fingerprint).
CA_DAYS = 20 * 365
# Under the 825 days macOS accepts for a private CA's server certificate.
SERVER_DAYS = 397
RENEW_BEFORE_DAYS = 30
# How often the gateway compares this Mac's addresses with its certificate.
REFRESH_SECONDS = 30
MAX_CA_BYTES = 64 * 1024
FETCH_TIMEOUT = 10
# OpenSSL's X509_V_ERR_HOSTNAME_MISMATCH.
_HOSTNAME_MISMATCH = 62

log = logging.getLogger("omm.cluster.tls")


class CertificateProblem(ValueError):
    """A user-facing reason the head's certificate cannot be trusted."""


def tls_dir() -> Path:
    return config.OMM_HOME / "cluster" / "tls"


def fingerprint(pem: str | bytes) -> str:
    """SHA-256 of the certificate, as `openssl x509 -fingerprint -sha256` prints it."""
    text = pem.decode("ascii") if isinstance(pem, bytes) else pem
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(text)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# -- head side -------------------------------------------------------------

def _lan_ipv4s(run=subprocess.run) -> list[str]:
    """This Mac's IPv4 addresses employees can reach: not loopback, not Thunderbolt link-local."""
    try:
        text = run(["/sbin/ifconfig"], capture_output=True, text=True, check=False, timeout=5).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet":
            try:
                address = ipaddress.IPv4Address(parts[1])
            except ValueError:
                continue
            if not (address.is_loopback or address.is_link_local):
                found.append(str(address))
    return found


def _local_hostname(run=subprocess.run) -> str | None:
    """The Bonjour name (`node4.local`) other Macs and PCs on the office network resolve."""
    try:
        name = (run(["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                    check=False, timeout=5).stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    if not name:
        name = socket.gethostname().split(".")[0]
    return f"{name}.local".lower() if name else None


def head_names(run=subprocess.run) -> list[str]:
    """What the server certificate must cover, in a stable order."""
    names = ["127.0.0.1", "localhost", *sorted(set(_lan_ipv4s(run)))]
    host = _local_hostname(run)
    if host:
        names.append(host)
    return list(dict.fromkeys(names))


def _write_private(path: Path, data: bytes) -> None:
    from omm.atomic import atomic_write_bytes

    atomic_write_bytes(path, data)
    path.chmod(0o600)


def _write_public(path: Path, data: bytes) -> None:
    from omm.atomic import atomic_write_bytes

    atomic_write_bytes(path, data)
    path.chmod(0o644)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _san(names):
    from cryptography import x509

    entries = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _names_of(certificate) -> set[str]:
    from cryptography import x509

    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(v) for v in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))


def _make_ca(directory: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    host = _local_hostname() or "head"
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "omm cluster"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"omm cluster CA ({host})")])
    now = _now()
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    _write_private(directory / CA_KEY_FILE, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _write_public(directory / CA_FILE, certificate.public_bytes(serialization.Encoding.PEM))
    # A server certificate from an earlier CA is useless now.
    (directory / SERVER_FILE).unlink(missing_ok=True)


def _issue_server(directory: Path, names: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_pem = (directory / CA_FILE).read_bytes()
    ca = x509.load_pem_x509_certificate(ca_pem)
    ca_key = serialization.load_pem_private_key((directory / CA_KEY_FILE).read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[-1])]))
        .issuer_name(ca.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(_san(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(directory / SERVER_KEY_FILE, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    # The chain carries the CA too, so a client can see what signed the leaf.
    _write_public(directory / SERVER_FILE, certificate.public_bytes(serialization.Encoding.PEM) + ca_pem)


def _server_current(directory: Path, names: list[str]) -> bool:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        leaf = x509.load_pem_x509_certificate((directory / SERVER_FILE).read_bytes())
        ca = x509.load_pem_x509_certificate((directory / CA_FILE).read_bytes())
        if not (directory / SERVER_KEY_FILE).is_file():
            return False
        # Signed by an earlier CA (one that was made again) means issue anew.
        ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm))
    except (OSError, ValueError, TypeError, InvalidSignature):
        return False
    expires = leaf.not_valid_after_utc
    return _names_of(leaf) == set(names) and expires - _now() > datetime.timedelta(days=RENEW_BEFORE_DAYS)


def ensure(directory: Path | None = None, names: list[str] | None = None) -> bool:
    """Make the CA if there is none and a server certificate for `names`; True when one was issued."""
    directory = directory or tls_dir()
    names = names if names is not None else head_names()
    if not ((directory / CA_FILE).is_file() and (directory / CA_KEY_FILE).is_file()):
        _make_ca(directory)
    if _server_current(directory, names):
        return False
    _issue_server(directory, names)
    return True


def ca_pem(directory: Path | None = None) -> bytes | None:
    try:
        return ((directory or tls_dir()) / CA_FILE).read_bytes()
    except OSError:
        return None


def head_fingerprint(directory: Path | None = None) -> str | None:
    """The CA fingerprint employees compare in `omm rag remote set`; None before the CA exists."""
    pem = ca_pem(directory)
    try:
        return fingerprint(pem) if pem else None
    except ValueError:
        return None


class HeadCertificates:
    """The gateway's TLS context; re-issues the server certificate when this Mac's addresses change."""

    def __init__(self, directory: Path | None = None, names: Callable[[], list[str]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.directory = directory or tls_dir()
        self._names = names or (lambda: head_names())
        self._clock = clock
        self._lock = threading.Lock()
        ensure(self.directory, self._names())
        self._context = self._load()
        self._checked = clock()

    def _load(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.directory / SERVER_FILE, self.directory / SERVER_KEY_FILE)
        return context

    def ca_pem(self) -> bytes:
        return (self.directory / CA_FILE).read_bytes()

    def context(self) -> ssl.SSLContext:
        with self._lock:
            if self._clock() - self._checked >= REFRESH_SECONDS:
                self._checked = self._clock()
                try:
                    if ensure(self.directory, self._names()):
                        self._context = self._load()
                        log.info("issued a new gateway certificate", extra={"event": "cluster_tls_reissued"})
                except Exception:  # keep serving with the certificate we have
                    log.warning("could not renew the gateway certificate", exc_info=True,
                                extra={"event": "cluster_tls_renew_failed"})
            return self._context


# -- employee side -----------------------------------------------------------

def client_context(cafile: str | Path | None = None, *, cadata: str | None = None) -> ssl.SSLContext:
    """Trusts only the pinned CA; the host name is still checked."""
    return ssl.create_default_context(cafile=str(cafile) if cafile else None, cadata=cadata)


def is_certificate_error(error: BaseException) -> bool:
    """True for urllib/http.client failures caused by the head's certificate."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, ssl.SSLCertVerificationError)


@dataclass(frozen=True)
class OfferedCA:
    pem: str
    fingerprint: str


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise CertificateProblem("The cluster gateway only accepts HTTPS; use https://<head address>:52440")
    return parts.hostname, parts.port or 443


def _single_certificate(body: bytes) -> str:
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        text = ""
    if text.count("-----BEGIN CERTIFICATE-----") != 1:
        raise CertificateProblem("The head sent something that is not one certificate; check the address")
    start = text.index("-----BEGIN CERTIFICATE-----")
    end = text.find("-----END CERTIFICATE-----", start)
    if end < 0:
        raise CertificateProblem("The head sent something that is not one certificate; check the address")
    pem = text[start:end + len("-----END CERTIFICATE-----")] + "\n"
    try:
        ssl.PEM_cert_to_DER_cert(pem)
    except ValueError as error:
        raise CertificateProblem("The head sent a damaged certificate; check the address") from error
    return pem


def _covered_names(pem: str, host: str, port: int, timeout: float) -> list[str]:
    context = client_context(cadata=pem)
    context.check_hostname = False
    with socket.create_connection((host, port), timeout=timeout) as raw, context.wrap_socket(raw) as tls:
        alt = tls.getpeercert().get("subjectAltName", ())
    return [value for _kind, value in alt]


def fetch_ca(url: str, timeout: float = FETCH_TIMEOUT) -> OfferedCA:
    """The CA the gateway at `url` offers, once its own certificate is shown to chain to it.

    Nothing secret is sent: no token goes out until the user has accepted
    the fingerprint. Proxy variables are ignored like every other gateway call.
    """
    host, port = _host_port(url)
    # First look: nothing is trusted yet, which is what the fingerprint check is for.
    blind = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    blind.check_hostname = False
    blind.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=blind)
    try:
        connection.request("GET", CA_ROUTE, headers={"Accept": "application/x-pem-file"})
        response = connection.getresponse()
        body = response.read(MAX_CA_BYTES + 1)
        status = response.status
    except ssl.SSLError as error:
        raise CertificateProblem(
            f"{host} did not answer over HTTPS. If it is the cluster head, update omm there "
            "(omm update), then run omm cluster init on it") from error
    except (OSError, http.client.HTTPException) as error:
        raise CertificateProblem(f"Could not reach the cluster head at {url}; check the address and that "
                                 "you are on the office network") from error
    finally:
        connection.close()
    if status == 404:
        raise CertificateProblem(f"{host} runs an older omm; update omm on the head Mac (omm update), "
                                 "then run omm cluster init on it")
    if status != 200 or len(body) > MAX_CA_BYTES:
        raise CertificateProblem(f"{host} did not send a cluster certificate (HTTP {status}); check the address")
    pem = _single_certificate(body)
    # Then prove it: a handshake that trusts only that CA, host name checked
    # the way every harness will check it.
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, \
                client_context(cadata=pem).wrap_socket(raw, server_hostname=host):
            pass
    except ssl.SSLCertVerificationError as error:
        if error.verify_code == _HOSTNAME_MISMATCH:
            try:
                names = [name for name in _covered_names(pem, host, port, timeout) if name not in ("127.0.0.1", "localhost")]
            except (OSError, ssl.SSLError):
                names = []
            hint = f" Use one of: {', '.join(names)}" if names else ""
            raise CertificateProblem(f"The head's certificate does not cover {host}.{hint} "
                                     f"(for example omm rag remote set https://{names[0] if names else '<head address>'}:{port})") from error
        raise CertificateProblem(f"{host}'s certificate is not signed by the certificate it offers; "
                                 "this is not an omm cluster head") from error
    except (OSError, ssl.SSLError) as error:
        raise CertificateProblem(f"Could not reach the cluster head at {url}; check the address and that "
                                 "you are on the office network") from error
    return OfferedCA(pem, fingerprint(pem))


# What makes each coding harness trust the pinned CA. Claude Code reads
# env.NODE_EXTRA_CA_CERTS from its settings.json (its own error text says so,
# 2.1.308); pi and Cursor are Node and read it only from the environment they
# start in; Codex reads CODEX_CA_CERTIFICATE (developers.openai.com/codex/auth).
HARNESS_CA_ENV = {"claude": "NODE_EXTRA_CA_CERTS", "claude-code": "NODE_EXTRA_CA_CERTS",
                  "codex": "CODEX_CA_CERTIFICATE", "pi": "NODE_EXTRA_CA_CERTS", "cursor": "NODE_EXTRA_CA_CERTS"}
_HARNESS_COMMAND = {"claude": "claude", "claude-code": "claude", "codex": "codex", "pi": "pi"}


def launch_command(harness: str, ca_file: str | Path) -> str:
    """The shell line that starts `harness` trusting the head's certificate."""
    import shlex

    variable = HARNESS_CA_ENV.get(harness, "NODE_EXTRA_CA_CERTS")
    command = _HARNESS_COMMAND.get(harness, harness)
    return f"{variable}={shlex.quote(str(ca_file))} {command}"


def harness_trust_hint(harness: str, ca_file: str | Path) -> tuple[str, str]:
    """(what to do, the command line) so a harness trusts the cluster head's certificate."""
    command = launch_command(harness, ca_file)
    if harness in ("claude", "claude-code"):
        return ("Claude Code trusts the cluster head only with env.NODE_EXTRA_CA_CERTS set to that file in "
                "~/.claude/settings.json (omm setting agent cluster claude adds it), or when started like this:",
                command)
    if harness == "cursor":
        return ("Cursor trusts the cluster head only when NODE_EXTRA_CA_CERTS is in the environment it starts in "
                "(not checked on a real Cursor), for example:", f"NODE_EXTRA_CA_CERTS={ca_file}")
    return f"Start {_HARNESS_COMMAND.get(harness, harness)} like this so it trusts the cluster head:", command
