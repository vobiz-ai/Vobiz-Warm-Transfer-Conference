#!/usr/bin/env python3
"""
call.py — place one demo call
==============================

    python call.py                       ring the numbers in .env
    python call.py --agent sip           reach the human on a SIP/WebRTC endpoint
    python call.py --agent both          ring PSTN and SIP, first to accept wins
    python call.py --to +9198... --agent-number +9199...

The work happens in the server, not here: `POST /api/start` places the customer
call and registers where `transfer_to_human` should ring. This script is only a
front door, so that the transfer record and both Gemini sessions stay inside the
one process that owns them.

Run `python selftest.py` first. It is much cheaper than a call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PORT = os.getenv("HTTP_PORT", "8100")
CUSTOMER = os.getenv("CUSTOMER_NUMBER", "") or os.getenv("TO_NUMBER", "")
AGENT_NUMBER = os.getenv("AGENT_NUMBER", "")
AGENT_SIP = os.getenv("AGENT_SIP", "")


def api(path: str, payload: dict | None = None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"{path} -> {exc.code} {exc.read().decode()[:300]}")
    except urllib.error.URLError:
        sys.exit(f"No server on port {PORT}. Start it with:  python app.py")


def main():
    parser = argparse.ArgumentParser(description="Place one warm-transfer demo call")
    parser.add_argument("--agent", choices=["pstn", "sip", "both"], default="pstn",
                        help="where the human agent is reached")
    parser.add_argument("--to", default=CUSTOMER, help="the customer's number")
    parser.add_argument("--agent-number", default=AGENT_NUMBER)
    parser.add_argument("--agent-sip", default=AGENT_SIP)
    parser.add_argument("--room", default="", help="conference room name")
    args = parser.parse_args()

    if not args.to:
        sys.exit("No customer number. Set CUSTOMER_NUMBER in .env or pass --to")

    targets: list[str] = []
    if args.agent in ("pstn", "both"):
        if not args.agent_number:
            sys.exit("No AGENT_NUMBER in .env and no --agent-number given")
        targets.append(args.agent_number)
    if args.agent in ("sip", "both"):
        if not args.agent_sip:
            sys.exit("No AGENT_SIP in .env and no --agent-sip given")
        if ":" in args.agent_sip.split("@")[-1]:
            sys.exit(
                "AGENT_SIP must not carry an explicit port — a SIP URI with one "
                "makes the dial silently no-op, with no INVITE and no error"
            )
        targets.append(args.agent_sip)

    health = api("/health")
    if health.get("vobiz_credentials") != "set":
        sys.exit("Vobiz credentials are missing from .env")
    if health.get("gemini", {}).get("api_key") != "set":
        sys.exit("GEMINI_API_KEY is missing from .env")

    print(f"  ai mode   {health.get('ai_mode')}")
    print(f"  customer  {args.to}")
    for target in targets:
        print(f"  agent     {target}")
    print()

    result = api("/api/start", {
        "to": args.to, "agents": targets, "room": args.room,
    })
    print(f"  tid       {result['tid']}")
    print(f"  room      {result['room']}")
    print(f"  call      {result['customer_uuid']}")
    print()
    print("  Answer, talk to the agent, then ask for a human.")
    print(f"  watch:   curl -s localhost:{PORT}/api/transfers | python3 -m json.tool")
    print(f"  cost:    python cost.py {result['tid']}")
    print(f"  console: http://localhost:{PORT}/panel")


if __name__ == "__main__":
    main()
