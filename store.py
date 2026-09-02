"""
store.py — shared mutable state for both warm-transfer flows
=============================================================

This module exists for one structural reason. `app.py` runs as `__main__`, so
`import app` from any other module produces a *second* module object with its
own empty globals. State kept in `app.py` is therefore written to one dict and
read from another, silently. That bug once dropped every conference reply in
this codebase. `store` is only ever imported as a module, so there is exactly
one of it.

Three things live here:

  Roster    which member IDs are in which room, built from ConferenceEnter /
            ConferenceExit callbacks — never from REST, because
            GET /Conference/{name}/ returns HTTP 200 {"error":"failed"} on a
            live room.
  Transfer  one record per warm transfer, carrying the handoff state machine.
  Capture   every webhook and every XML reply, so a run can be read back.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.jsonl"

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Handoff state machine
# ---------------------------------------------------------------------------
# Ported from VOBIZ_CONFERENCE_HANDOFF_RACE_CONDITION.md. The lead and the
# human-agent leg progress asynchronously and their callbacks can arrive in
# either order, so every transition is recorded and every cleanup is idempotent.

BOT_ACTIVE = "bot_active"
AGENT_DIALING = "agent_dialing"
AGENT_ANSWERED = "agent_answered"
BRIEFING = "briefing"
ACCEPTED = "accepted"
REJECTED = "rejected"
BRIDGED = "bridged"
NO_ANSWER = "no_answer"
ENDED = "ended"


@dataclass
class Transfer:
    """One warm transfer, from the tool call to the bridge or the fallback."""

    tid: str
    flow: str                       # "conference" | "dial"
    room: str
    state: str = BOT_ACTIVE

    customer_uuid: str = ""
    customer_member: str = ""
    customer_stream_id: str = ""

    # Two agent legs may be dialled at once (PSTN + SIP); the first to accept
    # wins and the loser is hung up.
    agent_uuids: list[str] = field(default_factory=list)
    agent_uuid: str = ""            # the winner
    agent_member: str = ""
    agent_target: str = ""

    reason: str = ""
    summary: str = ""
    transcript: list[dict] = field(default_factory=list)

    created_at: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    cleanup_done: bool = False

    # Timings that answer the questions Sarvam asked, measured rather than
    # asserted: how long the human rang, and how long the customer's audio
    # bridge was down across a REST Transfer.
    marks: dict = field(default_factory=dict)

    # Mirror of the mute/deaf flags we have set, per member. The platform offers
    # no way to read them back — GET /Conference/{name}/ answers 200 with
    # {"error":"failed"} — so the console shows our own view of the state.
    member_flags: dict = field(default_factory=dict)

    def mark(self, name: str):
        self.marks.setdefault(name, time.time())

    def gap(self, start: str, end: str) -> float | None:
        if start in self.marks and end in self.marks:
            return round(self.marks[end] - self.marks[start], 3)
        return None

    def to(self, state: str, **detail):
        self.state = state
        self.events.append({"t": time.time(), "state": state, **detail})

    @property
    def settled(self) -> bool:
        return self.state in (BRIDGED, REJECTED, NO_ANSWER, ENDED)


TRANSFERS: dict[str, Transfer] = {}
ROOM_MEMBERS: dict[str, dict[str, str]] = {}     # room -> {member_id: call_uuid}
CALL_TO_TRANSFER: dict[str, str] = {}            # call_uuid -> tid
EVENTS: list[dict] = []


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

def new_transfer(flow: str, room: str = "") -> Transfer:
    with _lock:
        tid = uuid.uuid4().hex[:12]
        room = room or f"warm{tid}"
        transfer = Transfer(tid=tid, flow=flow, room=room)
        TRANSFERS[tid] = transfer
        return transfer


def get(tid: str) -> Transfer | None:
    return TRANSFERS.get(tid)


def by_room(room: str) -> Transfer | None:
    for transfer in TRANSFERS.values():
        if transfer.room == room:
            return transfer
    return None


def by_call(call_uuid: str) -> Transfer | None:
    tid = CALL_TO_TRANSFER.get(call_uuid)
    return TRANSFERS.get(tid) if tid else None


def bind_call(call_uuid: str, tid: str):
    if call_uuid:
        with _lock:
            CALL_TO_TRANSFER[call_uuid] = tid


def latest() -> Transfer | None:
    if not TRANSFERS:
        return None
    return max(TRANSFERS.values(), key=lambda t: t.created_at)


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def member_entered(room: str, member_id: str, call_uuid: str):
    with _lock:
        ROOM_MEMBERS.setdefault(room, {})[str(member_id)] = call_uuid


def member_exited(room: str, member_id: str):
    with _lock:
        ROOM_MEMBERS.get(room, {}).pop(str(member_id), None)


def members(room: str) -> list[str]:
    return list(ROOM_MEMBERS.get(room, {}).keys())


def member_for_call(room: str, call_uuid: str) -> str:
    for member_id, uuid_ in ROOM_MEMBERS.get(room, {}).items():
        if uuid_ == call_uuid:
            return member_id
    return ""


def others(room: str, member_id: str) -> list[str]:
    return [m for m in members(room) if m != str(member_id)]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def record(kind: str, payload: dict):
    """Append one captured event, in memory and to disk."""
    entry = {"t": time.time(), "kind": kind, **payload}
    with _lock:
        EVENTS.append(entry)
        try:
            with EVENTS_FILE.open("a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass
    tid = payload.get("tid") or _tid_for(payload)
    if tid and tid in TRANSFERS:
        TRANSFERS[tid].events.append(entry)
    return entry


def _tid_for(payload: dict) -> str:
    for key in ("CallUUID", "call_uuid", "ALegUUID"):
        value = (payload.get("body") or {}).get(key) if isinstance(
            payload.get("body"), dict
        ) else None
        if value and value in CALL_TO_TRANSFER:
            return CALL_TO_TRANSFER[value]
    return ""


def snapshot() -> dict:
    with _lock:
        return {
            "transfers": {t: asdict(v) for t, v in TRANSFERS.items()},
            "rooms": {r: dict(m) for r, m in ROOM_MEMBERS.items()},
            "events": len(EVENTS),
        }


def reset():
    with _lock:
        TRANSFERS.clear()
        ROOM_MEMBERS.clear()
        CALL_TO_TRANSFER.clear()
        EVENTS.clear()
