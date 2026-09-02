"""
vobiz.py — async Vobiz REST client for the warm-transfer flows
==============================================================

Everything here is called from inside the FastAPI event loop, usually while two
Gemini WebSockets are live, so it is httpx/async rather than the requests-based
client in ../1-testing.

Three groups of calls matter for a warm transfer:

  calls        place the customer and human-agent legs, hang one up, redirect one
  streams      attach / detach a media bug on a leg that is already in a room
  conference   per-member control — the only granularity the platform offers

Status codes are not what the docs say. Runtime, on live rooms:
  Mute / Deaf / Play / Kick / Speak  -> 202 Accepted
  Unmute / Undeaf / StopPlay         -> 204 No Content
  member_id comes back as an *array*, even for a single member.
So `ok()` accepts any 2xx rather than testing for 200.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_BASE = os.getenv("VOBIZ_API_BASE", "https://api.vobiz.ai/api/v1")
AUTH_ID = os.getenv("VOBIZ_AUTH_ID", "")
AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")
FROM_NUMBER = os.getenv("FROM_NUMBER", "")

HEADERS = {
    "Content-Type": "application/json",
    "X-Auth-ID": AUTH_ID,
    "X-Auth-Token": AUTH_TOKEN,
}

TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# With VOBIZ_MOCK=1 no request leaves the process: every call returns a
# plausible synthetic response and is recorded. That is what lets `mock.py`
# exercise the whole handoff — both flows, the race conditions and the cleanup —
# without spending a phone call on it.
MOCK = os.getenv("VOBIZ_MOCK", "").lower() in ("1", "true", "yes")
MOCK_CALLS: list[dict] = []

logger = logging.getLogger("vobiz")


class Result(dict):
    """A REST result that is truthy when the platform accepted the request."""

    @property
    def ok(self) -> bool:
        return 200 <= self["status"] < 300

    def __bool__(self) -> bool:
        return self.ok


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def _mock(method: str, path: str, payload: dict | None) -> Result:
    """Synthetic responses shaped like the real ones, including status codes."""
    import uuid as _uuid

    MOCK_CALLS.append({"method": method, "path": path, "payload": payload})
    logger.info(f"[mock] {method} {path}")

    if method == "POST" and path.endswith("/Call/"):
        call_uuid = str(_uuid.uuid4())
        return Result(status=201, body={"message": "call fired",
                                        "call_uuid": call_uuid,
                                        "request_uuid": call_uuid})
    if "/Stream/" in path and method == "POST":
        return Result(status=201, body={"message": "stream started",
                                        "stream_id": str(_uuid.uuid4())})
    if method == "POST" and payload and "aleg_url" in payload:
        return Result(status=202, body={
            "message": "call transferred",
            "call_uuids": [path.split("/Call/")[-1].strip("/")],
        })
    if method == "DELETE":
        return Result(status=204, body=None)
    if "/Member/" in path:
        # Mute / Deaf / Play / Speak / Kick all answer 202, and member_id comes
        # back as an array even for one member.
        member = path.rstrip("/").split("/Member/")[1].split("/")[0]
        return Result(status=202, body={"message": "ok", "member_id": [member]})
    if path.startswith("/account/") and "/cdr/" in path:
        return Result(status=200, body={
            "uuid": path.rsplit("/", 1)[-1], "billsec": 62, "duration": 70,
            "ring_time": 8, "total_cost": 0.90, "streaming_cost": 0.20,
            "currency": "INR", "billing_status": "completed", "mos": 4.5,
        })
    return Result(status=200, body={"message": "ok"})


async def _request(method: str, path: str, payload: dict | None = None) -> Result:
    if MOCK:
        return _mock(method, path, payload)
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.request(
                method, url, json=payload, headers=HEADERS
            )
    except Exception as exc:                      # network, DNS, TLS, timeout
        logger.error(f"{method} {path} raised {type(exc).__name__}: {exc}")
        return Result(status=0, body=None, error=f"{type(exc).__name__}: {exc}")

    body = _safe_json(response)
    level = logging.INFO if 200 <= response.status_code < 300 else logging.WARNING
    logger.log(level, f"{method} {path} -> {response.status_code} {body}")
    return Result(status=response.status_code, body=body)


def configured() -> bool:
    return MOCK or bool(AUTH_ID and AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

async def place_call(
    to: str,
    answer_url: str,
    *,
    hangup_url: str = "",
    ring_url: str = "",
    from_number: str = "",
    ring_timeout: int = 30,
    sip_headers: str = "",
    caller_name: str = "",
    **extra,
) -> Result:
    """
    Outbound call. `to` accepts an E.164 number *or* a SIP URI — the make-call
    API resolves `sip:user@registrar.vobiz.ai` against the registrar's location
    table, so no <Dial> is needed to reach a registered endpoint.

    `sip_headers` is the make-call spelling and is echoed back on every webhook
    with an **X-PH-** prefix — not X-VH-, which is what the <Dial sipHeaders>
    attribute produces. Both are restricted to [A-Za-z0-9] in keys and values,
    so only an opaque reference id can ride here, never a summary sentence.
    """
    payload = {
        "from": from_number or FROM_NUMBER,
        "to": to,
        "answer_url": answer_url,
        "answer_method": "POST",
        "ring_timeout": str(ring_timeout),
        **extra,
    }
    if hangup_url:
        payload["hangup_url"] = hangup_url
        payload["hangup_method"] = "POST"
    if ring_url:
        payload["ring_url"] = ring_url
        payload["ring_method"] = "POST"
    if sip_headers:
        payload["sip_headers"] = sip_headers
    if caller_name:
        payload["caller_name"] = caller_name
    return await _request("POST", f"/Account/{AUTH_ID}/Call/", payload)


async def hangup_call(call_uuid: str) -> Result:
    return await _request("DELETE", f"/Account/{AUTH_ID}/Call/{call_uuid}/")


async def transfer_call(call_uuid: str, aleg_url: str, legs: str = "aleg") -> Result:
    """
    Redirect a live leg to a new XML document. This is a *redirect*, not a
    bridge: the leg abandons the document it is executing, which means an
    in-flight <Stream> is torn down. Returns 202 Accepted.
    """
    payload = {"legs": legs, "aleg_url": aleg_url, "aleg_method": "POST"}
    return await _request("POST", f"/Account/{AUTH_ID}/Call/{call_uuid}/", payload)


# ---------------------------------------------------------------------------
# Streams — the media bug that makes an AI-in-conference possible
# ---------------------------------------------------------------------------

async def stream_attach(
    call_uuid: str,
    service_url: str,
    *,
    content_type: str = "audio/x-mulaw;rate=8000",
    status_callback_url: str = "",
    stream_timeout: int = 3600,
    bidirectional: bool = True,
    audio_track: str = "inbound",
) -> Result:
    """
    Attach a media stream to a leg that is already inside a <Conference>.

    <Stream> and <Conference> cannot be siblings — XML verbs run sequentially on
    a leg, so a blocking bidirectional stream means the room is never reached.
    This API attaches the stream as a media bug on the channel instead, which is
    independent of XML sequencing, so one leg does both.

    `bidirectional` must serialise to the STRING "true". The server compares
    against the string, and a JSON boolean silently yields a one-way stream that
    looks fine in the logs and never plays anything back.
    """
    payload = {
        "service_url": service_url,
        "bidirectional": "true" if bidirectional else "false",
        "audio_track": audio_track,          # required when bidirectional
        "content_type": content_type,
        "stream_timeout": stream_timeout,
    }
    if status_callback_url:
        payload["status_callback_url"] = status_callback_url
        payload["status_callback_method"] = "POST"
    return await _request("POST", f"/Account/{AUTH_ID}/Call/{call_uuid}/Stream/", payload)


async def stream_list(call_uuid: str) -> Result:
    return await _request("GET", f"/Account/{AUTH_ID}/Call/{call_uuid}/Stream/")


async def stream_detach(call_uuid: str, stream_id: str) -> Result:
    """Stop one media bug. This is how the AI 'drops off' without ending the leg."""
    return await _request(
        "DELETE", f"/Account/{AUTH_ID}/Call/{call_uuid}/Stream/{stream_id}/"
    )


# ---------------------------------------------------------------------------
# Conference — per-member only
# ---------------------------------------------------------------------------

def _room(name: str) -> str:
    return quote(name, safe="")


async def member_speak(room: str, member_id: str, text: str, **extra) -> Result:
    """
    Speak text to specific members, using Vobiz's own TTS.

    There is no room-wide inject: POST /Conference/{room}/Speak/ returns the
    gateway's generic {"message":"Not Found"} while the per-member route returns
    {"error":"conference not found"} — that difference is how you tell which
    routes exist. Reaching everyone means fanning out one call per member.

    Because this is Vobiz TTS it is *not* the agent's own voice. Prefer
    member_play() with audio the agent synthesised itself when the voice matters.
    """
    return await _request(
        "POST",
        f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Speak/",
        {"text": text, **extra},
    )


async def member_play(room: str, member_id: str, url: str, **extra) -> Result:
    """
    Play an audio URL to specific members. Other participants do not hear it,
    which makes this the natural whisper primitive for briefing a human agent
    who is already in the room.
    """
    return await _request(
        "POST",
        f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Play/",
        {"url": url, **extra},
    )


async def member_stop_play(room: str, member_id: str) -> Result:
    return await _request(
        "DELETE", f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Play/"
    )


async def member_mute(room: str, member_id: str) -> Result:
    """Others cannot hear this member. Returns 202."""
    return await _request(
        "POST", f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Mute/", {}
    )


async def member_unmute(room: str, member_id: str) -> Result:
    """Returns 204 with an empty body."""
    return await _request(
        "DELETE", f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Mute/"
    )


async def member_deaf(room: str, member_id: str) -> Result:
    """
    This member stops receiving room audio.

    Careful: if the AI's media bug rides *this* leg, deafening the member also
    deafens the AI, because audio_track="inbound" is exactly what the room sends
    into the leg. That is why the briefing in flow_conference happens on the
    human's leg before it joins, rather than by deafening the customer.
    """
    return await _request(
        "POST", f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Deaf/", {}
    )


async def member_undeaf(room: str, member_id: str) -> Result:
    return await _request(
        "DELETE", f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Deaf/"
    )


async def member_kick(room: str, member_id: str) -> Result:
    return await _request(
        "POST", f"/Account/{AUTH_ID}/Conference/{_room(room)}/Member/{member_id}/Kick/", {}
    )


async def hangup_conference(room: str) -> Result:
    return await _request("DELETE", f"/Account/{AUTH_ID}/Conference/{_room(room)}/")


async def conference_record(room: str, start: bool = True, **extra) -> Result:
    """
    Record a room over REST. Prefer this over <Conference record="true">, which
    produced zero recordings in live testing while this route does exist.
    """
    method = "POST" if start else "DELETE"
    return await _request(
        method, f"/Account/{AUTH_ID}/Conference/{_room(room)}/Record/", extra or None
    )


# ---------------------------------------------------------------------------
# CDR — the only place a cost ever appears
# ---------------------------------------------------------------------------

async def list_recordings(limit: int = 20, **filters) -> Result:
    """
    Recordings for the account. Rows paginate under `objects` with a `meta`
    block — not the bare array the field tables imply.
    """
    from urllib.parse import urlencode
    query = urlencode({"limit": limit, **filters})
    return await _request("GET", f"/Account/{AUTH_ID}/Recording/?{query}")


async def get_cdr(call_uuid: str) -> Result:
    """
    Note the lowercase path. Every webhook reports TotalCost=0.00000 on PSTN
    legs; the CDR carries the settled cost and is readable within ~120 ms of
    hangup, so no polling delay is needed — but the fetch is mandatory.
    """
    return await _request("GET", f"/account/{AUTH_ID}/cdr/{call_uuid}")
