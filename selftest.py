#!/usr/bin/env python3
"""
selftest.py — parse every answer document before spending a call
=================================================================

Malformed answer XML is not reported as an HTTP error. Vobiz accepts the 200,
answers the call, and drops it about a second later; the only explanation is in
the CDR, as `hangup_cause_name: "Invalid Answer XML"` / code 8011. That costs a
live call and several minutes to discover, so every document both flows can
return is built and parsed here first.

    python selftest.py            structure only
    python selftest.py --gemini   also opens a real Gemini Live session

Checks per document: well-formed; a single <Response> root; only elements Vobiz
recognises; every URL absolute and correctly escaped; the room name present
where a <Conference> needs one.
"""

from __future__ import annotations

import os
import sys
from xml.etree import ElementTree

os.environ.setdefault("PUBLIC_URL", "https://selftest.example.com")

import runtime  # noqa: E402
import xml_docs  # noqa: E402

KNOWN = {
    "Response", "Conference", "Stream", "Speak", "Play", "Wait",
    "Hangup", "Dial", "Number", "User", "Record", "Redirect", "Gather",
}

PASS, FAIL = [], []


def check(label: str, document: str, *, expect_room: str = "", expect_ws: bool = False):
    problems: list[str] = []

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        FAIL.append((label, [f"not well-formed: {exc}"]))
        return

    if root.tag != "Response":
        problems.append(f"root is <{root.tag}>, expected <Response>")

    elements = [root.tag] + [e.tag for e in root.iter() if e is not root]
    for tag in elements:
        if tag not in KNOWN:
            problems.append(f"unknown element <{tag}>")

    # A bare '&' inside an attribute is the single most common way to kill a
    # call, and it survives string formatting silently.
    raw = document
    for attribute in ("callbackUrl=", "waitSound=", "statusCallbackUrl="):
        start = 0
        while (idx := raw.find(attribute, start)) != -1:
            value = raw[idx + len(attribute):]
            quote = value[0]
            value = value[1:value.index(quote, 1)]
            if value and not value.startswith(("http://", "https://")):
                problems.append(f"{attribute}{value!r} is not absolute")
            if "&" in value and "&amp;" not in value:
                problems.append(f"{attribute}{value!r} has an unescaped &")
            start = idx + len(attribute)

    for stream in root.iter("Stream"):
        url = (stream.text or "").strip()
        if not url.startswith(("ws://", "wss://")):
            problems.append(f"<Stream> body {url!r} is not a websocket URL")
        if stream.get("bidirectional") == "true" and stream.get("keepCallAlive") != "true":
            problems.append("bidirectional <Stream> without keepCallAlive")
        if stream.get("bidirectional") == "true" and stream.get("audioTrack") not in (None, "inbound"):
            problems.append("bidirectional <Stream> must not use audioTrack both/outbound")

    conferences = list(root.iter("Conference"))
    for conference in conferences:
        if not (conference.text or "").strip():
            problems.append("<Conference> has no room name")
        if conference.get("stayAlone") != "true":
            # A lone member is kicked straight back out; the customer is alone
            # for the first several seconds of every warm transfer.
            problems.append("<Conference> without stayAlone=\"true\"")
    if expect_room and not any((c.text or "").strip() == expect_room for c in conferences):
        problems.append(f"expected room {expect_room!r}")

    if expect_ws and not list(root.iter("Stream")):
        problems.append("expected a <Stream> and found none")

    # The rule that governs both flows, and the easiest one to get wrong.
    #
    # A bidirectional <Stream> BLOCKS its leg. Put one before a <Conference> and
    # the two can never be simultaneous — which is why an AI that must hear the
    # room is attached over the Stream REST API instead, as a media bug outside
    # XML sequencing.
    #
    # Sequentially, though, the pair is exactly right, and it is what makes the
    # briefing work: keepCallAlive="true" holds the leg on the stream until the
    # socket closes, then the document resumes at the next element. So the AI
    # briefs the human privately and the leg walks into the room afterwards.
    #
    # Without keepCallAlive the handover point is undefined, and that is the
    # case worth failing on.
    for index, element in enumerate(list(root)):
        if element.tag != "Stream":
            continue
        after = [e.tag for e in list(root)[index + 1:]]
        if "Conference" not in after:
            continue
        if element.get("bidirectional") == "true":
            if element.get("keepCallAlive") != "true":
                problems.append(
                    "bidirectional <Stream> before <Conference> without "
                    'keepCallAlive="true": the leg has no defined moment to '
                    "leave the stream and join the room"
                )
        elif element.get("audioTrack") != "both":
            # The non-blocking tap only earns its place if it hears the room.
            problems.append(
                'non-bidirectional <Stream> before <Conference> should use '
                'audioTrack="both" so the tap hears the room mix'
            )

    (FAIL if problems else PASS).append((label, problems))


def main():
    room = "warmdemo123"
    events = runtime.url("/d1/conf-events", tid="abc123")
    wait = runtime.url("/d1/wait", tid="abc123")
    status = runtime.url("/d1/stream-status", tid="abc123")
    ws_customer = runtime.ws_url("/ws", role="customer", tid="abc123")
    ws_agent = runtime.ws_url("/ws", role="agent", tid="abc123")

    check("customer (duplex)",
          xml_docs.conference_customer(room, events, wait), expect_room=room)
    check("customer + session recording",
          xml_docs.conference_customer(
              room, events, wait,
              record=xml_docs.record_block(
                  runtime.url("/d1/record-action", tid="abc123"),
                  runtime.url("/d1/record-callback", tid="abc123"))),
          expect_room=room)
    check("customer (tap fallback)",
          xml_docs.conference_customer(room, events, wait, tap_ws=ws_customer),
          expect_room=room, expect_ws=True)
    check("agent + session recording",
          xml_docs.conference_agent(
              room, events, ws_agent, status, "tid=abc123",
              record=xml_docs.record_block(
                  runtime.url("/d1/record-action", tid="abc123", leg="agent"),
                  runtime.url("/d1/record-callback", tid="abc123", leg="agent"))),
          expect_room=room, expect_ws=True)
    check("agent (brief then join)",
          xml_docs.conference_agent(room, events, ws_agent, status, "tid=abc123"),
          expect_room=room, expect_ws=True)
    check("wait sound", xml_docs.conference_wait())
    check("agent declined", xml_docs.agent_declined())

    check("empty response", xml_docs.empty())
    check("hangup with message", xml_docs.hangup("Nobody is available right now."))

    # The escaping check that matters: an ampersand in a caller-supplied value
    # must not be able to break the document.
    check("escaping — & in room name",
          xml_docs.conference_customer("a&b<c", events, wait))
    check("escaping — two query params",
          xml_docs.conference_customer(
              room, runtime.url("/d1/conf-events", tid="a", room="b"), wait),
          expect_room=room)

    for label, _ in PASS:
        print(f"  pass  {label}")
    for label, problems in FAIL:
        print(f"  FAIL  {label}")
        for problem in problems:
            print(f"          {problem}")

    print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")

    if "--gemini" in sys.argv:
        print("\n  opening a real Gemini Live session…")
        import asyncio

        import gemini_live

        async def probe():
            import google.genai as genai
            client = genai.Client(api_key=gemini_live.GEMINI_API_KEY)
            persona = gemini_live.customer_persona("audio/x-l16", 24000)
            async with client.aio.live.connect(
                model=gemini_live.GEMINI_MODEL,
                config=gemini_live._live_config(persona),
            ):
                print(f"  pass  Gemini Live connected — {gemini_live.GEMINI_MODEL}")

        try:
            asyncio.run(probe())
        except Exception as exc:
            print(f"  FAIL  Gemini Live: {type(exc).__name__}: {exc}")
            print("        run  python ../vobiz-gemini-live/models.py  "
                  "to see which models this key can use")
            return 1

    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
