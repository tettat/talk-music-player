"""Preserve empty response cookies without changing pinned SDK/library sources."""
from collections.abc import Mapping
from http.cookiejar import CookieJar
from types import MappingProxyType


def cookie_snapshot(jar):
    """Snapshot cookie values in memory; reject ambiguous names instead of guessing."""
    values = {}
    if isinstance(jar, CookieJar):
        # Requests-style __getitem__ raises KeyError for a cookie whose value is ''.
        # Iterate Cookie objects directly, retaining empty values and no raw logs.
        entries = ((cookie.name, cookie.value) for cookie in jar)
    elif isinstance(jar, Mapping):
        entries = jar.items()
    else:
        raise ValueError('PROVIDER_COOKIE_FORMAT_REJECTED')
    for name, value in entries:
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError('PROVIDER_COOKIE_FORMAT_REJECTED')
        if name in values and values[name] != value:
            raise ValueError('AMBIGUOUS_PROVIDER_COOKIES')
        values[name] = value
    return MappingProxyType(values)


class CookieResponse:
    """Delegate a buffered response while providing a normal immutable cookie mapping."""
    def __init__(self, response):
        self._response = response
        self.cookies = cookie_snapshot(response.cookies)

    def __getattr__(self, name):
        """Delegate non-cookie response attributes without copying their values."""
        return getattr(self._response, name)


def build_transport(**kwargs):
    """Build a pinned SDK transport that normalizes both single and batch responses."""
    from qqmusic_api.core.transport import NiquestsTransport

    class CompatibleTransport(NiquestsTransport):
        """Retain upstream timeouts, TLS verification and response ownership semantics."""
        async def request_many(self, requests, *, return_exceptions=True):
            """Wrap buffered responses only; preserve exception positions and policy."""
            results = await super().request_many(requests, return_exceptions=return_exceptions)
            normalized = []
            for result in results:
                if isinstance(result, Exception):
                    normalized.append(result)
                    continue
                try:
                    normalized.append(CookieResponse(result))
                except Exception as exc:
                    if not return_exceptions:
                        raise
                    normalized.append(exc)
            return normalized

    return CompatibleTransport(**kwargs)
