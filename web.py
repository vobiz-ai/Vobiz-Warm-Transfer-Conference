"""web.py — request parsing and XML replies, shared by both flows."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

import store


async def call_params(request: Request) -> dict:
    """
    Every Vobiz webhook is `application/x-www-form-urlencoded`, never JSON, and
    the answer/callback URLs also carry our own query parameters. The mock
    harness sends JSON. Merge all three.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            params.update({k: str(v) for k, v in form.items()})
        except Exception:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    params.update({k: str(v) for k, v in body.items()})
            except Exception:
                pass
    return params


def xml_reply(document: str, tid: str = "", note: str = "") -> Response:
    """Return an XML document and keep a copy of exactly what was sent."""
    store.record("xml_reply", {"tid": tid, "note": note, "xml": document})
    return Response(content=document, media_type="application/xml")
