"""
flow_conference.py — the warm-transfer call flow
=================================================

The customer sits in a <Conference> from the moment they answer. The AI joins
that leg as a media bug over the Stream REST API. When a human is needed, the
human is dialled into a *briefing* stream first, and only walks into the room
once they have accepted.

    customer ──answer──▶ <Conference>ROOM</Conference>
                              │
              ConferenceEnter │  (+2s, let the leg settle)
                              ▼
                  POST /Call/{cust}/Stream/   ← AI is now duplex in the room
                              │
              transfer_to_human(reason, summary)
                              ▼
                  POST /Call/  ──▶ human agent
                              │
                     <Stream> briefing  ← private: the human is NOT in the
                              │            room yet, so nothing can leak
                     accept_transfer()
                              ▼
                  socket closes, leg walks into <Conference>ROOM
                              │
                     ConferenceEnter (agent)  ← all three parties bridged

Why the briefing is off-room rather than a mute/deaf dance: the AI's media bug
rides the customer's leg with audio_track="inbound", which is exactly the audio
the room sends into that leg. Deafening the customer to hide the briefing would
deafen the AI along with them. Briefing before the human joins avoids the
problem entirely instead of working around it.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import Response  # noqa: F401  (re-exported via web)

import gemini_live
import runtime
import store
import vobiz
import web
import xml_docs
from web import call_params

logger = logging.getLogger("d1")
router = APIRouter(prefix="/d1")

# AI_MODE=duplex  the Stream REST API media bug — real-time, the agent's own voice
# AI_MODE=tap     <Stream audioTrack="both"> + per-member REST Speak — the proven
#                 fallback, but half-duplex and in Vobiz's TTS voice, not Gemini's
AI_MODE = os.getenv("AI_MODE", "duplex").lower()

# The REST-attached media bug is mulaw/8k — the format this API is proven with.
CONF_CONTENT_TYPE = os.getenv("CONF_CONTENT_TYPE", "audio/x-mulaw;rate=8000")
CONF_OUT_RATE = int(os.getenv("CONF_OUT_RATE", "8000"))
CONF_OUT_TYPE = os.getenv("CONF_OUT_TYPE", "audio/x-mulaw")

# Record the room over REST. The <Conference record="true"> attribute is broken.
# Per-leg session recording, the only method that produces a file.
RECORD_ROOM = os.getenv("RECORD_ROOM", "true").lower() == "true"
# The conference REST route. Returns 200 and records nothing; off by default.
RECORD_CONFERENCE_REST = os.getenv("RECORD_CONFERENCE_REST", "false").lower() == "true"
RING_TIMEOUT = int(os.getenv("RING_TIMEOUT", "25"))
SETTLE_DELAY = float(os.getenv("SETTLE_DELAY", "2.0"))
# How long to wait after a handover is promised before nudging the model, and
# then before dialling without it. See _watch_tool_call.
TOOL_NUDGE_SECONDS = float(os.getenv("TOOL_NUDGE_SECONDS", "6"))
# Hard cap on how long the customer waits in silence after the human accepts.
ACCEPT_JOIN_TIMEOUT = float(os.getenv("ACCEPT_JOIN_TIMEOUT", "3.0"))
TOOL_FORCE_SECONDS = float(os.getenv("TOOL_FORCE_SECONDS", "8"))

NO_ANSWER_LINE = os.getenv(
    "NO_ANSWER_LINE",
    "I'm sorry, nobody is free to take this right now. We'll call you back "
    "shortly. Thanks for your patience.",
)
DECLINED_LINE = os.getenv(
    "DECLINED_LINE",
    "Sorry about that, my colleague can't help with this one. Let me take it "
    "from here.",
)
JOINING_LINE = os.getenv(
    "JOINING_LINE",
    "My colleague is on the line now and has the background. I'll leave you both to it.",
)

SESSIONS: dict[str, gemini_live.GeminiLiveSession] = {}   # "tid:role" -> session


def _key(tid: str, role: str) -> str:
    return f"{tid}:{role}"


_xml = web.xml_reply


# ===========================================================================
#  Answer documents
# ===========================================================================

@router.api_route("/answer/customer", methods=["GET", "POST"])
async def answer_customer(request: Request):
    params = await call_params(request)
    tid = params.get("tid", "")
    transfer = store.get(tid)
    store.record("answer_customer", {"tid": tid, "body": params})
    if not transfer:
        return _xml(xml_docs.hangup("Sorry, this call could not be set up."), tid)

    transfer.customer_uuid = params.get("CallUUID", "") or transfer.customer_uuid
    store.bind_call(transfer.customer_uuid, tid)
    transfer.mark("customer_answered")

    tap_ws = ""
    if AI_MODE == "tap":
        tap_ws = runtime.ws_url("/ws", role="customer", tid=tid, mode="tap")

    record = ""
    if RECORD_ROOM:
        # On the customer's leg, because recordSession captures everything
        # bridged into it — including the human agent once they join.
        record = xml_docs.record_block(
            runtime.url("/d1/record-action", tid=tid, leg="customer"),
            runtime.url("/d1/record-callback", tid=tid, leg="customer"),
        )

    return _xml(
        xml_docs.conference_customer(
            room=transfer.room,
            events_url=runtime.url("/d1/conf-events", tid=tid),
            wait_url=runtime.url("/d1/wait", tid=tid),
            tap_ws=tap_ws,
            record=record,
        ),
        tid,
        "customer",
    )


@router.api_route("/answer/agent", methods=["GET", "POST"])
async def answer_agent(request: Request):
    params = await call_params(request)
    tid = params.get("tid", "")
    transfer = store.get(tid)
    call_uuid = params.get("CallUUID", "")
    store.record("answer_agent", {"tid": tid, "body": params})

    if not transfer:
        return _xml(xml_docs.hangup(), tid)

    # Two agent legs may be racing (PSTN and SIP dialled together). The first to
    # answer wins; the loser is hung up so a second human is never tied up.
    if transfer.agent_uuid and transfer.agent_uuid != call_uuid:
        logger.info(f"[{tid}] second agent leg {call_uuid} lost the race")
        store.record("agent_lost_race", {"tid": tid, "call_uuid": call_uuid})
        return _xml(xml_docs.hangup(), tid, "agent-lost-race")

    transfer.agent_uuid = call_uuid
    store.bind_call(call_uuid, tid)
    transfer.mark("agent_answered")
    transfer.to(store.AGENT_ANSWERED, call_uuid=call_uuid)

    for other in transfer.agent_uuids:
        if other and other != call_uuid:
            await vobiz.hangup_call(other)

    record = ""
    if RECORD_ROOM:
        # Record the agent's leg too. It carries the private briefing, which is
        # the part of a warm transfer worth being able to replay — and it is
        # audio the customer's recording cannot contain, because the agent is
        # not in the room yet while it happens.
        record = xml_docs.record_block(
            runtime.url("/d1/record-action", tid=tid, leg="agent"),
            runtime.url("/d1/record-callback", tid=tid, leg="agent"),
        )

    return _xml(
        xml_docs.conference_agent(
            room=transfer.room,
            events_url=runtime.url("/d1/conf-events", tid=tid),
            brief_ws=runtime.ws_url("/ws", role="agent", tid=tid),
            status_url=runtime.url("/d1/stream-status", tid=tid),
            record=record,
            # extraHeaders is [A-Za-z0-9] only, so it carries the reference id
            # and nothing else. The summary is looked up server-side from it.
            extra_headers=f"tid={tid}",
        ),
        tid,
        "agent",
    )


@router.api_route("/wait", methods=["GET", "POST"])
async def wait(request: Request):
    params = await call_params(request)
    store.record("conference_wait", {"tid": params.get("tid", ""), "body": params})
    return _xml(xml_docs.conference_wait(), params.get("tid", ""), "wait")


@router.api_route("/declined", methods=["GET", "POST"])
async def declined(request: Request):
    params = await call_params(request)
    store.record("agent_declined_xml", {"tid": params.get("tid", ""), "body": params})
    return _xml(xml_docs.agent_declined(), params.get("tid", ""), "declined")


# ===========================================================================
#  Callbacks
# ===========================================================================

@router.api_route("/conf-events", methods=["GET", "POST"])
async def conf_events(request: Request):
    params = await call_params(request)
    tid = params.get("tid", "")
    action = params.get("ConferenceAction", "")
    room = params.get("ConferenceName", "")
    member = params.get("ConferenceMemberID", "")
    call_uuid = params.get("CallUUID", "")
    store.record("conference_event", {"tid": tid, "body": params})

    transfer = store.get(tid) or store.by_room(room)
    if not transfer:
        return {"ok": True}

    if action == "enter" and member:
        store.member_entered(transfer.room, member, call_uuid)
        logger.info(
            f"[{transfer.tid}] enter member={member} call={call_uuid} "
            f"roster={store.members(transfer.room)}"
        )
        if call_uuid == transfer.customer_uuid:
            transfer.customer_member = member
            transfer.mark("customer_in_room")
            if AI_MODE == "duplex":
                asyncio.create_task(_attach_customer_stream(transfer))
            if RECORD_CONFERENCE_REST:
                # Kept behind a flag purely to re-check the defect. It returns
                # HTTP 200 and records nothing.
                asyncio.create_task(_start_recording(transfer))
        elif call_uuid == transfer.agent_uuid:
            transfer.agent_member = member
            if transfer.state == store.REJECTED:
                # They declined and their leg is being torn down; it can still
                # touch the room on the way out. Never announce that as a bridge.
                store.record(
                    "rejected_agent_entered_room",
                    {"tid": transfer.tid, "member": member},
                )
                asyncio.create_task(_evict(transfer, member))
                return {"ok": True}
            transfer.mark("agent_in_room")
            transfer.to(store.BRIDGED, member=member)
            asyncio.create_task(_announce_bridged(transfer))

    elif action == "exit" and member:
        store.member_exited(transfer.room, member)
        logger.info(
            f"[{transfer.tid}] exit member={member} "
            f"roster={store.members(transfer.room)}"
        )

    return {"ok": True}


@router.api_route("/record-action", methods=["GET", "POST"])
async def record_action(request: Request):
    """
    Fires when recording starts, with every duration field as -1.

    Must return an EMPTY <Response>: with redirect="false" the call flow has
    already moved on, and returning call-control XML here would interrupt it.
    """
    params = await call_params(request)
    store.record("record_action", {"tid": params.get("tid", ""), "body": params})
    return _xml(xml_docs.empty(), params.get("tid", ""), "record-action")


@router.api_route("/record-callback", methods=["GET", "POST"])
async def record_callback(request: Request):
    """Where the finished recording's URL arrives, after the room ends."""
    params = await call_params(request)
    store.record("recording_callback", {"tid": params.get("tid", ""), "body": params})
    return {"ok": True}


@router.api_route("/stream-status", methods=["GET", "POST"])
async def stream_status(request: Request):
    params = await call_params(request)
    store.record("stream_status", {"tid": params.get("tid", ""), "body": params})
    return {"ok": True}


@router.api_route("/hangup", methods=["GET", "POST"])
async def hangup(request: Request):
    params = await call_params(request)
    tid = params.get("tid", "")
    store.record("hangup", {"tid": tid, "body": params})
    transfer = store.get(tid)
    if transfer:
        call_uuid = params.get("CallUUID", "")
        if call_uuid == transfer.customer_uuid:
            asyncio.create_task(cleanup(transfer, "customer hung up"))
        elif call_uuid in transfer.agent_uuids and not transfer.settled:
            if transfer.agent_uuid and call_uuid != transfer.agent_uuid:
                # A losing leg from a simultaneous dial, hung up by us on
                # purpose. Reading this as "the agent is gone" once tore down
                # the leg a human had answered 400 ms earlier.
                store.record(
                    "agent_loser_hangup", {"tid": transfer.tid, "call_uuid": call_uuid}
                )
            else:
                asyncio.create_task(_agent_unavailable(transfer, "agent leg ended"))
    return {"ok": True}


# ===========================================================================
#  Orchestration
# ===========================================================================

async def start(to: str, room: str = "") -> store.Transfer:
    """Place the customer call that begins a Deliverable 1 demo."""
    transfer = store.new_transfer("conference", room)
    result = await vobiz.place_call(
        to=to,
        answer_url=runtime.url("/d1/answer/customer", tid=transfer.tid),
        hangup_url=runtime.url("/d1/hangup", tid=transfer.tid),
        ring_timeout=45,
    )
    body = result.get("body") or {}
    if isinstance(body, dict):
        transfer.customer_uuid = body.get("call_uuid") or body.get("request_uuid", "")
        store.bind_call(transfer.customer_uuid, transfer.tid)
    store.record("place_customer", {"tid": transfer.tid, "result": dict(result)})
    return transfer


async def _start_recording(transfer: store.Transfer):
    """
    Record the room over REST.

    Not via `<Conference record="true">`: that attribute parses but is gated on
    an S3 URL being present and silently records nothing when it is not — zero
    recordings were produced across every XML attempt. The REST route works.
    """
    await asyncio.sleep(SETTLE_DELAY)
    result = await vobiz.conference_record(
        transfer.room,
        start=True,
        file_format="mp3",
        callback_url=runtime.url("/d1/record-callback", tid=transfer.tid),
        callback_method="POST",
    )
    transfer.mark("recording_started")
    store.record("recording_start", {"tid": transfer.tid, "result": dict(result)})


async def _stop_recording(transfer: store.Transfer):
    result = await vobiz.conference_record(transfer.room, start=False)
    store.record("recording_stop", {"tid": transfer.tid, "result": dict(result)})


async def _attach_customer_stream(transfer: store.Transfer):
    """
    Give the AI ears and a voice inside the room.

    The delay matters: the leg has only just been told to join, and attaching a
    media bug before it has settled in the room gets a stream that hears
    nothing.
    """
    await asyncio.sleep(SETTLE_DELAY)
    result = await vobiz.stream_attach(
        transfer.customer_uuid,
        runtime.ws_url("/ws", role="customer", tid=transfer.tid),
        content_type=CONF_CONTENT_TYPE,
        status_callback_url=runtime.url("/d1/stream-status", tid=transfer.tid),
    )
    body = result.get("body") or {}
    if isinstance(body, dict):
        transfer.customer_stream_id = (
            body.get("stream_id") or body.get("streamId") or ""
        )
    transfer.mark("ai_attached")
    store.record("stream_attach", {"tid": transfer.tid, "result": dict(result)})
    if not result.ok:
        logger.error(f"[{transfer.tid}] stream attach failed — the AI has no audio")


def _sip_context(transfer) -> str:
    """
    Context for a SIP/WebRTC agent, as custom SIP headers.

    The validator requires the key to **end** with `X-VH` — `helpers.py`
    `validate_headers_content` does `key.endswith('X-VH')` and returns False
    otherwise, rejecting the whole set. `tid=...` was silently dropped; the
    correct spelling is `tidX-VH=...`.

    Both key stem and value must satisfy `str.isalnum()`, so no spaces, dots or
    hyphens — which is why this carries an opaque reference id and not the
    summary itself. The agent's client resolves the id against /api/transfers.

    NOT YET VERIFIED on the wire: no packet capture of the far-end INVITE exists
    on either side, so what actually arrives is unconfirmed.
    """
    return f"tidX-VH={transfer.tid}"

async def dial_agents(transfer: store.Transfer, targets: list[str]):
    """
    Ring one or more humans. `targets` may mix E.164 numbers and SIP URIs — the
    make-call API resolves `sip:user@registrar.vobiz.ai` against the registrar,
    so a WebRTC agent needs no <Dial>.
    """
    # Idempotent. The tool call and the watchdog can both reach here for one
    # handover, and on a live call they did — the human's phone rang twice, and
    # the duplicate leg then broke the race handling below.
    if transfer.marks.get("dialing_started"):
        logger.info(f"[{transfer.tid}] dial already under way — ignoring duplicate")
        store.record("dial_duplicate_suppressed", {"tid": transfer.tid})
        return
    transfer.mark("dialing_started")
    transfer.to(store.AGENT_DIALING, targets=targets)
    transfer.mark("agent_dialing")

    for target in targets:
        is_sip = target.startswith("sip:")
        result = await vobiz.place_call(
            to=target,
            answer_url=runtime.url("/d1/answer/agent", tid=transfer.tid),
            hangup_url=runtime.url("/d1/hangup", tid=transfer.tid),
            ring_timeout=RING_TIMEOUT,
            # Only on the SIP leg: a WebRTC client can read this off the INVITE
            # and pop the case on screen. Values are [A-Za-z0-9] only, so this
            # is a reference id, never the summary text itself.
            sip_headers=_sip_context(transfer) if is_sip else "",
            caller_name="Warm transfer",
        )
        body = result.get("body") or {}
        if isinstance(body, dict):
            uuid_ = body.get("call_uuid") or body.get("request_uuid", "")
            if uuid_:
                transfer.agent_uuids.append(uuid_)
                store.bind_call(uuid_, transfer.tid)
        store.record(
            "place_agent", {"tid": transfer.tid, "to": target, "result": dict(result)}
        )

    if not transfer.agent_uuids:
        await _agent_unavailable(transfer, "no agent leg was created")
        return

    asyncio.create_task(_watch_no_answer(transfer))


async def _watch_no_answer(transfer: store.Transfer):
    """
    If nobody picks up, the customer must be told — not left in silence.
    """
    await asyncio.sleep(RING_TIMEOUT + 5)
    if transfer.state in (store.AGENT_DIALING,):
        await _agent_unavailable(transfer, "ring timeout")


def _reset_handover(transfer: store.Transfer):
    """
    Clear the per-attempt handover state so the caller can ask again.

    Without this, a failed handover poisons every retry: `agent_uuid` still
    names the dead leg, so the next agent to answer is misread as a losing race
    entrant and hung up the instant they pick up. On a live call the same human
    answered twice and was cut off both times — once by the REST hangup, once by
    the lost-race `<Hangup/>`.

    The transfer identity, the room and the transcript survive; only the state
    describing *this attempt* is discarded.
    """
    transfer.agent_uuid = ""
    transfer.agent_uuids = []
    transfer.agent_member = ""
    for mark in ("dialing_started", "tool_fired", "handover_expected",
                 "agent_dialing", "agent_answered"):
        transfer.marks.pop(mark, None)
    transfer.state = store.BOT_ACTIVE
    store.record("handover_reset", {"tid": transfer.tid})


async def _agent_unavailable(transfer: store.Transfer, why: str):
    if transfer.settled:
        return
    # A human who has answered is being briefed right now. Never tear that down
    # and tell the customer nobody is free over the top of it.
    if transfer.state in (store.AGENT_ANSWERED, store.BRIEFING, store.ACCEPTED):
        store.record(
            "agent_unavailable_suppressed",
            {"tid": transfer.tid, "why": why, "state": transfer.state},
        )
        return
    transfer.to(store.NO_ANSWER, why=why)
    logger.info(f"[{transfer.tid}] no human available — {why}")
    store.record("agent_unavailable", {"tid": transfer.tid, "why": why})
    for uuid_ in transfer.agent_uuids:
        await vobiz.hangup_call(uuid_)
    # Only after the ringing legs are cancelled — the reset clears the very list
    # this loop walks.
    _reset_handover(transfer)
    await _tell_customer(transfer, NO_ANSWER_LINE)


async def _evict(transfer: store.Transfer, member: str):
    """Remove a member who should not be in the room, without ending the room."""
    await vobiz.member_kick(transfer.room, member)


async def _announce_bridged(transfer: store.Transfer):
    if transfer.state != store.BRIDGED:
        return
    await asyncio.sleep(0.8)      # let the join tone and the leg settle
    await _tell_customer(transfer, JOINING_LINE)

    # The introduction is the AI's last word. From here it listens only —
    # anything further is talking over two people who are now connected.
    await asyncio.sleep(4)
    session = SESSIONS.get(_key(transfer.tid, "customer"))
    if session and hasattr(session, "go_silent"):
        session.go_silent()
        store.record("ai_silenced", {"tid": transfer.tid})
    if os.getenv("AI_DROP_AFTER_BRIDGE", "false").lower() == "true":
        await asyncio.sleep(6)
        await drop_ai(transfer)


async def _tell_customer(transfer: store.Transfer, line: str):
    """
    Speak to the customer, whichever topology is in play.

    duplex — the AI says it itself, in its own voice, over the media bug.
    tap    — no media bug exists, so it goes through Vobiz TTS on the member.
    """
    session = SESSIONS.get(_key(transfer.tid, "customer"))
    if AI_MODE == "duplex" and session:
        await session.say(line)
        return
    if transfer.customer_member:
        await vobiz.member_speak(transfer.room, transfer.customer_member, line)


async def drop_ai(transfer: store.Transfer):
    """Detach the media bug. The call continues; only the AI leaves."""
    if transfer.customer_stream_id:
        result = await vobiz.stream_detach(
            transfer.customer_uuid, transfer.customer_stream_id
        )
        store.record("stream_detach", {"tid": transfer.tid, "result": dict(result)})


async def whisper(transfer: store.Transfer, text: str, audio_url: str = ""):
    """
    Private line to the human agent while they are already in the room.

    Not used by the main flow — the briefing happens before they join — but this
    is the mechanism for supervisor barge-in, and it is what makes an in-room
    whisper possible at all: per-member Play/Speak is heard only by the members
    named, and there is no room-wide inject to leak it.
    """
    if not transfer.agent_member:
        return None
    if audio_url:
        return await vobiz.member_play(transfer.room, transfer.agent_member, audio_url)
    return await vobiz.member_speak(transfer.room, transfer.agent_member, text)


async def cleanup(transfer: store.Transfer, why: str = ""):
    """
    Idempotent teardown. The same cleanup can be triggered by several callbacks
    arriving within milliseconds of each other, so it must be safe to repeat.
    """
    if transfer.cleanup_done:
        return
    transfer.cleanup_done = True
    logger.info(f"[{transfer.tid}] cleanup — {why}")
    store.record("cleanup", {"tid": transfer.tid, "why": why, "state": transfer.state})

    if RECORD_CONFERENCE_REST and transfer.marks.get("recording_started"):
        await _stop_recording(transfer)

    if not transfer.settled:
        # The customer left before the bridge: cancel the human rather than
        # dropping them into an empty room.
        for uuid_ in transfer.agent_uuids:
            await vobiz.hangup_call(uuid_)
        if store.members(transfer.room):
            await vobiz.hangup_conference(transfer.room)

    transfer.to(store.ENDED, why=why)
    asyncio.create_task(_collect_cdrs(transfer))


async def _collect_cdrs(transfer: store.Transfer):
    """
    Cost lives only in the CDR — every webhook reports TotalCost 0.00000 on a
    PSTN leg. It is settled within ~120 ms of hangup, so this needs no polling
    delay, but it does need to happen for every leg.
    """
    await asyncio.sleep(2)
    for uuid_ in [transfer.customer_uuid, *transfer.agent_uuids]:
        if not uuid_:
            continue
        result = await vobiz.get_cdr(uuid_)
        store.record("cdr", {"tid": transfer.tid, "call_uuid": uuid_, "result": dict(result)})


# ===========================================================================
#  WebSocket sessions
# ===========================================================================

class TapStream(gemini_live.VobizStream):
    """
    Fallback topology. The leg carries a non-bidirectional <Stream>, so there is
    no path back down the socket — anything played would only reach the leg
    carrying the tap, not the room. Gemini's audio is therefore discarded and
    only its text is used, fanned out through per-member Speak.
    """

    async def play(self, pcm24: bytes):
        return

    async def clear(self):
        return


def build_session(ws, role: str, tid: str, mode: str = "") -> gemini_live.GeminiLiveSession | None:
    transfer = store.get(tid)
    if not transfer:
        return None

    if role == "agent":
        persona = gemini_live.brief_persona(
            transfer.summary, transfer.reason,
            out_content_type="audio/x-l16", out_rate=24000,
        )
    else:
        persona = gemini_live.customer_persona(
            out_content_type=CONF_OUT_TYPE if AI_MODE == "duplex" else "audio/x-l16",
            out_rate=CONF_OUT_RATE if AI_MODE == "duplex" else 24000,
        )

    session = gemini_live.GeminiLiveSession(
        ws,
        persona,
        on_turn=lambda who, text: _on_turn(transfer, role, who, text),
        on_event=lambda name, data: store.record(
            "ws", {"tid": tid, "role": role, "event": name, "body": data}
        ),
    )
    if mode == "tap" and role == "customer":
        session.vobiz = TapStream(ws, "audio/x-l16", 24000)

    session.on_tool = lambda name, args: _on_tool(transfer, role, session, name, args)
    SESSIONS[_key(tid, role)] = session
    return session


# Phrases that mean the caller wants a person. Deliberately broad — a false
# positive only arms a watchdog, while a miss leaves the caller waiting.
_WANTS_HUMAN = (
    "human", "person", "someone else", "real agent", "actual agent",
    "speak to a", "talk to a", "put me through", "transfer me",
    "connect me", "supervisor", "manager",
)

# Things the agent says when it *believes* it is transferring. On the first live
# call the model said all of these and never called the function once.
_CLAIMS_TRANSFER = (
    "colleague", "bringing in", "connecting you", "put you through",
    "transfer you", "someone will", "they will be", "joining",
)


def _on_turn(transfer: store.Transfer, role: str, who: str, text: str):
    transfer.transcript.append({"leg": role, "role": who, "text": text})
    store.record("turn", {"tid": transfer.tid, "leg": role, "role": who, "text": text})
    if AI_MODE == "tap" and role == "customer" and who == "agent" and text:
        asyncio.create_task(_fan_out(transfer, text))

    if role != "customer" or transfer.state != store.BOT_ACTIVE:
        return
    arm_tool_watchdog(
        transfer, who, text, SESSIONS.get(_key(transfer.tid, "customer")), dial_agents
    )


def arm_tool_watchdog(transfer, who, text, session, dial):
    """
    Arm the watchdog if this turn implies a handover is coming.

    Shared by both flows — the failure and the remedy are identical, only the
    dial differs.
    """
    if transfer.state != store.BOT_ACTIVE:
        return

    lowered = text.lower()
    asked = who == "caller" and any(p in lowered for p in _WANTS_HUMAN)
    claimed = who == "agent" and any(p in lowered for p in _CLAIMS_TRANSFER)
    if (asked or claimed) and not transfer.marks.get("handover_expected"):
        transfer.mark("handover_expected")
        store.record(
            "handover_expected",
            {"tid": transfer.tid, "trigger": "caller" if asked else "agent", "text": text},
        )
        asyncio.create_task(_watch_tool_call(transfer, session, dial))


async def _watch_tool_call(transfer: store.Transfer, session, dial):
    """
    Make sure a promised handover actually happens.

    On the first live call the model announced a colleague was joining and never
    called `transfer_to_human`, so nobody was dialled and the caller was left
    waiting on a person who had never been contacted. The prompt now forbids
    that, but a prompt is a behavioural bet and this failure is silent from the
    caller's side — so the server checks rather than trusts.

    Nudge first, since the model may simply be mid-turn. Only if that is ignored
    do we synthesise the handover ourselves from the transcript.
    """
    await asyncio.sleep(TOOL_NUDGE_SECONDS)
    if transfer.state != store.BOT_ACTIVE or "tool_fired" in transfer.marks:
        return

    logger.warning(f"[{transfer.tid}] handover promised but no tool call — nudging")
    store.record("tool_nudge", {"tid": transfer.tid})
    if session:
        await session.inform(
            "SYSTEM: you have not called transfer_to_human. No colleague has been "
            "dialled and the caller is waiting. Call transfer_to_human now, with a "
            "summary of this call. Do not reply in words instead."
        )

    await asyncio.sleep(TOOL_FORCE_SECONDS)
    if transfer.state != store.BOT_ACTIVE or "tool_fired" in transfer.marks:
        return

    # The model will not act. Better a summary built from the transcript than a
    # caller holding for a phone that never rang.
    logger.error(f"[{transfer.tid}] nudge ignored — forcing the handover")
    said = " ".join(
        t["text"] for t in transfer.transcript
        if t.get("leg") == "customer" and t.get("role") == "caller"
    )[:600]
    transfer.reason = transfer.reason or "caller asked for a human"
    transfer.summary = transfer.summary or (
        "Automatic summary — the assistant did not provide one. The caller said: "
        + (said or "nothing that was transcribed.")
    )
    store.record("tool_forced", {"tid": transfer.tid, "summary": transfer.summary})
    targets = transfer.agent_target.split(",") if transfer.agent_target else []
    if targets:
        await dial(transfer, targets)


async def _fan_out(transfer: store.Transfer, text: str):
    """Tap mode only: the agent's words reach the room one member at a time."""
    members = store.members(transfer.room)
    for member in members:
        await vobiz.member_speak(transfer.room, member, text)
    store.record(
        "tap_speak", {"tid": transfer.tid, "members": members, "text": text}
    )


async def _on_tool(transfer, role, session, name, args):
    if name == "transfer_to_human":
        # Stand the watchdog down now, not when the deferred dial runs. The dial
        # waits for playback to finish, and that gap was long enough for the
        # watchdog to place a second, duplicate call to the same human.
        transfer.mark("tool_fired")
        transfer.reason = args.get("reason", "")
        transfer.summary = args.get("summary", "")
        targets = transfer.agent_target.split(",") if transfer.agent_target else []
        store.record(
            "tool_transfer", {"tid": transfer.tid, "reason": transfer.reason,
                              "summary": transfer.summary, "targets": targets}
        )
        # Dial only once the holding line has actually been heard, so the
        # customer is never left wondering during the ring.
        session.run_after_playback(lambda: dial_agents(transfer, targets))
        return {"result": "dialing", "targets": len(targets)}

    if name == "accept_transfer":
        transfer.mark("accepted")
        transfer.to(store.ACCEPTED)
        store.record("tool_accept", {"tid": transfer.tid})
        # Closing the briefing socket releases keepCallAlive, and the leg walks
        # on to the <Conference> element beneath it.
        session.run_after_playback(session.vobiz.close, timeout=ACCEPT_JOIN_TIMEOUT)
        return {"result": "joining"}

    if name == "reject_transfer":
        transfer.to(store.REJECTED, reason=args.get("reason", ""))
        store.record("tool_reject", {"tid": transfer.tid, "args": args})

        async def finish():
            # Tell the customer's agent the colleague refused. Without this it
            # only hears itself say the decline line and then contradicts it —
            # observed live: "Sorry, my colleague can't help" at +122.7s,
            # followed by "My colleague is on the line to help you" at +131.3s.
            customer_session = SESSIONS.get(_key(transfer.tid, "customer"))
            if customer_session:
                await customer_session.inform(
                    "SYSTEM: the colleague DECLINED and is no longer on the call. "
                    "Nobody else is joining. Do not say anyone is connecting, "
                    "joining, or on the line. Handle the caller yourself, or "
                    "offer a callback."
                )

            # Hang the leg up; do NOT close the socket first. Closing it
            # releases keepCallAlive, and the leg then walks into the
            # <Conference> beneath the <Stream> — which is the accept path. On a
            # live call a colleague who declined still appeared in the room for
            # 200 ms, and that join announced them to the customer as connected.
            await vobiz.hangup_call(transfer.agent_uuid)
            await _tell_customer(transfer, DECLINED_LINE)

        session.run_after_playback(finish)
        return {"result": "declined"}

    if name == "end_call":
        session.run_after_playback(session.vobiz.close)
        return {"result": "ending"}

    return {"result": "unknown_tool"}
