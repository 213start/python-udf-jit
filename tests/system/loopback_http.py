from __future__ import annotations

import ipaddress
import ssl
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class LoopbackRedirectError(RuntimeError):
    """An authenticated loopback request attempted to redirect."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise LoopbackRedirectError(f"redirect_refused:{code}")


def require_numeric_loopback(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("loopback_http_url_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise ValueError("loopback_http_host_invalid") from error
    if not address.is_loopback:
        raise ValueError("loopback_http_host_not_loopback")


def loopback_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
):
    """Open one no-proxy, no-redirect request to a numeric loopback address."""

    require_numeric_loopback(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )
    return opener.open(request, timeout=timeout)
