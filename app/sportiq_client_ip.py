"""The visitor's IP address, for per-IP guards (vote dedupe, rate limits, lockouts).

Production path (checked 2026-11-25 with GET only): billnojay.com and
*.onrender.com both resolve to Render, and both answer through Render's own
Cloudflare edge (Server: cloudflare, CF-RAY, rndr-id). The owner has no
Cloudflare zone of their own; DNS is at GoDaddy. So a request goes
visitor -> Cloudflare (Render's) -> Render's load balancer -> uvicorn.

uvicorn runs without --forwarded-allow-ips, so request.client.host is the
load balancer's internal address and is the same for every visitor.

Order:
1. CF-Connecting-IP, only on Render (RENDER env var, set by Render on every
   service) or when SPORTIQ_TRUST_CF_CONNECTING_IP=1. Cloudflare sets it to
   the address that connected to its edge; a value a visitor sends is not
   what Cloudflare forwards. Off Render it is just a client header.
2. X-Forwarded-For read from the right, skipping hops that are proxies
   (Cloudflare's published ranges, private/internal addresses). The leftmost
   entries are whatever the client sent, so the first non-proxy entry from
   the right is the one a client cannot choose. This holds whether or not a
   proxy appended its own address after the visitor's.
3. The socket address.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import threading
from typing import Any, Optional

log = logging.getLogger("sportiq.client_ip")

# https://www.cloudflare.com/ips-v4 and /ips-v6
_CLOUDFLARE_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]

_LOGGED = [False]
_LOG_LOCK = threading.Lock()


def _parse(value: Any) -> Optional[ipaddress._BaseAddress]:
    s = str(value or "").strip().strip('"')
    if s.startswith("[") and "]" in s:          # [v6]:port
        s = s[1:s.index("]")]
    elif s.count(":") == 1:                     # v4:port
        s = s.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


def _is_proxy(ip: ipaddress._BaseAddress) -> bool:
    return (not ip.is_global) or any(ip in n for n in _CLOUDFLARE_NETS if n.version == ip.version)


def _trust_cf_header() -> bool:
    flag = (os.environ.get("SPORTIQ_TRUST_CF_CONNECTING_IP") or "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    return flag in ("1", "true", "yes") or bool(os.environ.get("RENDER"))


def _log_shape_once(headers: Any, xff: list, cf: Optional[str], chosen_from: str) -> None:
    """One line per process describing the header layout, without any address."""
    if _LOGGED[0]:
        return
    with _LOG_LOCK:
        if _LOGGED[0]:
            return
        _LOGGED[0] = True
    kinds = ",".join("proxy" if (p := _parse(v)) is None or _is_proxy(p) else "public" for v in xff)
    cf_ip = _parse(cf)
    log.warning(
        "client-ip header shape: cf-connecting-ip=%s true-client-ip=%s x-real-ip=%s xff=[%s] "
        "xff_last_public_equals_cf=%s chosen_from=%s",
        "yes" if cf else "no",
        "yes" if headers.get("true-client-ip") else "no",
        "yes" if headers.get("x-real-ip") else "no",
        kinds,
        (cf_ip is not None and any(_parse(v) == cf_ip for v in xff)),
        chosen_from,
    )


def client_ip(request: Any) -> str:
    if request is None:
        return "local"
    headers = getattr(request, "headers", None) or {}
    xff = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    cf = headers.get("cf-connecting-ip")

    chosen, source = None, "socket"
    if cf and _trust_cf_header():
        ip = _parse(cf)
        if ip is not None and ip.is_global:
            chosen, source = str(ip), "cf-connecting-ip"
    if chosen is None and xff:
        parsed = [_parse(v) for v in xff]
        for ip in reversed(parsed):
            if ip is not None and not _is_proxy(ip):
                chosen, source = str(ip), "xff"
                break
        if chosen is None:
            first = next((ip for ip in parsed if ip is not None), None)
            if first is not None:
                chosen, source = str(first), "xff-all-proxies"
    if chosen is None:
        client = getattr(request, "client", None)
        chosen = (client.host if client else None) or "local"

    try:
        _log_shape_once(headers, xff, cf, source)
    except Exception:
        pass
    return chosen[:64]
