"""HTTPS on the cluster gateway and certificate pinning on employee PCs (#536)."""

from __future__ import annotations

import json
import os
import shlex
import socket
import ssl
import threading

import pytest
from typer.testing import CliRunner

from omm import cli, rag_remote
from omm.cluster import gateway, tls, tokens
from tests import _gateway_tls

runner = CliRunner()


def _start(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def head(isolated_omm_home):
    token = tokens.add_token("kim")
    server = _start(gateway.GatewayServer("127.0.0.1", 0, upstream="http://127.0.0.1:9"))
    yield server, token
    server.shutdown()
    server.server_close()


# -- the head's certificates ----------------------------------------------------

def test_ca_and_server_keys_are_owner_only(tmp_path):
    assert tls.ensure(tmp_path, ["127.0.0.1", "192.0.2.10", "node4.local"]) is True
    if os.name != "nt":
        assert (tmp_path / "ca.key").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "server.key").stat().st_mode & 0o777 == 0o600
    assert tls.ensure(tmp_path, ["127.0.0.1", "192.0.2.10", "node4.local"]) is False


def test_a_long_bonjour_name_still_gets_certificates(tmp_path, monkeypatch):
    # X.509 caps a common name at 64 characters; a 63-character LocalHostName plus ".local" is 69.
    from cryptography import x509

    host = "m" * 63 + ".local"
    monkeypatch.setattr(tls, "_local_hostname", lambda run=None: host)
    assert tls.ensure(tmp_path, ["127.0.0.1", host]) is True
    assert host in tls._names_of(x509.load_pem_x509_certificate((tmp_path / "server.pem").read_bytes()))


def test_server_certificate_is_reissued_from_the_same_ca_when_addresses_change(tmp_path):
    names = ["127.0.0.1", "192.0.2.10"]
    now = [0.0]
    certificates = tls.HeadCertificates(tmp_path, names=lambda: list(names), clock=lambda: now[0])
    ca_before = certificates.ca_pem()
    first = certificates.context()
    names[1] = "192.0.2.77"  # DHCP gave the head a new address
    assert certificates.context() is first  # not checked again before REFRESH_SECONDS
    now[0] += tls.REFRESH_SECONDS
    second = certificates.context()
    assert second is not first
    assert certificates.ca_pem() == ca_before
    from cryptography import x509

    leaf = x509.load_pem_x509_certificate((tmp_path / "server.pem").read_bytes())
    assert tls._names_of(leaf) == {"127.0.0.1", "192.0.2.77"}


def test_head_names_skip_loopback_and_thunderbolt_link_local():
    class Done:
        def __init__(self, stdout):
            self.stdout = stdout

    def run(argv, **_):
        if argv[0].endswith("ifconfig"):
            return Done("lo0: flags\n\tinet 127.0.0.1 netmask 0xff000000\n"
                        "en0: flags\n\tinet 192.168.0.5 netmask 0xffffff00 broadcast 192.168.0.255\n"
                        "en2: flags\n\tinet 169.254.10.3 netmask 0xffff0000\n\tinet6 fe80::1%en2\n")
        return Done("node4\n")

    assert tls.head_names(run) == ["127.0.0.1", "localhost", "192.168.0.5", "node4.local"]


# -- the gateway ------------------------------------------------------------------

def test_plain_http_is_refused_before_the_token_is_read(head, monkeypatch):
    server, token = head
    checked = []
    monkeypatch.setattr(server.store, "verify", lambda presented: checked.append(presented))
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(f"GET /v1/models HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n\r\n".encode())
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    head_line, _, body = reply.partition(b"\r\n\r\n")
    assert head_line.startswith(b"HTTP/1.1 400")
    assert "omm rag remote set https://" in json.loads(body)["error"]["message"]
    assert checked == []


def test_ca_is_served_without_a_token(head):
    server, _ = head
    conn = _gateway_tls.connection(server)
    conn.request("GET", tls.CA_ROUTE)
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == server.certificates.ca_pem()


def test_a_client_pinning_another_ca_cannot_connect(head, tmp_path):
    server, _ = head
    tls.ensure(tmp_path / "other", ["127.0.0.1"])
    context = tls.client_context(tmp_path / "other" / "ca.pem")
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as raw:
        with pytest.raises(ssl.SSLCertVerificationError):
            context.wrap_socket(raw, server_hostname="127.0.0.1")


# -- employee: fetching and pinning -----------------------------------------------

def test_fetch_ca_returns_the_heads_ca_and_its_fingerprint(head):
    server, _ = head
    offered = tls.fetch_ca(_gateway_tls.url(server))
    assert offered.pem.encode() == server.certificates.ca_pem()
    assert offered.fingerprint == tls.head_fingerprint()


def test_fetch_ca_names_the_addresses_a_mismatched_host_should_use(head, monkeypatch):
    server, _ = head
    tls.ensure(server.certificates.directory, ["127.0.0.1", "192.0.2.10"])
    server.certificates._context = server.certificates._load()
    # "localhost" resolves to 127.0.0.1 but is not in this certificate.
    with pytest.raises(tls.CertificateProblem, match="does not cover localhost. Use one of: 192.0.2.10"):
        tls.fetch_ca(f"https://localhost:{server.server_address[1]}")


def test_fetch_ca_from_a_plain_http_server_says_to_update_the_head():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Old(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    old = _start(ThreadingHTTPServer(("127.0.0.1", 0), Old))
    try:
        with pytest.raises(tls.CertificateProblem, match="did not answer over HTTPS"):
            tls.fetch_ca(f"https://127.0.0.1:{old.server_address[1]}")
    finally:
        old.shutdown()
        old.server_close()


def _set(server, *extra, token=None, tty=True, confirm=True, monkeypatch=None):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: tty)
    asked = []

    def ask(message, default=False):
        asked.append(message)
        return confirm

    monkeypatch.setattr(cli, "_ask_confirm", ask)
    result = runner.invoke(cli.app, ["rag", "remote", "set", _gateway_tls.url(server), "--token", token, *extra])
    return result, asked


def test_tofu_confirmed_pins_the_ca_and_later_sets_do_not_ask(head, monkeypatch):
    server, token = head
    result, asked = _set(server, token=token, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert asked and tls.head_fingerprint() in result.output
    assert rag_remote.pinned_ca().encode() == server.certificates.ca_pem()
    assert rag_remote.load().url == _gateway_tls.url(server)
    again, asked = _set(server, token=token, monkeypatch=monkeypatch)
    assert again.exit_code == 0 and asked == []


def test_tofu_declined_saves_nothing_and_sends_no_token(head, monkeypatch):
    server, token = head
    sent = []
    monkeypatch.setattr(server.store, "verify", lambda presented: sent.append(presented))
    result, asked = _set(server, token=token, confirm=False, monkeypatch=monkeypatch)
    assert result.exit_code == 1 and asked
    assert not rag_remote.ca_path().exists() and not rag_remote.remote_path().exists()
    assert sent == []


def test_tofu_without_a_terminal_needs_yes(head, monkeypatch):
    server, token = head
    result, asked = _set(server, token=token, tty=False, monkeypatch=monkeypatch)
    assert result.exit_code == 1 and "--yes" in result.output and asked == []
    assert not rag_remote.ca_path().exists()
    result, asked = _set(server, "--yes", token=token, tty=False, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert asked == [] and rag_remote.ca_path().exists()


def test_a_head_with_a_different_certificate_is_refused(head, monkeypatch, tmp_path):
    server, token = head
    tls.ensure(tmp_path / "earlier", ["127.0.0.1"])
    rag_remote.save_ca((tmp_path / "earlier" / "ca.pem").read_text(encoding="utf-8"))
    result, asked = _set(server, "--yes", token=token, monkeypatch=monkeypatch)
    assert result.exit_code == 1 and asked == []
    assert "different certificate" in result.output and "omm rag remote clear" in result.output
    assert not rag_remote.remote_path().exists()


def _pin_earlier_head(tmp_path, url):
    tls.ensure(tmp_path / "earlier", ["127.0.0.1"])
    earlier = (tmp_path / "earlier" / "ca.pem").read_text(encoding="utf-8")
    rag_remote.save_ca(earlier)
    rag_remote.save(rag_remote.Remote(url, "omm_old"))
    return earlier


def test_the_same_address_with_a_different_certificate_is_still_refused(head, monkeypatch, tmp_path):
    server, token = head
    earlier = _pin_earlier_head(tmp_path, _gateway_tls.url(server))
    result, asked = _set(server, "--yes", token=token, monkeypatch=monkeypatch)
    assert result.exit_code == 1 and asked == []
    assert "different certificate" in result.output
    assert rag_remote.pinned_ca() == earlier and rag_remote.load().token == "omm_old"


def test_a_new_head_address_asks_again_and_replaces_the_pin(head, monkeypatch, tmp_path):
    # After `omm cluster handover` the head has another address and its own CA.
    server, token = head
    _pin_earlier_head(tmp_path, "https://10.0.0.9:52440")
    result, asked = _set(server, token=token, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert asked and tls.head_fingerprint() in result.output
    assert rag_remote.pinned_ca().encode() == server.certificates.ca_pem()
    assert rag_remote.load() == rag_remote.Remote(_gateway_tls.url(server), token)


def test_a_new_head_address_declined_keeps_the_old_head(head, monkeypatch, tmp_path):
    server, token = head
    earlier = _pin_earlier_head(tmp_path, "https://10.0.0.9:52440")
    result, asked = _set(server, token=token, confirm=False, monkeypatch=monkeypatch)
    assert result.exit_code == 1 and asked
    assert rag_remote.pinned_ca() == earlier and rag_remote.load().url == "https://10.0.0.9:52440"


def test_a_new_head_address_without_a_terminal_needs_yes(head, monkeypatch, tmp_path):
    server, token = head
    earlier = _pin_earlier_head(tmp_path, "https://10.0.0.9:52440")
    result, asked = _set(server, token=token, tty=False, monkeypatch=monkeypatch)
    assert result.exit_code == 1 and "--yes" in result.output
    assert rag_remote.pinned_ca() == earlier
    result, asked = _set(server, "--yes", token=token, tty=False, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert rag_remote.pinned_ca().encode() == server.certificates.ca_pem()


def test_a_remote_saved_over_plain_http_says_to_set_it_again(isolated_omm_home):
    rag_remote.save(rag_remote.Remote("http://10.0.0.5:52440", "omm_x"))
    result = runner.invoke(cli.app, ["rag", "collection", "list"])
    assert result.exit_code == 1
    assert "HTTPS only" in result.output and "https://" in result.output


def test_rag_connect_remote_says_how_the_harness_trusts_the_head(isolated_omm_home, tmp_path, monkeypatch):
    from pathlib import Path

    from omm import rag_connect

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(rag_connect.shutil, "which", lambda name: "/installed/" + name)
    rag_remote.save(rag_remote.Remote("https://10.0.0.5:52440", "omm_x"))
    result = runner.invoke(cli.app, ["rag", "connect", "codex", "--remote"])
    assert result.exit_code == 0, result.output
    assert f"CODEX_CA_CERTIFICATE={shlex.quote(str(rag_remote.ca_path()))} codex" in result.output


def test_fingerprint_is_printed_verbatim_not_as_emoji(head, monkeypatch):
    # rich turns ":CD:" into an emoji unless told not to.
    server, token = head
    real = tls.fetch_ca(_gateway_tls.url(server))
    monkeypatch.setattr(tls, "fetch_ca", lambda url: tls.OfferedCA(real.pem, "AB:CD:EF:01"))
    result, _ = _set(server, token=token, confirm=False, monkeypatch=monkeypatch)
    assert "AB:CD:EF:01" in result.output
