#!/usr/bin/env python3
"""
mock.py — run a whole warm transfer with no phone call
=======================================================

    python mock.py                 happy path
    python mock.py --case declined
    python mock.py --case no-answer
    python mock.py --case race
    python mock.py --case customer-drops
    python mock.py --all           every case

This impersonates Vobiz. It POSTs the real answer and callback webhooks to the
real endpoints, in the order and shape the platform sends them — form-encoded,
with the field names and the quirks that actually arrive. The REST calls the
server makes back are intercepted by the mock layer in `vobiz.py`, and the agent
tool calls are fired through /api/simulate/tool.

What it proves: the webhook sequence, the handoff state machine, the two-agent
race, the no-answer fallback, and that cleanup is idempotent. What it does not
touch is the model — ../vobiz-gemini-live already proves that bridge on a real
call, and duplicating it here would only make this slower.

The server must be started with VOBIZ_MOCK=1, or it would place real calls:

    VOBIZ_MOCK=1 python app.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = os.getenv("HTTP_PORT", "8100")
BASE = ""
FAILURES: list[str] = []


def _http(path: str, data=None, form=False):
    url = f"{BASE}{path}"
    body, headers = None, {}
    if form:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST" if body else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as exc:
        return {"error": exc.code, "body": exc.read().decode()[:300]}
    except urllib.error.URLError:
        sys.exit(f"No server on {BASE}. Start it with:  VOBIZ_MOCK=1 python app.py")


def webhook(path: str, **fields):
    """A Vobiz callback: form-encoded, exactly as the platform sends them."""
    return _http(path, fields, form=True)


def state(tid: str) -> dict:
    return _http(f"/api/transfers/{tid}")


def expect(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"    pass  {label}")
    else:
        print(f"    FAIL  {label}{'  — ' + detail if detail else ''}")
        FAILURES.append(label)


def settle(seconds: float = 0.6):
    time.sleep(seconds)


# ---------------------------------------------------------------------------

def begin(agents: list[str]) -> dict:
    started = _http("/api/start", {
        "to": "+910000000001", "agents": agents,
    })
    if "error" in started:
        sys.exit(f"could not start: {started}")
    return started


def customer_answers(prefix: str, tid: str, call_uuid: str):
    xml = webhook(f"{prefix}/answer/customer", tid=tid, CallUUID=call_uuid,
                  Event="StartApp", Direction="outbound", CallStatus="in-progress",
                  From="91XXXXXXXXXX", To="+910000000001")
    return xml if isinstance(xml, str) else ""


def enter(prefix: str, tid: str, room: str, member: str, call_uuid: str, first: str):
    webhook(f"{prefix}/conf-events", tid=tid, Event="ConferenceEnter",
            ConferenceAction="enter", ConferenceName=room,
            ConferenceMemberID=member, ConferenceUUID="conf-" + tid,
            ConferenceFirstMember=first, CallUUID=call_uuid,
            Direction="outbound", CallStatus="in-progress")


def exit_room(prefix: str, tid: str, room: str, member: str, call_uuid: str, last: str):
    # Note the smaller field set: an exit carries no From/To/Direction/CallStatus.
    webhook(f"{prefix}/conf-events", tid=tid, Event="ConferenceExit",
            ConferenceAction="exit", ConferenceName=room,
            ConferenceMemberID=member, ConferenceUUID="conf-" + tid,
            ConferenceLastMember=last, CallUUID=call_uuid)


def said_by(tid: str) -> str:
    """What the agent said on THIS transfer. Scoped by tid, because the server
    keeps every scenario's events and an unscoped read is answered by whichever
    case ran before."""
    return " ".join(
        e.get("text", "") for e in _http("/api/events?limit=800")
        if e.get("kind") == "mock_said" and e.get("tid") == tid
    )


def turn(tid: str, role: str, text: str):
    return _http("/api/simulate/turn", {"tid": tid, "role": role, "text": text})


def tool(tid: str, role: str, name: str, **args):
    return _http("/api/simulate/tool",
                 {"tid": tid, "role": role, "name": name, "args": args})


# ---------------------------------------------------------------------------

def run(case: str):
    flow, prefix = "1", "/d1"
    print(f"\n  ── {case} ──")

    agents = ["+910000000002"]
    if case == "race":
        agents.append("sip:agent@registrar.vobiz.ai")

    started = begin(agents)
    tid, room = started["tid"], started["room"]
    customer = started["customer_uuid"]
    expect("customer call placed", bool(customer))

    xml = customer_answers(prefix, tid, customer)
    expect("customer answer XML returned", "<Response>" in xml)
    if flow == "1":
        expect("customer XML joins the room", f">{room}</Conference>" in xml)
        expect('customer XML sets stayAlone', 'stayAlone="true"' in xml)
    else:
        expect("customer XML opens a bidirectional stream",
               'bidirectional="true"' in xml and "keepCallAlive" in xml)

    if flow == "1":
        enter(prefix, tid, room, "101", customer, "true")
        settle(2.8)   # SETTLE_DELAY plus the attach
        snapshot = state(tid)
        expect("AI attached to the customer leg",
               bool(snapshot.get("customer_stream_id")),
               f"marks={list(snapshot.get('marks', {}))}")

    if case == "answered-then-loser-hangup":
        # Regression for the second live call. The tool fired AND the watchdog
        # forced, so the human's phone rang twice. They answered one leg; we
        # hung up the duplicate; that hangup was read as "the agent is gone" and
        # tore down the leg they had just answered, 400 ms after pickup.
        turn(tid, "caller", "Please connect me to a human.")
        turn(tid, "agent", "Sure, I'm bringing in a colleague.")
        tool(tid, "customer", "transfer_to_human",
             reason="wants a human", summary="Caller wants a human.")
        settle(1)
        snapshot = state(tid)
        expect("exactly one leg dialled per target",
               len(snapshot["agent_uuids"]) == len(agents),
               f"{len(snapshot['agent_uuids'])} legs for {len(agents)} target(s)")

        # let the watchdog window elapse — it must NOT place a second dial
        settle(16)
        snapshot = state(tid)
        expect("watchdog stood down once the tool fired",
               len(snapshot["agent_uuids"]) == len(agents),
               f"{len(snapshot['agent_uuids'])} legs after the watchdog window")

        winner = snapshot["agent_uuids"][0]
        webhook(f"{prefix}/answer/agent", tid=tid, CallUUID=winner,
                Event="StartApp", CallStatus="in-progress")
        settle()
        # now the duplicate leg hangs up, exactly as it did live
        webhook(f"{prefix}/hangup", tid=tid, CallUUID="a-duplicate-leg",
                Event="Hangup", CallStatus="completed")
        settle(1.5)
        snapshot = state(tid)
        expect("the answered leg survives the loser's hangup",
               snapshot["state"] == "agent_answered", snapshot["state"])
        said = said_by(tid)
        expect("customer is NOT told nobody is free",
               "call you back" not in said.lower(), said[:80])
        return

    if case == "member-controls":
        # Exercise every member control against a bridged room and assert the
        # real status codes: 202 for mute/deaf/play/speak/kick, 204 for the
        # DELETE-based unmute/undeaf, and member_id echoed back as an ARRAY.
        tool(tid, "customer", "transfer_to_human",
             reason="wants a human", summary="Caller wants a human.")
        settle(1)
        agent_uuid = state(tid)["agent_uuids"][0]
        webhook(f"{prefix}/answer/agent", tid=tid, CallUUID=agent_uuid,
                Event="StartApp", CallStatus="in-progress")
        tool(tid, "agent", "accept_transfer")
        settle(1)
        # Flow 1 puts the customer in as member 101 during the preamble (that
        # enter is what triggers the AI attach). Flow 2 has no room until the
        # bridge, so the customer has to be entered here.
        if flow == "2":
            enter(prefix, tid, room, "101", customer, "true")
        enter(prefix, tid, room, "402", agent_uuid, "false")
        settle(1)

        snapshot = state(tid)
        expect("both parties in the room", len(snapshot.get("members") or []) == 2,
               str(snapshot.get("members")))

        def ctl(action, member, **kw):
            return _http(f"/api/control/{action}", {"tid": tid, "member": member, **kw})

        for action, member, want in (
            ("mute",   "101", 202), ("unmute", "101", 204),
            ("deaf",   "101", 202), ("undeaf", "101", 204),
            ("speak",  "402", 202), ("play",   "402", 202),
            ("kick",   "402", 202),
        ):
            kw = {"text": "test"} if action == "speak" else (
                 {"url": "https://example.com/a.mp3"} if action == "play" else {})
            r = ctl(action, member, **kw)
            expect(f"{action} member {member} -> {want}", r.get("status") == want,
                   f"got {r.get('status')}")

        # "all" is a valid target and is echoed back literally, not expanded
        r = ctl("mute", "all")
        expect("mute all accepted", r.get("ok"), str(r))
        body = r.get("body") or {}
        expect("member_id comes back as an array",
               isinstance(body.get("member_id"), list), str(body))
        return

    if case == "retry-after-failure":
        # Regression for the second live call. The first handover failed, but
        # `agent_uuid` still named the dead leg — so when the caller asked again
        # and the same human answered, they were misread as a losing race entrant
        # and hung up on the instant they picked up. CDR proof: billsec 0,
        # "Normal Hangup / Answer XML".
        tool(tid, "customer", "transfer_to_human",
             reason="first try", summary="Caller wants a human.")
        settle(1)
        first = state(tid)["agent_uuids"][0]
        webhook(f"{prefix}/hangup", tid=tid, CallUUID=first, Event="Hangup",
                CallStatus="no-answer", HangupCause="NO_ANSWER")
        settle(1)
        snapshot = state(tid)
        events = _http("/api/events?limit=500")
        expect("first attempt reported as unavailable",
               any(e.get("kind") == "agent_unavailable" and e.get("tid") == tid
                   for e in events))
        # and then deliberately returns to bot_active, so the caller can ask again
        expect("caller can ask again", snapshot["state"] == "bot_active",
               snapshot["state"])
        expect("handover state reset for a retry",
               not snapshot["agent_uuid"] and not snapshot["agent_uuids"],
               f"uuid={snapshot['agent_uuid']!r} uuids={snapshot['agent_uuids']}")

        # caller asks again
        tool(tid, "customer", "transfer_to_human",
             reason="second try", summary="Caller still wants a human.")
        settle(1)
        snapshot = state(tid)
        expect("second attempt dials", len(snapshot["agent_uuids"]) == len(agents),
               f"{len(snapshot['agent_uuids'])} legs")

        second = snapshot["agent_uuids"][0]
        xml = webhook(f"{prefix}/answer/agent", tid=tid, CallUUID=second,
                      Event="StartApp", CallStatus="in-progress")
        xml = xml if isinstance(xml, str) else ""
        expect("the retry's agent is BRIEFED, not hung up on",
               "<Stream" in xml, xml[:110].replace(chr(10), " "))
        return

    if case == "stalled-tool":
        # Regression for the first live call: the model announced a colleague
        # and never called transfer_to_human, so nobody was dialled and the
        # caller waited on a person who had never been contacted.
        turn(tid, "caller", "Connect me to a human agent please.")
        turn(tid, "agent", "Sure thing! I'm bringing in a colleague for you.")
        snapshot = state(tid)
        expect("watchdog armed", "handover_expected" in snapshot.get("marks", {}),
               f"marks={list(snapshot.get('marks', {}))}")
        # nudge at 6s, forced dial at 6+8s
        settle(17)
        snapshot = state(tid)
        events = _http("/api/events?limit=400")
        expect("model was nudged", any(e.get("kind") == "tool_nudge" for e in events))
        expect("handover forced when the nudge was ignored",
               any(e.get("kind") == "tool_forced" for e in events))
        expect("a colleague was actually dialled",
               len(snapshot["agent_uuids"]) == len(agents),
               f"{len(snapshot['agent_uuids'])} dialled")
        expect("summary salvaged from the transcript",
               "human agent" in snapshot["summary"].lower(), snapshot["summary"][:70])
        return

    if case == "customer-drops":
        # The lead hangs up while the human is still ringing — the case the
        # race-condition doc is entirely about.
        tool(tid, "customer", "transfer_to_human",
             reason="wants a human", summary="Customer wants a human.")
        settle()
        webhook(f"{prefix}/hangup", tid=tid, CallUUID=customer, Event="Hangup",
                CallStatus="completed", HangupCause="NORMAL_CLEARING")
        settle()
        snapshot = state(tid)
        expect("transfer ended", snapshot["state"] == "ended", snapshot["state"])
        expect("cleanup ran", snapshot["cleanup_done"])
        webhook(f"{prefix}/hangup", tid=tid, CallUUID=customer, Event="Hangup")
        settle()
        expect("cleanup is idempotent", state(tid)["state"] == "ended")
        return

    tool(tid, "customer", "transfer_to_human",
         reason="billing dispute",
         summary="Test Customer, invoice 4471, charged twice in March. "
                 "Identity verified. Wants the duplicate refunded.")
    settle()
    snapshot = state(tid)
    expect("agent legs dialled", len(snapshot["agent_uuids"]) == len(agents),
           f"{len(snapshot['agent_uuids'])} of {len(agents)}")
    expect("summary captured", "invoice 4471" in snapshot["summary"])

    if case == "no-answer":
        for uuid_ in snapshot["agent_uuids"]:
            webhook(f"{prefix}/hangup", tid=tid, CallUUID=uuid_, Event="Hangup",
                    CallStatus="no-answer", HangupCause="NO_ANSWER",
                    HangupCauseCode="6010")
        settle()
        settle(1)
        snapshot = state(tid)
        expect("reported as unavailable",
               any(e.get("kind") == "agent_unavailable" and e.get("tid") == tid
                   for e in _http("/api/events?limit=500")))
        # then reset to bot_active on purpose, so asking again works
        expect("caller can ask again", snapshot["state"] == "bot_active",
               snapshot["state"])
        said = said_by(tid)
        expect("customer was told, not left silent", "call you back" in said.lower(), said[:80])
        return

    agent_uuid = snapshot["agent_uuids"][0]
    xml = webhook(f"{prefix}/answer/agent", tid=tid, CallUUID=agent_uuid,
                  Event="StartApp", Direction="outbound", CallStatus="in-progress")
    xml = xml if isinstance(xml, str) else ""
    expect("agent gets a briefing stream", 'bidirectional="true"' in xml)
    if flow == "1":
        expect("briefing happens before the room is joined",
               xml.index("<Stream") < xml.index("<Conference"))
        expect("agent XML names the same room", f">{room}</Conference>" in xml)
    else:
        expect("agent XML has no conference yet", "<Conference" not in xml)

    if case == "race":
        loser = snapshot["agent_uuids"][1]
        losing_xml = webhook(f"{prefix}/answer/agent", tid=tid, CallUUID=loser,
                             Event="StartApp", CallStatus="in-progress")
        losing_xml = losing_xml if isinstance(losing_xml, str) else ""
        expect("second agent is hung up, not briefed",
               "<Hangup/>" in losing_xml and "<Stream" not in losing_xml)
        expect("winner unchanged", state(tid)["agent_uuid"] == agent_uuid)

    if case == "declined":
        tool(tid, "agent", "reject_transfer", reason="not my area")
        settle()
        snapshot = state(tid)
        expect("state is rejected", snapshot["state"] == "rejected", snapshot["state"])

        # Live bug: closing the briefing socket released keepCallAlive, so a
        # colleague who had just declined still walked into the room — and that
        # join was announced to the customer as "my colleague is on the line".
        agent_uuid = snapshot["agent_uuids"][0]
        enter(prefix, tid, room, "298", agent_uuid, "false")
        settle(1)
        said = said_by(tid)
        expect("a declined agent is not announced as connected",
               "on the line now" not in said.lower(), said[-90:])
        expect("state stays rejected after the stray join",
               state(tid)["state"] == "rejected", state(tid)["state"])
        said = said_by(tid)
        expect("customer's agent was told the colleague declined",
               any(e.get("kind") == "mock_informed" and e.get("tid") == tid
                   and "DECLINED" in e.get("text", "")
                   for e in _http("/api/events?limit=500")))
        expect("customer told the colleague could not help",
               "take it from here" in said.lower(), said[:80])
        return

    tool(tid, "agent", "accept_transfer")
    settle()
    expect("state is accepted or bridging", state(tid)["state"] in ("accepted", "bridged"))

    if flow == "2":
        calls = _http("/api/events?limit=400")
        bridged = [e for e in calls if e.get("kind") == "bridge_transfer"]
        expect("both legs transferred into a room", bool(bridged))
        # The Transfer replaces the customer's XML document, and their stream
        # goes with it.
        webhook(f"{prefix}/stream-status", tid=tid, CallUUID=customer,
                Event="DroppedStream", StreamID="stub")
        enter(prefix, tid, room, "101", customer, "true")
        settle(2.8)
        snapshot = state(tid)
        expect("AI re-attached after the Transfer",
               bool(snapshot.get("customer_stream_id")))
        blackout = snapshot.get("marks", {})
        expect("blackout measured",
               "customer_stream_down" in blackout and "ai_reattached" in blackout,
               f"marks={list(blackout)}")

    enter(prefix, tid, room, "102", agent_uuid, "false")
    settle()
    snapshot = state(tid)
    expect("all parties bridged", snapshot["state"] == "bridged", snapshot["state"])
    expect("roster has two members", len(snapshot.get("members") or []) >= 2,
           f"members={snapshot.get('members')}")

    exit_room(prefix, tid, room, "101", customer, "false")
    webhook(f"{prefix}/hangup", tid=tid, CallUUID=customer, Event="Hangup",
            CallStatus="completed", HangupCause="NORMAL_CLEARING",
            Duration="94", BillDuration="120", TotalCost="0.00000")
    settle()
    snapshot = state(tid)
    expect("transfer ended cleanly", snapshot["state"] == "ended", snapshot["state"])
    # The webhook says nothing about cost; the CDR fetch is what fills it in,
    # and it is deliberately deferred a couple of seconds past hangup.
    settle(2.6)
    cdrs = [e for e in _http("/api/events?limit=400") if e.get("kind") == "cdr"]
    expect("a CDR was fetched for every leg", len(cdrs) >= 1 + len(agents),
           f"{len(cdrs)} CDRs")


def main():
    global BASE, PORT
    parser = argparse.ArgumentParser(description="Run a warm transfer with no phone call")
    parser.add_argument("--case", default="happy",
                        choices=["happy", "declined", "no-answer", "race",
                                 "customer-drops", "stalled-tool",
                                 "answered-then-loser-hangup",
                                 "retry-after-failure", "member-controls"])
    parser.add_argument("--all", action="store_true", help="every case on both flows")
    parser.add_argument("--port", default=PORT)
    args = parser.parse_args()

    PORT = args.port
    BASE = f"http://127.0.0.1:{PORT}"

    health = _http("/health")
    if not isinstance(health, dict):
        sys.exit(f"No server answering on {BASE}.")
    # Hard stop. /api/start places a REAL outbound call on a live server, and
    # this harness fires it repeatedly with synthetic numbers. Running it
    # against a non-mock server once cost four real calls to a stranger.
    if not health.get("vobiz_mock"):
        sys.exit(
            f"REFUSING TO RUN: the server on {BASE} is NOT in mock mode.\n"
            f"  /api/start would place real outbound calls.\n"
            f"  Start it with:  VOBIZ_MOCK=1 python app.py\n"
            f"  Or point this at the mock server:  --port <port>"
        )

    if args.all:
        for case in ("happy", "declined", "no-answer", "race", "customer-drops",
                     "stalled-tool", "answered-then-loser-hangup",
                     "retry-after-failure", "member-controls"):
            run(case)
    else:
        run(args.case)

    print()
    if FAILURES:
        print(f"  {len(FAILURES)} failed: " + ", ".join(FAILURES))
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
