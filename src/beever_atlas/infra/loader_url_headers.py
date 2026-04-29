"""Middleware that hardens responses to URLs carrying ``?access_token=``.

Issue #35 — the frontend's ``buildLoaderUrl`` appends ``?access_token=<key>``
to URLs consumed by browser-native loaders (``<img src>``, ``<a href>``)
that cannot set custom Authorization headers. This is intentional, but it
means the API key can leak into:

  * server access logs (uvicorn, load balancers, CDNs)
  * browser history (the URL is bookmarkable)
  * the ``Referer`` header sent on outbound navigation

The Referer leak is the most serious of the three. This middleware adds
``Referrer-Policy: no-referrer`` and ``Cache-Control: no-store`` to every
response whose request carried ``?access_token=`` in the query string.
Endpoints that already set these headers explicitly (e.g. the public share
GET) are unaffected — we use ``setdefault`` to avoid overriding intentional
``Cache-Control: private, no-store`` overrides.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class LoaderUrlSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add ``Referrer-Policy`` + ``Cache-Control`` when ``?access_token=``
    appears on the request URL.

    Implementation note: the middleware checks ``request.query_params``
    rather than scanning ``request.url.query`` so URL-encoded variants
    like ``%61ccess_token`` are not matched (they wouldn't be parsed
    by FastAPI's ``Query`` dependency either, so they wouldn't have
    authenticated; protecting them would be defense-in-depth but is not
    required for the fix).
    """

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next,
    ) -> Response:
        response = await call_next(request)
        if "access_token" in request.query_params:
            # `setdefault` preserves explicit overrides like the public
            # share endpoint's `Cache-Control: private, no-store`.
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Cache-Control", "no-store")
        return response
