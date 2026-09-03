"""Network-address parsing shared by graphical and command-line entry points."""

from __future__ import annotations

from ipaddress import IPv4Address


def normalize_ipv4(value: str) -> str:
    """Return a canonical IPv4 address, accepting surrounding whitespace."""
    return str(IPv4Address(value.strip()))
