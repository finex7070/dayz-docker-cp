"""Trusted reverse proxy handling.

Werkzeug's ProxyFix only counts hops: it rewrites the request from
X-Forwarded-* headers without ever checking who sent them. That is fine behind a
fixed proxy chain, but it means the panel would believe forged headers from any
client that reaches it directly.

This middleware instead trusts the *peer*: forwarded headers are honoured only
when the immediate connection comes from an address in TRUSTED_PROXY_IPS.
Anything else is served with its real connection data, so a forged
X-Forwarded-For cannot bypass the login rate limit and a forged
X-Forwarded-Proto cannot make a plaintext request look like HTTPS.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Iterable

log = logging.getLogger(__name__)

# Accepted as the whole value of TRUSTED_PROXY_IPS to trust every peer. Only
# safe when nothing but the proxy can reach the panel's port.
TRUST_ALL = {"*", "any", "all"}

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_trusted(entries: Iterable[str]) -> tuple[list[IPNetwork], bool]:
    """Turn configured entries into networks. Raises ValueError on bad input.

    Accepts single addresses ("10.1.2.3") and CIDR blocks ("172.16.0.0/12"),
    IPv4 and IPv6 alike.
    """
    networks: list[IPNetwork] = []
    trust_all = False

    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        if entry.lower() in TRUST_ALL:
            trust_all = True
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"TRUSTED_PROXY_IPS: {entry!r} is neither an IP address nor a CIDR block ({exc})"
            ) from None

    return networks, trust_all


def _to_address(text: str) -> IPAddress | None:
    """Parse an address that may carry a port or IPv6 brackets.

    Proxies write entries in several shapes: "1.2.3.4", "1.2.3.4:5678",
    "[2001:db8::1]:443", "2001:db8::1" and IPv4-mapped IPv6 such as
    "::ffff:1.2.3.4".
    """
    value = text.strip()
    if not value:
        return None

    if value.startswith("["):
        value = value.partition("]")[0].lstrip("[")
    elif value.count(":") == 1:
        # Exactly one colon means host:port; more colons mean bare IPv6.
        value = value.split(":", 1)[0]

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None

    # Normalise ::ffff:10.0.0.1 to 10.0.0.1 so it can be matched against
    # IPv4 networks - Docker hands out such addresses on dual-stack setups.
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped

    return address


class TrustedProxyFix:
    """WSGI middleware rewriting the request from headers of a trusted peer."""

    def __init__(self, app, networks: list[IPNetwork], trust_all: bool = False) -> None:
        self.app = app
        self.networks = networks
        self.trust_all = trust_all

    @property
    def enabled(self) -> bool:
        return self.trust_all or bool(self.networks)

    def is_trusted(self, text: str | None) -> bool:
        if self.trust_all:
            return True
        if not text:
            return False
        address = _to_address(text)
        if address is None:
            return False
        return any(address in network for network in self.networks)

    def client_from_chain(self, forwarded_for: str) -> str | None:
        """Pick the real client out of an X-Forwarded-For chain.

        The chain reads left to right, client first, each proxy appending the
        peer it saw. Walking it from the right and skipping trusted proxies
        yields the last address the trusted chain can vouch for. Taking the
        leftmost entry instead would be wrong: that part is written by the
        client and can say anything.
        """
        parts = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if not parts:
            return None

        for candidate in reversed(parts):
            if not self.is_trusted(candidate):
                address = _to_address(candidate)
                return str(address) if address else None

        # Every hop is trusted, so the client itself sits inside a trusted
        # network - the leftmost entry is then genuine.
        address = _to_address(parts[0])
        return str(address) if address else None

    def __call__(self, environ, start_response):
        if not self.enabled or not self.is_trusted(environ.get("REMOTE_ADDR")):
            return self.app(environ, start_response)

        environ["panel.proxy.original_remote_addr"] = environ.get("REMOTE_ADDR")

        forwarded_for = environ.get("HTTP_X_FORWARDED_FOR")
        if forwarded_for:
            client = self.client_from_chain(forwarded_for)
            if client:
                environ["REMOTE_ADDR"] = client

        proto = _first_value(environ.get("HTTP_X_FORWARDED_PROTO"))
        if proto in {"http", "https"}:
            environ["wsgi.url_scheme"] = proto

        host = _first_value(environ.get("HTTP_X_FORWARDED_HOST"))
        if host:
            environ["HTTP_HOST"] = host

        port = _first_value(environ.get("HTTP_X_FORWARDED_PORT"))
        if port and port.isdigit():
            environ["SERVER_PORT"] = port

        return self.app(environ, start_response)


def _first_value(header: str | None) -> str | None:
    """Take the first entry of a comma separated header, lowercased/trimmed."""
    if not header:
        return None
    return header.split(",")[0].strip().lower() or None
