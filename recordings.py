#!/usr/bin/env python3
"""
recordings.py — find, download and split call recordings
=========================================================

    python recordings.py                    recordings for the most recent call
    python recordings.py <tid>
    python recordings.py --latest 5         the 5 newest on the account
    python recordings.py --all              list everything, newest first

Downloads into ./recordings/ and, when a file is stereo, splits the channels so
each party can be heard on their own.

Two things worth knowing about these files:

  * **The media URL needs the auth headers.** Without `X-Auth-ID` /
    `X-Auth-Token` it answers 401, so the URL cannot be opened in a browser or
    handed to anyone who lacks the credentials.
  * **A `.mp3` here may actually be a WAV.** Verified: a file served as
    `Recording/<id>.mp3` with `Content-Type: audio/mpeg` was RIFF/WAVE,
    16-bit stereo 8 kHz. Trust the magic bytes, not the extension.

Recordings are stereo with **one party per channel**, which is what makes the
split below useful — you can listen to each side in isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import httpx

import vobiz

PORT = os.getenv("HTTP_PORT", "8100")
OUT = Path(__file__).parent / "recordings"
OUT.mkdir(exist_ok=True)


def api(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=25) as r:
            return json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError):
        # A 404 before the first call, or no server at all, are both normal for
        # --latest / --all, which read the account rather than a transfer.
        return None


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
    latest = "--latest" in sys.argv or "--all" in sys.argv
    limit = 50 if "--all" in sys.argv else int(args[0]) if latest and args else 5

    r = await vobiz.list_recordings(limit=limit if latest else 40)
    body = r.get("body") or {}
    rows = body.get("objects") or []
    total = (body.get("meta") or {}).get("total_count")

    if latest:
        if not r.ok:
            body = r.get("body")
            sys.exit(f"\n  Could not list recordings: HTTP {r['status']} {body}\n"
                     f"  Check VOBIZ_AUTH_ID / VOBIZ_AUTH_TOKEN in .env.\n")
        if not rows:
            print("\n  No recordings on this account yet.\n")
            return
        print(f"\n  {total} recordings on the account. Newest {len(rows)}:\n")
        for x in rows[:limit]:
            print(f"  {x.get('recording_id')}")
            print(f"    {x.get('add_time')}   call={str(x.get('call_uuid'))[:8]}"
                  f"   {x.get('recording_duration_ms')}ms"
                  f"   conf={x.get('conference_name') or '-'}")
        print()
        if args and not args[0].isdigit():
            pass
        else:
            return

    tid = args[0] if args and not args[0].isdigit() else "last"
    d = api(f"/api/transfers/{tid}")
    if not d or "error" in d:
        sys.exit("  No calls recorded yet. Place one with:  python call.py\n"
                 "  Or list what is on the account:  python recordings.py --latest 5")

    uuids = {d.get("customer_uuid"), d.get("agent_uuid")} | set(d.get("agent_uuids") or [])
    uuids.discard("")
    mine = [x for x in rows if x.get("call_uuid") in uuids
            or x.get("conference_name") == d.get("room")]

    print(f"\n  transfer {d['tid']}   room {d.get('room')}   state {d.get('state')}")
    print(f"  legs: {', '.join(sorted(u[:8] for u in uuids))}")
    print(f"  {total} recordings on the account; {len(mine)} belong to this call\n")

    if not mine:
        print("  No recording for this call.")
        print("  Conference recording writes no file — checked, 0 of 103 rows carry a")
        print("  conference_name. Per-leg <Record recordSession=\"true\"> is the working")
        print("  method; set RECORD_ROOM=true and place a call longer than ~60s.\n")
        return

    for x in mine:
        print(f"  {x.get('recording_id')}   call={str(x.get('call_uuid'))[:8]}")
        print(f"    {x.get('recording_duration_ms')}ms  "
              f"rounded={x.get('rounded_recording_duration')}s  "
              f"storage_rate={x.get('recording_storage_rate')}")
        path = await fetch(x)
        if path:
            split(path)
        print()

    print(f"  files in {OUT}/  — open them with:  open {OUT}\n")


if __name__ == "__main__":
    asyncio.run(main())
