from __future__ import annotations

import json
import socket
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .utils import DATA_DIR, now_iso, read_json, sha1_hex, write_json


CACHE_DIR = DATA_DIR / "cache" / "http"

# Retry policy for transient network failures. Backoffs in seconds between
# attempts; the length of this tuple is also the number of *retries* (the
# initial attempt is in addition to these). Keep it small to avoid stalling
# the trading loop on a truly dead route.
_RETRY_BACKOFFS: tuple[float, ...] = (0.4, 1.0)

# Substrings that indicate a low-level SSL/TLS handshake failure where the
# request body has not been delivered to the server yet. Retrying these is
# safe even for write operations (POST/PUT/DELETE) such as live order
# placement, because the server never saw the request.
_RETRYABLE_SSL_HINTS: tuple[str, ...] = (
    "EOF occurred in violation of protocol",
    "UNEXPECTED_EOF_WHILE_READING",
    "WRONG_VERSION_NUMBER",
    "decryption failed or bad record mac",
    "TLSV1_ALERT_INTERNAL_ERROR",
    "SSLV3_ALERT_HANDSHAKE_FAILURE",
)


class HttpRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _cache_path(namespace: str, url: str) -> Path:
    return CACHE_DIR / namespace / f"{sha1_hex(url)}.json"


def _cache_payload(path: Path, payload: Any, ttl_seconds: int, max_stale_seconds: int) -> None:
    now_ms = int(__import__("time").time() * 1000)
    write_json(
        path,
        {
            "fetchedAt": now_iso(),
            "fetchedAtMs": now_ms,
            "expiresAtMs": now_ms + max(1, ttl_seconds) * 1000,
            "staleUntilMs": now_ms + max(ttl_seconds, max_stale_seconds) * 1000,
            "payload": payload,
        },
    )


def _cache_is_fresh(cache: dict[str, Any] | None) -> bool:
    if not cache:
        return False
    return int(cache.get("expiresAtMs") or 0) > int(__import__("time").time() * 1000)


def _cache_is_usable(cache: dict[str, Any] | None) -> bool:
    if not cache:
        return False
    return int(cache.get("staleUntilMs") or 0) > int(__import__("time").time() * 1000)


def _should_bypass_proxy(hostname: str, network_settings: dict[str, Any]) -> bool:
    no_proxy = [item.lower() for item in network_settings.get("noProxy", [])]
    host = (hostname or "").lower()
    return any(host == item or host.endswith(f".{item}") for item in no_proxy)


def _build_opener(url: str, network_settings: dict[str, Any] | None):
    if not network_settings:
        return build_opener()
    parsed = urlparse(url)
    if (
        not network_settings.get("proxyEnabled")
        or not network_settings.get("proxyUrl")
        or _should_bypass_proxy(parsed.hostname or "", network_settings)
    ):
        # Explicitly install an empty ProxyHandler so urllib does not silently
        # fall back to HTTP(S)_PROXY environment variables when this request
        # is configured to bypass the local proxy.
        return build_opener(ProxyHandler({}))
    proxy_url = str(network_settings.get("proxyUrl") or "").strip()
    scheme = parsed.scheme.lower()
    proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }
    if scheme in {"http", "https"}:
        return build_opener(ProxyHandler(proxies))
    return build_opener()


def _is_retryable_url_error(error: URLError, method_upper: str) -> bool:
    """Decide whether a URLError is safe and worthwhile to retry.

    SSL handshake failures are always retryable — the request body never
    reached the server, so even non-idempotent calls (live order placement)
    cannot have been duplicated. For other transport-level failures
    (timeouts, connection resets) we restrict retries to GET because GET is
    the only method we can assume idempotent in this codebase.
    """
    reason = error.reason
    if isinstance(reason, ssl.SSLError):
        if isinstance(reason, ssl.SSLEOFError):
            return True
        message = str(reason)
        return any(hint in message for hint in _RETRYABLE_SSL_HINTS)
    if method_upper != "GET":
        return False
    if isinstance(
        reason,
        (TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.timeout),
    ):
        return True
    if isinstance(reason, OSError):
        # ECONNRESET on macOS is errno 54, on Linux 104. Either way the
        # remote killed the socket before any response, so a GET retry is
        # safe.
        if reason.errno in {54, 104}:
            return True
    return False


def request_text(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout_seconds: int = 45,
    network_settings: dict[str, Any] | None = None,
) -> str:
    body: bytes | None
    if payload is None:
        body = None
    elif isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload).encode("utf-8")
    method_upper = method.upper()
    request = Request(url=url, method=method_upper, data=body)
    merged_headers = {
        "accept": "application/json",
        "user-agent": "python-trading-agent/1.0",
    }
    if body is not None and "content-type" not in {key.lower() for key in (headers or {})}:
        merged_headers["content-type"] = "application/json"
    merged_headers.update(headers or {})
    for key, value in merged_headers.items():
        request.add_header(key, value)
    opener = _build_opener(url, network_settings or {})

    # Initial attempt + entries in _RETRY_BACKOFFS as retries. We sleep
    # *before* each retry (not before the first attempt).
    last_url_error: URLError | None = None
    for attempt_index in range(len(_RETRY_BACKOFFS) + 1):
        if attempt_index > 0:
            time.sleep(_RETRY_BACKOFFS[attempt_index - 1])
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            # The server actually responded (4xx/5xx). Never retry here —
            # for write operations like order placement a retry could
            # double-submit, and for reads the same input will deterministically
            # produce the same status.
            detail = error.read().decode("utf-8", errors="replace")
            retry_after = error.headers.get("Retry-After") if error.headers else None
            raise HttpRequestError(
                f"{error.code} {error.reason}: {detail}",
                status_code=error.code,
                retry_after=retry_after,
            ) from error
        except URLError as error:
            last_url_error = error
            has_more_attempts = attempt_index < len(_RETRY_BACKOFFS)
            if has_more_attempts and _is_retryable_url_error(error, method_upper):
                continue
            raise HttpRequestError(str(error.reason)) from error

    # Defensive: the loop above always returns or raises, but mypy/readers
    # appreciate an explicit terminal raise.
    assert last_url_error is not None
    raise HttpRequestError(str(last_url_error.reason)) from last_url_error


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout_seconds: int = 45,
    network_settings: dict[str, Any] | None = None,
) -> Any:
    text = request_text(
        method,
        url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        network_settings=network_settings,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        snippet = text[:220].replace("\n", " ").strip()
        if len(text) > 220:
            snippet += "..."
        message = f"invalid JSON response from {url}: {error}"
        if snippet:
            message += f" | response starts with: {snippet}"
        raise HttpRequestError(message) from error


def cached_get_json(
    url: str,
    *,
    namespace: str = "generic",
    ttl_seconds: int = 60,
    max_stale_seconds: int = 3600,
    timeout_seconds: int = 45,
    headers: dict[str, str] | None = None,
    network_settings: dict[str, Any] | None = None,
    prefer_cache: bool = False,
    allow_network: bool = True,
) -> Any:
    path = _cache_path(namespace, url)
    cache = read_json(path, {})
    if _cache_is_fresh(cache):
        return cache.get("payload")
    if prefer_cache and _cache_is_usable(cache):
        return cache.get("payload")
    if not allow_network:
        raise HttpRequestError("cached response unavailable while exchange API cooldown is active")
    try:
        payload = request_json(
            "GET",
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            network_settings=network_settings,
        )
        _cache_payload(path, payload, ttl_seconds, max_stale_seconds)
        return payload
    except Exception:
        if _cache_is_usable(cache):
            return cache.get("payload")
        raise
