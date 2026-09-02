#!/usr/bin/env python3
"""
report.py — what happened on one warm transfer
===============================================

    python report.py                 the most recent transfer
    python report.py <tid>
    python report.py --download      also fetch the recordings and split them
    python report.py --recordings 5  list the newest recordings on the account

Four sections:

  FLOW       every webhook Vobiz sent, in arrival order, with gaps
  WEBHOOKS   which documented events arrived, and which never did
  PRICING    per-leg CDR, the pulse arithmetic, and the all-in total
  RECORDING  what exists, where to fetch it, and how to split the channels

Cost is CDR-only — every webhook reports TotalCost 0.00000 on a PSTN leg — and
`total_cost` is the all-in figure that already contains `streaming_cost`.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import logging
import math
import os
import sys
import urllib.error
import urllib.request

import httpx

import vobiz

OUT = pathlib.Path(__file__).parent / "recordings"

# This tool reports its own diagnostics; the client's per-request
# logging would otherwise print a raw 401 ahead of them.
logging.getLogger("vobiz").setLevel(logging.CRITICAL)

PORT = os.getenv("HTTP_PORT", "8100")
PULSE = 60
VOICE_RATE = 0.45      # per 60s pulse, measured
STREAM_RATE = 0.20     # per 60s pulse, measured — NOT the 0.3 channel variable

# Events the docs say to expect on a conference warm transfer.
EXPECTED = ["StartApp", "ConferenceEnter", "ConferenceExit", "Hangup",
            "StartStream", "PlayedStream", "DroppedStream"]


def api(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError:
        return None          # 404 before the first call is the normal state
    except urllib.error.URLError:
        sys.exit(f"No server on port {PORT}. Start it with:  python app.py")


def f(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0


def pulses(billsec):
    s = f(billsec)
    return math.ceil(s / PULSE) if s > 0 else 0



# ---------------------------------------------------------------------------
# Downloading the audio
# ---------------------------------------------------------------------------

def kind_of(blob: bytes) -> str:
    """
    Identify the container from its magic bytes, because the extension and the
    Content-Type both lie here.

    An MP3 frame starts with an 11-bit sync word — `0xFF` then the top three
    bits of the next byte set — which covers MPEG-1, MPEG-2 and MPEG-2.5. A
    fixed list of second bytes misses versions: these recordings arrive as
    MPEG-2.5 layer III (`0xFF 0xE3`), which a `\xff\xfb`/`\xf3`/`\xf2` check
    rejects.
    """
    if blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return "wav"
    if blob[:3] == b"ID3":
        return "mp3"
    if len(blob) > 1 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0:
        return "mp3"
    return "bin"


async def fetch(row: dict) -> Path | None:
    url = row.get("recording_url")
    if not url:
        print("    no recording_url on this row")
        return None
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        r = await c.get(url, headers=vobiz.HEADERS)
    if r.status_code != 200:
        print(f"    download failed: HTTP {r.status_code}")
        return None

    real = kind_of(r.content)
    claimed = (row.get("recording_format") or "mp3").lower()
    name = f"{row.get('recording_id','rec')[:8]}.{real}"
    path = OUT / name
    path.write_bytes(r.content)
    note = "" if real == claimed else f"  ← served as .{claimed}, actually {real.upper()}"
    print(f"    saved {path.name}  {len(r.content):,} bytes{note}")
    return path


def split(path: Path):
    """Separate the two parties, when the file is stereo and ffmpeg is around."""
    if not shutil.which("ffmpeg"):
        print("    (ffmpeg not installed — skipping the channel split)")
        return
    # Keep the field names: positional parsing silently mislabelled
    # sample_rate as the channel count.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,channels,sample_rate,duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True)
    info = dict(
        line.split("=", 1) for line in probe.stdout.strip().splitlines() if "=" in line
    )
    if not info:
        return
    print(f"    {info.get('codec_name','?')}  {info.get('channels','?')} channel(s)  "
          f"{info.get('sample_rate','?')} Hz  {info.get('duration','?')}s")
    if info.get("channels") != "2":
        print("    mono — nothing to split")
        return
    left, right = path.with_name(path.stem + "_A.wav"), path.with_name(path.stem + "_B.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-filter_complex", "channelsplit=channel_layout=stereo[l][r]",
         "-map", "[l]", str(left), "-map", "[r]", str(right)],
        check=False)
    if left.exists():
        print(f"    split -> {left.name} (one party)  {right.name} (the other)")


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    download = "--download" in sys.argv

    if "--recordings" in sys.argv:
        n = int(args[0]) if args and args[0].isdigit() else 10
        r = await vobiz.list_recordings(limit=n)
        if not r.ok:
            sys.exit(f"\n  Could not list recordings: HTTP {r['status']}\n"
                     f"  Check VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN in .env.\n")
        body = r.get("body") or {}
        rows = body.get("objects") or []
        if not rows:
            print("\n  No recordings on this account yet.\n"); return
        print(f"\n  {(body.get('meta') or {}).get('total_count')} on the account. "
              f"Newest {len(rows)}:\n")
        for x in rows:
            print(f"  {x.get('recording_id')}")
            print(f"    {x.get('add_time')}   call={str(x.get('call_uuid'))[:8]}"
                  f"   {x.get('recording_duration_ms')}ms")
        print()
        return

    tid = args[0] if args else "last"
    d = api(f"/api/transfers/{tid}")
    if not d or "error" in d:
        if tid == "last":
            sys.exit("  No calls recorded yet. Place one with:  python call.py")
        sys.exit(f"  No transfer {tid}. Run report.py with no arguments for the latest.")
    tid = d["tid"]
    events = [e for e in api("/api/events?limit=1200") if e.get("tid") == tid]

    print(f"\n{'='*74}\n  TRANSFER {tid}   flow={d['flow']}   state={d['state']}   room={d['room']}\n{'='*74}")

    # ---------- FLOW ----------
    print("\n  FLOW — every webhook, in arrival order\n")
    hooks = [e for e in events if e.get("kind") in
             ("answer_customer","answer_agent","conference_event","stream_status",
              "hangup","recording_callback","join","member_control")]
    if not hooks:
        print("   (none captured)")
    t0 = hooks[0]["t"] if hooks else 0
    prev = t0
    for e in hooks:
        b = e.get("body") or {}
        name = b.get("Event") or e.get("kind")
        extra = ""
        if e["kind"] == "conference_event":
            extra = f"member={b.get('ConferenceMemberID')} last={b.get('ConferenceLastMember')}"
        elif e["kind"] == "member_control":
            name = f"CONTROL {e.get('action')}"
            extra = f"member={e.get('member')} -> HTTP {e.get('status')}"
        elif e["kind"] == "hangup":
            extra = (f"{b.get('HangupCause')} / {b.get('HangupSource')} "
                     f"dur={b.get('Duration')} cost={b.get('TotalCost')}")
        print(f"   +{e['t']-t0:7.2f}s  (+{e['t']-prev:5.2f})  {name:<22} {extra}")
        prev = e["t"]

    # ---------- WEBHOOKS ----------
    print("\n  WEBHOOKS — documented vs received\n")
    seen = set()
    for e in events:
        b = e.get("body") or {}
        if b.get("Event"):
            seen.add(b["Event"])
    for name in EXPECTED:
        mark = "yes" if name in seen else "NO"
        print(f"   {name:<20} {mark}")
    extra = sorted(seen - set(EXPECTED))
    if extra:
        print(f"\n   also received: {', '.join(extra)}")
    print("\n   note: TotalCost / BillRate are 0.00000 on every one of these.")

    # ---------- PRICING ----------
    print("\n  PRICING — per leg, from the CDR\n")
    legs = [("customer", d.get("customer_uuid",""))]
    for i, u in enumerate(d.get("agent_uuids") or [], 1):
        legs.append(("agent" if u == d.get("agent_uuid") else f"agent-{i} (unused)", u))

    hdr = f"   {'leg':<20} {'billsec':>7} {'ring':>5} {'pulses':>6} {'voice':>7} {'stream':>7} {'total':>7}"
    print(hdr); print("   " + "-"*(len(hdr)-3))
    grand = 0.0
    for role, uuid_ in legs:
        if not uuid_: continue
        r = await vobiz.get_cdr(uuid_)
        b = r.get("body") or {}
        cdr = b.get("data") if isinstance(b.get("data"), dict) else b
        if not r.ok or not isinstance(cdr, dict):
            print(f"   {role:<20} CDR {r['status']}"); continue
        p = pulses(cdr.get("billsec"))
        total = f(cdr.get("total_cost")); strm = f(cdr.get("streaming_cost"))
        grand += total
        print(f"   {role:<20} {str(cdr.get('billsec')):>7} {str(cdr.get('ring_time')):>5} "
              f"{p:>6} {total-strm:>7.2f} {strm:>7.2f} {total:>7.2f}")
        exp = p*VOICE_RATE + (p*STREAM_RATE if strm else 0)
        if abs(exp - total) > 0.005:
            print(f"   {'':<20} ^ expected {exp:.2f} from {p}×{VOICE_RATE}"
                  f"{f' + {p}×{STREAM_RATE}' if strm else ''} — MISMATCH")
    print("   " + "-"*(len(hdr)-3))
    print(f"   {'ALL LEGS':<20} {'':>7} {'':>5} {'':>6} {'':>7} {'':>7} {grand:>7.2f} INR")
    print(f"\n   rate card, measured: voice {VOICE_RATE}/60s pulse, "
          f"streaming {STREAM_RATE}/60s pulse (all-in {VOICE_RATE+STREAM_RATE}/min)")
    print("   total_cost ALREADY INCLUDES streaming_cost — do not add them.")

    # ---------- RECORDING ----------
    print("\n  RECORDING\n")
    action = [e for e in events if e.get("kind") == "record_action"]
    cbs = [e for e in events if e.get("kind") == "recording_callback"]
    rest = [e for e in events if e.get("kind") == "recording_start"]

    if action:
        b = action[0].get("body") or {}
        print(f"   <Record> started — RecordingID {b.get('RecordingID')}")
        print(f"     duration fields are -1 at start, by design: "
              f"RecordingDuration={b.get('RecordingDuration')}")
    if rest:
        print(f"   conference REST route: HTTP {rest[0]['result']['status']}  "
              f"{rest[0]['result'].get('body')}   (known to produce nothing)")
    if not action and not rest:
        print("   no recording attempted (set RECORD_ROOM=true)")

    print(f"\n   RecordStop callbacks: {len(cbs)}")
    for c in cbs:
        b = c.get("body") or {}
        print(f"     Event={b.get('Event')}  {b.get('RecordingDuration')}s  "
              f"{b.get('RecordingDurationMs')}ms")
        print(f"     {b.get('RecordUrl') or b.get('RecordFile')}")

    r = await vobiz.list_recordings(limit=25)
    body = r.get("body") or {}
    rows = body.get("objects") or body.get("data") or []
    total = (body.get("meta") or {}).get("total_count")
    uuids = {d.get("customer_uuid"), d.get("agent_uuid")} | set(d.get("agent_uuids") or [])
    mine = [x for x in rows if x.get("call_uuid") in uuids
            or x.get("conference_name") == d["room"]]
    confs = [x for x in rows if x.get("conference_name")]

    print(f"\n   account has {total} recordings; this page {len(rows)}; "
          f"matching this call {len(mine)}")
    for x in mine:
        print(f"     {x.get('recording_url')}")
        print(f"       {x.get('recording_duration_ms')}ms  {x.get('recording_format')}  "
              f"rounded={x.get('rounded_recording_duration')}s  "
              f"rate={x.get('recording_storage_rate')}")
    if not mine:
        print("     none — see the note below")
    print(f"\n   rows on this page with conference_name set: {len(confs)}")
    print("   conference recording produces no rows at all — not via")
    print("   <Conference record=\"true\"> (gated on an S3 URL) nor via")
    print("   POST /Conference/{room}/Record/ (returns 200, records nothing).")
    print("   Per-leg <Record recordSession=\"true\"> is the working method.")
    if download and mine:
        OUT.mkdir(exist_ok=True)
        print()
        for x in mine:
            path = await fetch(x)
            if path:
                split(path)
        print(f"   files in {OUT}/")
    elif mine:
        print("\n   pass --download to fetch these and split the channels")
    print()


if __name__ == "__main__":
    asyncio.run(main())
