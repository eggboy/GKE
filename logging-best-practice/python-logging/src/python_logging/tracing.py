"""Keep query-string values out of the server span.

The OpenTelemetry instrumentation records the URL of every request with its query string (``http.url``, or
``url.query`` with the stable HTTP conventions) and redacts only a fixed list of signature parameters. A request to
``/orders?access_token=...`` therefore lands in Cloud Trace as typed, the same way it would land in an access log.
The middleware here runs inside the server span and writes those attributes again: every parameter keeps its name
and loses its value.
"""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from opentelemetry import trace

REDACTED = "REDACTED"

# The attributes that can hold the query string, in the old and in the stable HTTP semantic conventions.
_QUERY_ATTRIBUTE = "url.query"
_URL_ATTRIBUTES = ("http.url", "http.target", "url.full")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class SpanQueryRedactionMiddleware:
    """ASGI middleware that replaces the query-string values on the current span with ``REDACTED``.

    Add it with ``app.add_middleware(SpanQueryRedactionMiddleware)``. The OpenTelemetry middleware wraps the whole
    Starlette stack, so this one runs inside the server span, for every request that reaches the application, a 404
    included. A request the instrumentation excludes has no recording span, and nothing happens.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the next ASGI application."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Redact before the request is handled, so that the attribute is right however the request ends."""
        if scope["type"] in ("http", "websocket") and scope.get("query_string"):
            redact_query_on_span(trace.get_current_span())
        await self._app(scope, receive, send)


def redact_query_on_span(span: trace.Span) -> None:
    """Rewrite the URL attributes the span has: parameter names stay, their values become ``REDACTED``."""
    # The API has no getter for attributes; a span of the SDK exposes what was set on it.
    attributes = getattr(span, "attributes", None)
    if not span.is_recording() or not attributes:
        return
    query = attributes.get(_QUERY_ATTRIBUTE)
    if isinstance(query, str):
        span.set_attribute(_QUERY_ATTRIBUTE, redact_query(query))
    for key in _URL_ATTRIBUTES:
        url = attributes.get(key)
        if isinstance(url, str) and "?" in url:
            head, _, query = url.partition("?")
            span.set_attribute(key, f"{head}?{redact_query(query)}")


def redact_query(query: str) -> str:
    """Turn ``a=1&b=2`` into ``a=REDACTED&b=REDACTED``; a part without a name is redacted whole."""
    return "&".join(_redact_pair(pair) for pair in query.split("&"))


def _redact_pair(pair: str) -> str:
    name, equals, _ = pair.partition("=")
    if not pair:
        return pair
    return f"{name}={REDACTED}" if equals else REDACTED
