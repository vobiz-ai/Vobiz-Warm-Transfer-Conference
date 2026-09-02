"""
xml_docs.py — every answer document this service returns
========================================================

Kept apart from the server so `selftest.py` can parse all of them offline. That
matters more here than it looks: **malformed answer XML is not an HTTP error.**
Vobiz takes the 200, answers the call, drops it about a second later, and the
only trace is the CDR — `hangup_source Error`, `hangup_cause_name
"Invalid Answer XML"`, `HangupCauseCode 8011`. A URL carrying two query
parameters contains a bare `&`, which is enough to break the document, so every
interpolated value goes through `xe()`.
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr


def xe(value) -> str:
    """Escape a value for XML *text*. `&`, `<`, `>`."""
    return escape("" if value is None else str(value))


def xa(value) -> str:
    """Escape and quote a value for an XML *attribute*."""
    return quoteattr("" if value is None else str(value))


def _doc(body: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n' + body + "\n</Response>"


# ---------------------------------------------------------------------------
# Deliverable 1 — conference
# ---------------------------------------------------------------------------

def record_block(action_url: str, callback_url: str) -> str:
    """
    Session recording on a leg, as a self-closing sibling placed FIRST.

    Conference recording does not work — neither `<Conference record="true">`
    (silently gated on an S3 URL) nor `POST /Conference/{room}/Record/` (returns
    HTTP 200 `{"message":"async api spawned"}` and produces no row). Checked
    against 103 recordings on the account: **zero** carry a `conference_name`.

    Per-leg session recording is proven: stereo MP3 at 8 kHz with one party per
    channel, and it survives a transfer. `recordSession="true"` captures the
    whole leg including everything bridged into it, so recording the customer's
    leg captures the human agent too once they are in the room.

    `redirect="false"` lets the call flow continue instead of waiting on the
    recording — which is why the action URL must return an empty <Response>.
    """
    return (
        '  <Record fileFormat="mp3" recordSession="true" redirect="false"'
        ' playBeep="false" maxLength="3600"'
        f" action={xa(action_url)}"
        f" callbackUrl={xa(callback_url)}"
        ' callbackMethod="POST"/>\n'
    )


def conference_customer(room: str, events_url: str, wait_url: str, tap_ws: str = "",
                        record: str = "") -> str:
    """
    The customer's leg. It joins the room and waits; the AI is attached
    afterwards over the Stream REST API, once ConferenceEnter says the leg has
    actually settled in the room.

    `stayAlone="true"` is not optional. It initialises to False, and a lone
    member is kicked straight back out — which is exactly what the customer is
    for the first few seconds, before any human is dialled.

    `tap_ws` switches to the fallback topology (AI_MODE=tap): a NON-bidirectional
    <Stream audioTrack="both">, which does not block the leg, so <Conference>
    below it is still reached. A bidirectional stream here would block and the
    room would never be joined.
    """
    tap = ""
    if tap_ws:
        tap = (
            f'  <Stream audioTrack="both" contentType="audio/x-mulaw;rate=8000">'
            f"{xe(tap_ws)}</Stream>\n"
        )
    return _doc(
        record
        + tap
        + "  <Conference"
        f' stayAlone="true"'
        f' startConferenceOnEnter="true"'
        f' endConferenceOnExit="true"'
        f' relayDTMF="false"'
        f' timeLimit="1800"'
        f" waitSound={xa(wait_url)}"
        f' waitMethod="POST"'
        f" callbackUrl={xa(events_url)}"
        f' callbackMethod="POST">'
        f"{xe(room)}</Conference>\n"
        "  <Hangup/>"
    )


def conference_agent(room: str, events_url: str, brief_ws: str, status_url: str,
                     extra_headers: str, record: str = "") -> str:
    """
    The human agent's leg, and the heart of the warm transfer.

    The briefing runs *before* the room is joined. <Stream> and <Conference> are
    siblings executed in order, and `keepCallAlive="true"` holds the leg on the
    stream until the socket closes — so the AI briefs the human privately, and
    only when it closes the socket does the leg walk on into the room.

    The customer therefore cannot overhear the briefing for a structural reason
    rather than a configured one: the human is not in the room yet. No mute, no
    deaf, and so no risk of deafening the AI along with them.
    """
    return _doc(
        record
        + '  <Stream bidirectional="true"'
        ' audioTrack="inbound"'
        ' keepCallAlive="true"'
        ' contentType="audio/x-l16;rate=16000"'
        f" extraHeaders={xa(extra_headers)}"
        f" statusCallbackUrl={xa(status_url)}"
        ' statusCallbackMethod="POST">'
        f"{xe(brief_ws)}</Stream>\n"
        "  <Conference"
        ' stayAlone="true"'
        ' startConferenceOnEnter="true"'
        ' endConferenceOnExit="false"'
        ' relayDTMF="false"'
        ' timeLimit="1800"'
        f" callbackUrl={xa(events_url)}"
        ' callbackMethod="POST">'
        f"{xe(room)}</Conference>\n"
        "  <Hangup/>"
    )


def conference_wait() -> str:
    """
    Played to a member waiting for the room to start.

    Note this fires on join whether or not the room is already started — a
    moderator with startConferenceOnEnter="true" does not suppress it.
    """
    return _doc('  <Wait length="3600"/>')


def agent_declined() -> str:
    """The human said no during the briefing — their leg ends without joining."""
    return _doc(
        '  <Speak voice="WOMAN" language="en-IN">Thanks, no problem. Ending here.</Speak>\n'
        "  <Hangup/>"
    )


def empty() -> str:
    """
    A valid no-op. Used for callbacks that must not steer the call — a Record
    action with redirect="false", for instance, where the flow has already moved
    on and returning call-control XML would interrupt it.
    """
    return _doc("")


def hangup(message: str = "") -> str:
    body = ""
    if message:
        body = f'  <Speak voice="WOMAN" language="en-IN">{xe(message)}</Speak>\n'
    return _doc(body + "  <Hangup/>")
