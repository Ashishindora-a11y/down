"""Input URL validation and SSRF hardening.

This service fetches whatever URL a caller supplies, which is exactly the shape
of a Server-Side Request Forgery primitive. On App Runner / Cloud Run / EC2 an
unguarded fetcher can be pointed at the instance metadata endpoint
(169.254.169.254) or at internal VPC services. So before yt-dlp ever sees a URL
we require http(s), a public port, and a hostname that resolves only to public
unicast addresses.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from .errors import ApiError, bad_request

_ALLOWED_SCHEMES = {"http", "https"}
_ALLOWED_PORTS = {80, 443}
MAX_URL_LENGTH = 2048

# Hostnames that must never be fetched regardless of DNS answers.
_HOST_DENYLIST = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}

_DENIED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".cluster.local",
)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # IPv4-mapped / translated IPv6 can smuggle a private v4 address.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or getattr(ip, "sixtofour", None)
        if mapped is not None and not _is_public(mapped):
            return False
        if ip.teredo is not None:
            server, client = ip.teredo
            if not _is_public(server) or not _is_public(client):
                return False
    return True


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ApiError(
            400,
            "unresolvable_host",
            f"Could not resolve host {host!r}.",
            hint=str(exc),
        ) from exc
    except UnicodeError as exc:
        raise bad_request(f"Invalid hostname {host!r}.") from exc

    addresses = []
    for info in infos:
        sockaddr = info[4]
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return addresses


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    """Match a host against domain patterns; a pattern also matches subdomains."""
    host = host.lower().rstrip(".")
    for pattern in patterns:
        needle = pattern.lower().lstrip("*.").rstrip(".")
        if not needle:
            continue
        if host == needle or host.endswith("." + needle):
            return True
    return False


def validate(
    raw: str,
    *,
    guard: bool = True,
    allowed_domains: tuple[str, ...] = (),
    blocked_domains: tuple[str, ...] = (),
) -> str:
    """Return a normalised URL or raise ApiError."""
    if not raw or not raw.strip():
        raise bad_request("A 'url' value is required.")

    candidate = raw.strip()
    if len(candidate) > MAX_URL_LENGTH:
        raise bad_request(f"URL exceeds {MAX_URL_LENGTH} characters.")

    # Be forgiving about a pasted link with no scheme.
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise bad_request(f"Malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise bad_request(
            f"Scheme {scheme!r} is not allowed.",
            hint="Only http and https URLs can be fetched.",
        )

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise bad_request("URL has no hostname.")

    try:
        port = parts.port
    except ValueError as exc:
        raise bad_request(f"Invalid port in URL: {exc}") from exc

    if parts.username or parts.password:
        raise bad_request("Credentials embedded in the URL are not allowed.")

    if blocked_domains and _host_matches(host, blocked_domains):
        raise ApiError(403, "domain_blocked", f"Host {host!r} is blocked by BLOCKED_DOMAINS.")

    if allowed_domains and not _host_matches(host, allowed_domains):
        raise ApiError(
            403,
            "domain_not_allowed",
            f"Host {host!r} is not in ALLOWED_DOMAINS.",
            hint="Add it to ALLOWED_DOMAINS, or clear that variable to allow every site.",
        )

    normalised = urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))

    # Scheme, credential and domain-list checks above are unconditional. What
    # follows is the SSRF guard proper: the port allowlist and the public-address
    # requirement. Turning the guard off is how an operator opts into reaching a
    # media server on an internal address or a non-standard port, accepting the
    # risk that comes with it.
    if not guard:
        return normalised

    if port is not None and port not in _ALLOWED_PORTS:
        raise bad_request(
            f"Port {port} is not allowed.",
            hint=(
                "Only the standard web ports 80 and 443 are permitted. Set "
                "SSRF_GUARD=false to lift this, but understand that it also "
                "allows requests to internal addresses."
            ),
        )

    if host in _HOST_DENYLIST or host.endswith(_DENIED_SUFFIXES):
        raise ApiError(403, "host_not_allowed", f"Host {host!r} is not a public internet host.")

    # A bare IP literal, or a hostname whose DNS answer is internal.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public(literal):
            raise ApiError(
                403,
                "host_not_allowed",
                f"Address {host} is not a public internet address.",
            )
        return normalised

    addresses = _resolve(host)
    if not addresses:
        raise ApiError(400, "unresolvable_host", f"Host {host!r} resolved to no addresses.")

    for address in addresses:
        if not _is_public(address):
            raise ApiError(
                403,
                "host_not_allowed",
                f"Host {host!r} resolves to the non-public address {address}.",
                hint="Blocked to prevent access to cloud metadata and internal services.",
            )

    return normalised
