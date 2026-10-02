"""The GPU runner's certificate and its server, over real TLS on 127.0.0.1.

`DeadlineH11` reaches into uvicorn's internals, and uvicorn is pinned for
exactly that reason: these tests are what fails first if an upgrade moves the
names it overrides. They drive the real config from server.build_config, so the
TLS floor and the head deadline asserted here are the ones the image serves
with.

Every test is named after the mistake it prevents.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import resource
import socket
import ssl
import sys
import time

import pytest

from runner_support import (AUTH, build_runner, client_context, settle,
                            tls_server)


# ------------------------------------------------------------ certificate --


def test_a_pair_is_minted_once_and_the_fingerprint_is_its_der_digest(runner_modules,
                                                                    tmp_path):
    tls = runner_modules.tls
    cert, key = tls.ensure(tmp_path / "tls")
    first = tls.fingerprint(cert)
    again_cert, again_key = tls.ensure(tmp_path / "tls")
    assert (again_cert, again_key) == (cert, key)
    assert tls.fingerprint(again_cert) == first, "a second start minted a new pair"
    pem = cert.read_text(encoding="ascii")
    assert first == hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    assert key.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "tls").stat().st_mode & 0o777 == 0o700
    assert not list((tmp_path / "tls").glob("*.tmp")), "a temporary file was left"


def test_the_certificate_is_a_p256_leaf_named_for_the_runner(runner_modules, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec

    cert, _ = runner_modules.tls.ensure(tmp_path / "tls")
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    assert isinstance(parsed.public_key(), ec.EllipticCurvePublicKey)
    assert parsed.public_key().curve.name == "secp256r1"
    assert parsed.subject.rfc4514_string() == "CN=calliope-tts-runner"
    assert parsed.extensions.get_extension_for_class(
        x509.BasicConstraints).value.ca is False
    assert parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == ["localhost"]
    assert parsed.signature_hash_algorithm.name == "sha256"


@pytest.mark.parametrize("keep", ["cert.pem", "key.pem"])
def test_half_a_pair_stops_the_runner_rather_than_minting_over_it(runner_modules,
                                                                  tmp_path, keep):
    tls = runner_modules.tls
    cert, key = tls.ensure(tmp_path / "tls")
    (key if keep == "cert.pem" else cert).unlink()
    with pytest.raises(SystemExit) as stopped:
        tls.ensure(tmp_path / "tls")
    assert "delete both to mint a new pair" in str(stopped.value.code)


# ----------------------------------------------------------------- server --


def test_the_server_is_tls_1_3_only_and_speaks_h11_with_a_deadline(runner_modules,
                                                                  tmp_path):
    from uvicorn.protocols.http.h11_impl import H11Protocol

    cert, key = runner_modules.tls.ensure(tmp_path / "tls")
    built = build_runner(runner_modules, tmp_path / "state")
    config = runner_modules.server.build_config(built.app, cert, key)
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_3
    assert config.http_protocol_class is runner_modules.server.DeadlineH11
    assert issubclass(config.http_protocol_class, H11Protocol)
    assert config.limit_concurrency is None
    assert config.server_header is False and config.access_log is False


@pytest.fixture
def served(runner_modules, tmp_path, monkeypatch):
    monkeypatch.setattr(runner_modules.server, "HEAD_DEADLINE_S", 1.0)
    cert, key = runner_modules.tls.ensure(tmp_path / "tls")
    built = build_runner(runner_modules, tmp_path / "state")
    with tls_server(runner_modules, built.app, cert, key) as port:
        yield port


def _get(port: int, path: str = "/v1/status") -> int:
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=10,
                                       context=client_context())
    try:
        conn.request("GET", path, headers={**AUTH, "Connection": "close"})
        return conn.getresponse().status
    finally:
        conn.close()


def _closed(sock: ssl.SSLSocket) -> bool:
    sock.settimeout(0.2)
    try:
        return sock.recv(1) == b""
    except (TimeoutError, socket.timeout):
        return False
    except (OSError, ssl.SSLError):
        return True


def test_idle_tls_connections_neither_block_requests_nor_outlive_the_deadline(served):
    """MEASURED: sixteen silent TLS sockets turned every request into a 503.

    With `limit_concurrency` gone and the head deadline in, sixty-four of them
    cost a request nothing, and all sixty-four are closed once the deadline
    passes.
    """
    context = client_context()
    idle = [context.wrap_socket(socket.create_connection(("127.0.0.1", served)),
                                server_hostname="localhost") for _ in range(64)]
    try:
        assert _get(served) == 200
        assert not any(_closed(s) for s in idle[:4]), "closed before the deadline"
        time.sleep(1.5)
        settle(lambda: all(_closed(s) for s in idle), timeout=10,
               why="idle connections outlived the head deadline")
    finally:
        for sock in idle:
            sock.close()
    assert _get(served) == 200


def test_a_megabyte_of_headers_is_refused_without_buffering_it(served):
    """MEASURED: httptools buffered 190 MiB of headers from a client with no key."""
    def peak_mib() -> float:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1 << 20) if sys.platform == "darwin" else peak / 1024

    before = peak_mib()
    sock = client_context().wrap_socket(
        socket.create_connection(("127.0.0.1", served)), server_hostname="localhost")
    sock.settimeout(10)
    line = b"X-Pad: " + b"a" * 1000 + b"\r\n"
    refused = False
    try:
        sock.sendall(b"GET /v1/status HTTP/1.1\r\nHost: localhost\r\n")
        for _ in range(1024):
            sock.sendall(line)
        sock.sendall(b"\r\n")
        answer = sock.recv(64)
        refused = answer == b"" or answer.startswith(b"HTTP/1.1 400")
    except (OSError, ssl.SSLError):
        refused = True
    finally:
        sock.close()
    assert refused, "a megabyte of headers was accepted"
    assert peak_mib() - before < 50
    assert _get(served) == 200


def test_a_tls_1_2_client_is_refused(served):
    context = client_context(maximum=ssl.TLSVersion.TLSv1_2)
    with pytest.raises(ssl.SSLError):
        with socket.create_connection(("127.0.0.1", served)) as raw:
            with context.wrap_socket(raw, server_hostname="localhost"):
                pass


def test_the_served_certificate_is_the_one_on_disk(runner_modules, served, tmp_path):
    with socket.create_connection(("127.0.0.1", served)) as raw:
        with client_context().wrap_socket(raw, server_hostname="localhost") as tls:
            der = tls.getpeercert(binary_form=True)
            assert tls.version() == "TLSv1.3"
    assert hashlib.sha256(der).hexdigest() == runner_modules.tls.fingerprint(
        tmp_path / "tls" / "cert.pem")
    assert os.path.exists(tmp_path / "tls" / "key.pem")
