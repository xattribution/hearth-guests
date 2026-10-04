"""LAN address check for guest endpoints.

Pure Python, no Home Assistant imports. The Nabu Casa cloud check needs Home Assistant and
lives in http.py; this module only classifies the remote address.
"""

from __future__ import annotations

import ipaddress


def is_lan_address(remote: str | None) -> bool:
    """True for private, link-local and loopback addresses.

    Unparseable or missing addresses are treated as not on the LAN. IPv4-mapped IPv6
    addresses are judged by their IPv4 address, and IPv6 zone ids ("%eth0") are ignored.
    """
    if not remote:
        return False
    host = remote.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_unspecified or address.is_multicast:
        return False
    return address.is_private or address.is_link_local or address.is_loopback
