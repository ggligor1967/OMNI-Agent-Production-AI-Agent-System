"""Central SSRF protection for outbound user-supplied URLs."""
import ipaddress
import socket
from typing import Callable, List
from urllib.parse import urlparse

BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data",
}
BLOCKED_IP_LITERALS = {
    "169.254.169.254",  # AWS/Azure metadata
    "169.254.170.2",    # ECS task metadata
    "100.100.100.200",  # Alibaba metadata
}


class SSRFProtectionError(ValueError):
    """Raised when an outbound URL is rejected by the SSRF guard."""


Resolver = Callable[[str], List[str]]


def resolve_host_ips(hostname: str) -> List[str]:
    resolved = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP):
        if family == socket.AF_INET:
            resolved.append(sockaddr[0])
        elif family == socket.AF_INET6:
            resolved.append(sockaddr[0])
    return list(dict.fromkeys(resolved))


def _is_blocked_ip(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return any([
        ip.is_loopback,
        ip.is_private,
        ip.is_link_local,
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,
    ])


def ensure_public_http_url(url: str, resolver: Resolver = resolve_host_ips) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SSRFProtectionError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise SSRFProtectionError("URL must include a hostname")

    hostname = parsed.hostname.strip("[]").lower()
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise SSRFProtectionError("Blocked outbound URL: localhost and metadata hosts are not allowed")
    if hostname in BLOCKED_IP_LITERALS:
        raise SSRFProtectionError("Blocked outbound URL: metadata endpoints are not allowed")

    try:
        ipaddress.ip_address(hostname)
        resolved_ips = [hostname]
    except ValueError:
        try:
            resolved_ips = resolver(hostname)
        except OSError as exc:
            raise SSRFProtectionError(f"DNS resolution failed for '{hostname}': {exc}") from exc

    if not resolved_ips:
        raise SSRFProtectionError(f"DNS resolution returned no addresses for '{hostname}'")

    for resolved_ip in resolved_ips:
        if resolved_ip in BLOCKED_IP_LITERALS or _is_blocked_ip(resolved_ip):
            raise SSRFProtectionError(
                f"Blocked outbound URL: '{hostname}' resolves to a non-public address ({resolved_ip})"
            )

    return url
