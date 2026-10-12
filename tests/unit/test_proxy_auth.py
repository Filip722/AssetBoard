"""Proxy header authentication, part A: peer capture, peer trust and header parsing (F1.4)."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
import uvicorn.config
from starlette.requests import Request
from starlette.types import Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from assetboard.app import create_app
from assetboard.branding import APP_NAME
from assetboard.cli import serve_app, server_options
from assetboard.config import Settings
from assetboard.middleware import PEER_SCOPE_KEY, PeerMiddleware
from assetboard.services.auth import client_ip_prefix
from assetboard.services.external_identity import PROXY_PROVIDER, SignInIdentity
from assetboard.services.proxy_auth import (
    ProxyHeaders,
    identity_header_names,
    peer_is_trusted,
    read_identity_headers,
)
from assetboard.web.security import proxy_peer_trusted


def proxy_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "proxy_auth_enabled": True,
        "proxy_auth_user_header": "Remote-User",
        "proxy_auth_email_header": "Remote-Email",
        "proxy_auth_name_header": "Remote-Name",
        "trusted_proxies": "172.20.0.2,fd00:20::2",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def at(self, level: int) -> list[str]:
        return [record.getMessage() for record in self.records if record.levelno == level]


@contextmanager
def app_log() -> Iterator[Records]:
    logger = logging.getLogger(APP_NAME.lower())
    handler = Records()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


# Peer trust: CIDR edges, IPv6 and peers that are not IP addresses


NETWORKS = (
    ip_network("172.20.0.0/24"),
    ip_network("fd00:20::/64"),
    ip_network("127.0.0.1/32"),
    ip_network("fe80::/64"),
)


@pytest.mark.parametrize(
    ("peer", "trusted"),
    [
        ("172.20.0.0", True),  # first address of the network
        ("172.20.0.255", True),  # last address
        ("172.20.1.0", False),  # one past the end
        ("172.19.255.255", False),  # one before the start
        ("127.0.0.1", True),
        ("127.0.0.2", False),
        ("::ffff:172.20.0.7", True),  # dual-stack socket: compared as IPv4
        ("::ffff:172.20.1.7", False),
        ("fd00:20::1", True),
        ("fd00:20::ffff:ffff:ffff:ffff", True),
        ("fd00:20:0:1::", False),
        ("::1", False),  # loopback over IPv6 is not 127.0.0.1/32
        ("fe80::1", True),
        ("fe80::1%eth0", False),  # a zone: the address may be another host on each interface
        ("fe80::1%2", False),
        ("2001:db8::1", False),
        (None, False),  # no peer (for example a Unix socket)
        ("", False),
        ("testclient", False),
        ("/run/assetboard.sock", False),
        ("172.20.0.2, 10.0.0.1", False),  # never a forwarding chain
        ("172.20.0.2:5000", False),
    ],
)
def test_peer_trust(peer: str | None, trusted: bool) -> None:
    assert peer_is_trusted(peer, NETWORKS) is trusted


def test_ipv4_peers_never_match_ipv6_networks_and_the_reverse() -> None:
    assert not peer_is_trusted("10.0.0.1", (ip_network("::/0"),))
    assert not peer_is_trusted("::1", (ip_network("0.0.0.0/0"),))
    assert not peer_is_trusted("::ffff:10.0.0.1", (ip_network("::ffff:0:0/96"),))


# Header parsing


def headers(**values: str | bytes | list[str | bytes]) -> list[tuple[bytes, bytes]]:
    raw: list[tuple[bytes, bytes]] = []
    for name, value in values.items():
        for item in value if isinstance(value, list) else [value]:
            data = item.encode("utf-8") if isinstance(item, str) else item
            raw.append((name.replace("_", "-").lower().encode("ascii"), data))
    return raw


def test_trusted_headers_become_a_proxy_identity_with_a_verified_email(tmp_path: Path) -> None:
    settings = proxy_settings(tmp_path)
    parsed = read_identity_headers(
        headers(remote_user="ola", remote_email=" Ola@Example.COM ", remote_name="Ola Nowak"),
        settings,
    )
    assert parsed == ProxyHeaders(
        "ok",
        SignInIdentity(
            issuer=PROXY_PROVIDER,
            subject="ola",
            email="ola@example.com",
            email_verified=True,
            display_name="Ola Nowak",
            groups=None,
        ),
    )


def test_header_names_are_matched_case_insensitively(tmp_path: Path) -> None:
    settings = proxy_settings(tmp_path)
    raw = [(b"Remote-User", b"ola"), (b"REMOTE-EMAIL", b"ola@example.com")]
    parsed = read_identity_headers(raw, settings)
    assert parsed.identity is not None and parsed.identity.email == "ola@example.com"
    duplicated = [(b"remote-user", b"ola"), (b"Remote-User", b"admin")]
    assert read_identity_headers(duplicated, settings) == ProxyHeaders(
        "duplicate", header="remote-user"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, ProxyHeaders("absent")),
        ({"remote_email": "ola@example.com"}, ProxyHeaders("absent")),
        ({"remote_user": ""}, ProxyHeaders("absent")),
        ({"remote_user": "   "}, ProxyHeaders("absent")),
        ({"remote_user": ["", "ola"]}, None),  # an empty copy counts as absent
        ({"remote_user": ["ola", "admin"]}, ProxyHeaders("duplicate", header="remote-user")),
        ({"remote_user": ["ola", "ola"]}, ProxyHeaders("duplicate", header="remote-user")),
        (
            {"remote_user": "ola", "remote_email": ["a@example.com", "b@example.com"]},
            ProxyHeaders("duplicate", header="remote-email"),
        ),
        (
            {"remote_user": "ola", "remote_name": ["A", "B"]},
            ProxyHeaders("duplicate", header="remote-name"),
        ),
        ({"remote_user": b"ol\xff"}, ProxyHeaders("invalid", header="remote-user")),
        ({"remote_user": "ola\x00admin"}, ProxyHeaders("invalid", header="remote-user")),
        ({"remote_user": "ola‮admin"}, ProxyHeaders("invalid", header="remote-user")),
        ({"remote_user": "x" * 256}, ProxyHeaders("invalid", header="remote-user")),
        ({"remote_user": "ola\u2028admin"}, ProxyHeaders("invalid", header="remote-user")),  # Zl
        ({"remote_user": "ola\u2029admin"}, ProxyHeaders("invalid", header="remote-user")),  # Zp
        ({"remote_user": "ola\ue000"}, ProxyHeaders("invalid", header="remote-user")),  # Co
        ({"remote_user": "ola\U000f0001"}, ProxyHeaders("invalid", header="remote-user")),  # Co
        ({"remote_user": "ola\u0378"}, ProxyHeaders("invalid", header="remote-user")),  # Cn
        ({"remote_user": "Żaneta"}, None),  # letters outside ASCII are fine
    ],
)
def test_absent_duplicate_and_invalid_headers(
    tmp_path: Path, raw: dict[str, Any], expected: ProxyHeaders | None
) -> None:
    parsed = read_identity_headers(headers(**raw), proxy_settings(tmp_path))
    if expected is None:
        assert parsed.status == "ok"
    else:
        assert parsed == expected


@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "ola@",
        b"ola@exa\xffmple.com",
        "ola\x00@example.com",
        "ola\u2029@example.com",
        "ola\ue000@example.com",
    ],
)
def test_a_missing_or_malformed_email_counts_as_unverified(
    tmp_path: Path, email: str | bytes
) -> None:
    raw = headers(remote_user="ola", remote_email=email)
    identity = read_identity_headers(raw, proxy_settings(tmp_path)).identity
    assert identity is not None
    assert identity.email is None and not identity.email_verified


@pytest.mark.parametrize(
    ("raw", "display_name"),
    [
        ({"remote_name": "  Ola   Nowak "}, "Ola Nowak"),
        ({"remote_name": "Żaneta Łęcka"}, "Żaneta Łęcka"),  # UTF-8 bytes are decoded
        ({"remote_name": "x" * 200}, "x" * 120),
        ({"remote_name": "Bad\x07Name"}, "ola"),  # control characters: the subject instead
        ({"remote_name": "Line\u2028Break"}, "ola"),
        ({"remote_name": "Private\ue000"}, "ola"),
        ({"remote_name": b"\xff"}, "ola"),
        ({}, "ola"),
    ],
)
def test_display_name_falls_back_to_the_subject(
    tmp_path: Path, raw: dict[str, Any], display_name: str
) -> None:
    parsed = read_identity_headers(headers(remote_user="ola", **raw), proxy_settings(tmp_path))
    assert parsed.identity is not None and parsed.identity.display_name == display_name


def test_without_a_name_header_setting_the_name_header_is_ignored(tmp_path: Path) -> None:
    settings = proxy_settings(tmp_path, proxy_auth_name_header=None)
    raw = headers(remote_user="ola", remote_name="Someone Else")
    parsed = read_identity_headers(raw, settings)
    assert parsed.identity is not None and parsed.identity.display_name == "ola"


def test_groups_are_never_read(tmp_path: Path) -> None:
    raw = headers(remote_user="ola", remote_groups="admins", remote_email="ola@example.com")
    parsed = read_identity_headers(raw, proxy_settings(tmp_path))
    assert parsed.identity is not None and parsed.identity.groups is None


def test_identity_header_names_lists_present_headers_only(tmp_path: Path) -> None:
    settings = proxy_settings(tmp_path)
    raw = headers(remote_user="admin", remote_email="", remote_name="Admin", cookie="x=1")
    assert identity_header_names(raw, settings) == ["remote-user", "remote-name"]
    assert identity_header_names(headers(cookie="x=1"), settings) == []


# Peer capture: PeerMiddleware keeps forwarded-header behaviour exactly as before


def _call(app: Any, scope: dict[str, Any]) -> dict[str, Any]:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(_message: Any) -> None:
        return None

    asyncio.run(app(scope, receive, send))
    return scope


async def _recorder(scope: Scope, _receive: Receive, _send: Send) -> None:
    request = Request(scope)
    host = request.client.host if request.client else None
    scope["seen"] = (
        host,
        request.client.port if request.client else None,
        request.url.scheme,
        client_ip_prefix(host, for_throttle=True),
        scope.get(PEER_SCOPE_KEY),
    )


def _before_f14(trusted: list[str]) -> Any:
    """The app as uvicorn ran it before F1.4: its own forwarded-header handling."""
    options: dict[str, Any] = (
        {"proxy_headers": True, "forwarded_allow_ips": ",".join(trusted)}
        if trusted
        else {"proxy_headers": False, "forwarded_allow_ips": ""}
    )
    config = uvicorn.Config(_recorder, log_config=None, **options)
    config.load()
    return config.loaded_app


def _with_f14(settings: Settings) -> Any:
    config = uvicorn.Config(
        serve_app(_recorder, settings), log_config=None, **server_options(settings)
    )
    config.load()
    return config.loaded_app


TRUSTED_LISTS = [[], ["127.0.0.1"], ["127.0.0.1", "10.20.0.0/16"], ["::1"], ["10.20.0.5"]]
PEERS = [
    ("127.0.0.1", 50000),
    ("10.20.3.4", 50001),
    ("10.20.0.5", 50002),
    ("203.0.113.5", 50003),
    ("::1", 50004),
    None,
]
FORWARDED = [
    [],
    [(b"x-forwarded-for", b"198.51.100.7")],
    [(b"x-forwarded-for", b"198.51.100.7, 10.20.0.9")],
    [(b"x-forwarded-for", b"10.20.0.9, 198.51.100.7")],
    [(b"x-forwarded-for", b"198.51.100.7"), (b"x-forwarded-for", b"192.0.2.4")],
    [(b"x-forwarded-for", b"[2001:db8::7]:4711")],
    [(b"x-forwarded-for", b"")],
    [(b"x-forwarded-proto", b"https")],
    [(b"x-forwarded-proto", b"ftp"), (b"x-forwarded-for", b"198.51.100.7")],
]


@pytest.mark.parametrize("trusted", TRUSTED_LISTS)
def test_forwarded_header_behaviour_is_unchanged(tmp_path: Path, trusted: list[str]) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, trusted_proxies=",".join(trusted))
    before, after = _before_f14(trusted), _with_f14(settings)
    rewritten = 0
    for peer, forwarded in itertools.product(PEERS, FORWARDED):

        def scope(peer: tuple[str, int] | None = peer, extra: Any = forwarded) -> dict[str, Any]:
            return {
                "type": "http",
                "scheme": "http",
                "path": "/",
                "headers": [(b"host", b"money.example.test"), *extra],
                "client": peer,
            }

        old, new = _call(before, scope())["seen"], _call(after, scope())["seen"]
        # Client address, port, scheme and throttle key are identical; only the peer is new.
        assert new[:4] == old[:4], (trusted, peer, forwarded)
        assert old[4] is None
        assert new[4] == (peer[0] if peer else None)
        rewritten += old[:3] != ((peer[0], peer[1]) if peer else (None, None)) + ("http",)
    # The cases exercise the rewrite whenever a proxy is trusted, and never otherwise.
    assert (rewritten > 0) == bool(trusted)


def test_the_peer_is_recorded_before_forwarded_headers_rewrite_the_client(
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, trusted_proxies="10.20.0.5")
    app = serve_app(_recorder, settings)
    seen = _call(
        app,
        {
            "type": "http",
            "scheme": "http",
            "path": "/",
            "headers": [
                (b"host", b"money.example.test"),
                (b"x-forwarded-for", b"198.51.100.7"),
                (b"x-forwarded-proto", b"https"),
            ],
            "client": ("10.20.0.5", 4000),
        },
    )["seen"]
    assert seen == ("198.51.100.7", 0, "https", "198.51.100.7/32", "10.20.0.5")


def test_peer_middleware_passes_lifespan_through_untouched() -> None:
    calls: list[dict[str, Any]] = []

    async def app(scope: Scope, _receive: Receive, _send: Send) -> None:
        calls.append(dict(scope))

    _call(PeerMiddleware(app, ["127.0.0.1"]), {"type": "lifespan"})
    assert calls == [{"type": "lifespan"}]


def test_uvicorn_proxy_headers_middleware_contract() -> None:
    """Fails if uvicorn moves or changes the class PeerMiddleware relies on (ADR-0015)."""
    # Same class uvicorn's own configuration uses.
    config_class = uvicorn.config.ProxyHeadersMiddleware  # pyright: ignore[reportPrivateImportUsage]
    assert config_class is ProxyHeadersMiddleware
    parameters = inspect.signature(ProxyHeadersMiddleware.__init__).parameters
    assert list(parameters) == ["self", "app", "trusted_hosts"]

    seen: dict[str, Any] = {}

    async def app(scope: Any, _receive: Any, _send: Any) -> None:
        seen.update(client=scope["client"], scheme=scope["scheme"])

    middleware = ProxyHeadersMiddleware(app, trusted_hosts="10.0.0.0/24,127.0.0.1")
    scope = {
        "type": "http",
        "scheme": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.9"), (b"x-forwarded-proto", b"https")],
        "client": ("10.0.0.7", 1234),
    }
    _call(middleware, scope)
    assert seen == {"client": ("203.0.113.9", 0), "scheme": "https"}


# The web helper reads only the recorded peer


def _request(settings: Settings, logger: logging.Logger, **scope: Any) -> Request:
    state = SimpleNamespace(settings=settings, logger=logger)
    return Request({"type": "http", "headers": [], "app": SimpleNamespace(state=state), **scope})


def test_proxy_peer_trusted_reads_the_recorded_peer_not_the_client(tmp_path: Path) -> None:
    settings = proxy_settings(tmp_path)
    logger = logging.getLogger("test.proxy_auth")
    trusted = {PEER_SCOPE_KEY: "172.20.0.2", "client": ("198.51.100.7", 0)}
    assert proxy_peer_trusted(_request(settings, logger, **trusted))
    # A client address rewritten to a trusted one changes nothing.
    spoofed = {PEER_SCOPE_KEY: "198.51.100.7", "client": ("172.20.0.2", 0)}
    assert not proxy_peer_trusted(_request(settings, logger, **spoofed))
    assert not proxy_peer_trusted(_request(settings, logger, **{PEER_SCOPE_KEY: None}))
    assert proxy_peer_trusted(_request(settings, logger, **{PEER_SCOPE_KEY: "fd00:20::2"}))


def test_proxy_peer_trusted_is_false_while_proxy_auth_is_off(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, trusted_proxies="172.20.0.2")
    request = _request(
        settings, logging.getLogger("test.proxy_auth"), **{PEER_SCOPE_KEY: "172.20.0.2"}
    )
    assert not proxy_peer_trusted(request)


def test_a_missing_peer_record_trusts_nothing_and_logs_one_error(tmp_path: Path) -> None:
    settings = proxy_settings(tmp_path)
    logger = logging.getLogger("test.proxy_auth.missing")
    handler = Records()
    logger.addHandler(handler)
    state = SimpleNamespace(settings=settings, logger=logger)
    app = SimpleNamespace(state=state)
    try:
        for _ in range(3):
            request = Request(
                {"type": "http", "headers": [], "app": app, "client": ("172.20.0.2", 1)}
            )
            assert not proxy_peer_trusted(request)
    finally:
        logger.removeHandler(handler)
    errors = handler.at(logging.ERROR)
    assert len(errors) == 1
    assert "serve" in errors[0]


# Startup


def test_startup_warns_about_every_trusted_proxy_wider_than_one_address(tmp_path: Path) -> None:
    settings = proxy_settings(
        tmp_path, trusted_proxies="172.20.0.2,172.20.1.0/24,fd00:20::/64", secret_key="k"
    )
    with app_log() as log:
        create_app(settings)
    warnings = [line for line in log.at(logging.WARNING) if "TRUSTED_PROXIES" in line]
    assert len(warnings) == 2
    assert "172.20.1.0/24" in warnings[0] and "fd00:20::/64" in warnings[1]
    assert not any("172.20.0.2 " in line for line in warnings)


def test_startup_is_quiet_about_trusted_proxies_without_proxy_auth(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, trusted_proxies="10.0.0.0/8", secret_key="k"
    )
    with app_log() as log:
        create_app(settings)
    assert not [line for line in log.at(logging.WARNING) if "TRUSTED_PROXIES" in line]
