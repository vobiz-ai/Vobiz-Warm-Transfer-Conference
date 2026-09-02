"""
runtime.py — public URL resolution and URL builders
====================================================

Separate from `app.py` for the same reason `store.py` is: `app.py` runs as
`__main__`, so importing it from a flow module would create a second copy with
its own empty globals. Anything both the server and the flows need lives here.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

HTTP_PORT = int(os.getenv("HTTP_PORT", "8100"))
RUNTIME_FILE = BASE_DIR / ".runtime.json"

logger = logging.getLogger("runtime")

_public_url = os.getenv("PUBLIC_URL", "").rstrip("/")


def public_url() -> str:
    return _public_url


def set_public_url(value: str):
    global _public_url
    _public_url = value.rstrip("/")
    try:
        RUNTIME_FILE.write_text(
            json.dumps({"public_url": _public_url, "port": HTTP_PORT}, indent=2)
        )
    except OSError:
        pass


def load_public_url() -> str:
    """For the CLIs, which run in a different process from the server."""
    if _public_url:
        return _public_url
    if RUNTIME_FILE.exists():
        try:
            return json.loads(RUNTIME_FILE.read_text()).get("public_url", "").rstrip("/")
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def url(path: str, **params) -> str:
    """
    Absolute HTTPS URL for a callback.

    Query strings are built with urlencode rather than f-strings, because a bare
    `&` between two parameters is invalid inside an XML attribute and kills the
    call with `Invalid Answer XML` a second after it answers.
    """
    base = f"{public_url()}/{path.lstrip('/')}"
    return f"{base}?{urlencode(params)}" if params else base


def ws_url(path: str, **params) -> str:
    base = public_url()
    scheme = "wss://" if base.startswith("https://") else "ws://"
    host = base.split("://", 1)[-1]
    out = f"{scheme}{host}/{path.lstrip('/')}"
    return f"{out}?{urlencode(params)}" if params else out


def start_ngrok() -> str:
    """Open a tunnel when PUBLIC_URL is not set. Returns the public URL."""
    from pyngrok import conf, ngrok

    token = os.getenv("NGROK_AUTH_TOKEN", "")
    if token:
        conf.get_default().auth_token = token
    domain = os.getenv("NGROK_DOMAIN", "")
    options = {"bind_tls": True}
    if domain:
        options["domain"] = domain
    tunnel = ngrok.connect(HTTP_PORT, "http", **options)
    return tunnel.public_url.replace("http://", "https://")
