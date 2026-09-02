"""
app.py — the warm-transfer server
=================================

    /d1/*   the warm-transfer call flow
    /ws     the media socket for every Gemini session, routed by ?role&tid
    /panel  a live console for the conference member controls

Run it, then drive it from `call.py` (real calls) or `mock.py` (no calls).

The capture middleware below is the reason a run can be argued about afterwards:
it records **every** request that reaches the process, including paths nothing
is routed to, together with the XML that was returned. An unexpected Vobiz
callback is answered 200 and stored rather than 404'd and lost.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

import flow_conference
import gemini_live
import runtime
import store
import vobiz

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(name)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app")

app = FastAPI(title="Vobiz warm call transfer")


# ---------------------------------------------------------------------------
# Capture — pure ASGI so the response body is visible too
# ---------------------------------------------------------------------------

class WebhookCapture:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        chunks: list[bytes] = []

        async def buffered_receive():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        started = time.time()
        status = {"code": 0}
        reply: list[bytes] = []

        async def mirrored_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            elif message["type"] == "http.response.body":
                reply.append(message.get("body", b""))
            await send(message)

        await self.inner(scope, buffered_receive, mirrored_send)

        path = scope.get("path", "")
        if path.startswith(("/api", "/health", "/ws")):
            return

        body = b"".join(chunks)
        store.record(
            "http",
            {
                "method": scope.get("method"),
                "path": path,
                "query": scope.get("query_string", b"").decode(),
                "body": body.decode("utf-8", "replace")[:4000],
                "user_agent": dict(
                    (k.decode(), v.decode()) for k, v in scope.get("headers") or []
                ).get("user-agent", ""),
                "status": status["code"],
                "ms": round((time.time() - started) * 1000, 1),
                "reply": b"".join(reply).decode("utf-8", "replace")[:4000],
            },
        )


app.include_router(flow_conference.router)


# ---------------------------------------------------------------------------
# Media socket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def media(ws: WebSocket):
    await ws.accept()
    params = ws.query_params
    role = params.get("role", "customer")
    tid = params.get("tid", "")
    mode = params.get("mode", "")

    session = flow_conference.build_session(ws, role, tid, mode)
    if session is None:
        logger.error(f"/ws for unknown transfer {tid!r} — closing")
        await ws.close()
        return

    logger.info(f"/ws open — role={role} tid={tid} mode={mode or 'default'}")
    try:
        while True:
            await session.handle(await ws.receive_text())
    except WebSocketDisconnect:
        logger.info(f"/ws closed — role={role} tid={tid}")
    except Exception as exc:
        logger.error(f"/ws error: {type(exc).__name__}: {exc}")
    finally:
        await session.cleanup()


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "public_url": runtime.public_url(),
        "port": runtime.HTTP_PORT,
        "ai_mode": flow_conference.AI_MODE,
        "gemini": gemini_live.status(),
        "vobiz_credentials": "set" if vobiz.configured() else "MISSING",
        # Whether REST calls are intercepted. mock.py refuses to run against a
        # server where this is false, because /api/start places a REAL call.
        "vobiz_mock": vobiz.MOCK,
        "from_number": vobiz.FROM_NUMBER,
        "answer_url": runtime.url("/d1/answer/customer"),
    }


@app.get("/api/transfers")
async def transfers():
    out = []
    for transfer in sorted(store.TRANSFERS.values(), key=lambda t: t.created_at, reverse=True):
        out.append(
            {
                "tid": transfer.tid,
                "flow": transfer.flow,
                "room": transfer.room,
                "state": transfer.state,
                "customer_uuid": transfer.customer_uuid,
                "agent_uuid": transfer.agent_uuid,
                "agent_uuids": transfer.agent_uuids,
                "reason": transfer.reason,
                "summary": transfer.summary,
                "members": store.members(transfer.room),
                "turns": len(transfer.transcript),
                "timings": {
                    "ring_seconds": transfer.gap("agent_dialing", "agent_answered"),
                    "brief_seconds": transfer.gap("agent_answered", "agent_in_room"),
                    "handoff_seconds": transfer.gap("agent_dialing", "agent_in_room"),
                    # How long the human's leg sat on the briefing stream
                    # before walking into the room.
                    "brief_to_join_seconds": transfer.gap(
                        "agent_answered", "agent_in_room"
                    ),
                },
            }
        )
    return out


@app.get("/api/transfers/{tid}")
async def transfer_detail(tid: str):
    transfer = store.latest() if tid == "last" else store.get(tid)
    if not transfer:
        return JSONResponse({"error": "not found"}, status_code=404)
    from dataclasses import asdict

    detail = asdict(transfer)
    # The roster is keyed by room in `store`, not carried on the record, but a
    # caller asking about one transfer always wants it alongside.
    detail["members"] = store.members(transfer.room)
    return detail


@app.post("/api/start")
async def start(request: Request):
    """
    Begin one demo. Driven by `call.py`, which cannot do this itself: the
    transfer record and both Gemini sessions live in this process.
    """
    body = await request.json()
    to = body.get("to", "")
    targets = [t for t in body.get("agents", []) if t]
    if not to:
        return JSONResponse({"error": "no customer number"}, status_code=400)
    if not targets:
        return JSONResponse({"error": "no agent target"}, status_code=400)

    transfer = await flow_conference.start(to, body.get("room", ""))
    # Where transfer_to_human should ring, decided at demo time rather than
    # baked into the agent's prompt.
    transfer.agent_target = ",".join(targets)
    return {
        "tid": transfer.tid,
        "flow": transfer.flow,
        "room": transfer.room,
        "customer_uuid": transfer.customer_uuid,
        "agents": targets,
    }


@app.post("/api/simulate/tool")
async def simulate_tool(request: Request):
    """
    Fire one agent tool call without a model, for `mock.py`.

    Only available with VOBIZ_MOCK=1, because it drives real call control.
    """
    if not vobiz.MOCK:
        return JSONResponse({"error": "only available with VOBIZ_MOCK=1"}, status_code=403)

    import mocking

    body = await request.json()
    tid, role = body.get("tid", ""), body.get("role", "customer")
    transfer = store.get(tid)
    if not transfer:
        return JSONResponse({"error": "unknown transfer"}, status_code=404)

    session = mocking.session_for(flow_conference, tid, role)
    result = await flow_conference._on_tool(
        transfer, role, session, body.get("name", ""), body.get("args", {})
    )
    await session.flush()
    return {"result": result, "state": transfer.state, "spoken": session.spoken}


@app.post("/api/simulate/turn")
async def simulate_turn(request: Request):
    """
    Feed one transcript turn in as if the model had said it, for `mock.py`.
    Used to test the tool-call watchdog, which is driven by what is said.
    """
    if not vobiz.MOCK:
        return JSONResponse({"error": "only available with VOBIZ_MOCK=1"}, status_code=403)

    import mocking

    body = await request.json()
    tid = body.get("tid", "")
    transfer = store.get(tid)
    if not transfer:
        return JSONResponse({"error": "unknown transfer"}, status_code=404)

    mocking.session_for(flow_conference, tid, "customer")
    flow_conference._on_turn(transfer, "customer", body.get("role", "caller"), body.get("text", ""))
    return {"ok": True, "state": transfer.state}


@app.post("/api/control/{action}")
async def control(action: str, request: Request):
    """
    Member controls against a live room: mute, unmute, deaf, undeaf, play,
    speak, kick. Driven by `control.py` during a real call, and by `mock.py`.
    """
    body = await request.json()
    transfer = store.get(body.get("tid", "")) or store.latest()
    if not transfer:
        return JSONResponse({"error": "no transfer"}, status_code=404)

    target = str(body.get("member", ""))
    room = transfer.room
    calls = {
        "mute":   lambda: vobiz.member_mute(room, target),
        "unmute": lambda: vobiz.member_unmute(room, target),
        "deaf":   lambda: vobiz.member_deaf(room, target),
        "undeaf": lambda: vobiz.member_undeaf(room, target),
        "kick":   lambda: vobiz.member_kick(room, target),
        "speak":  lambda: vobiz.member_speak(room, target, body.get("text", "")),
        "play":   lambda: vobiz.member_play(room, target, body.get("url", "")),
        "stop_play": lambda: vobiz.member_stop_play(room, target),
    }
    if action not in calls:
        return JSONResponse({"error": f"unknown action {action}"}, status_code=400)

    result = await calls[action]()

    # Track what we set, so the console can show it. Nothing reads these back
    # from the platform.
    if result.ok and target:
        flags = transfer.member_flags.setdefault(target, {"muted": False, "deaf": False})
        if action in ("mute", "unmute"):
            flags["muted"] = action == "mute"
        elif action in ("deaf", "undeaf"):
            flags["deaf"] = action == "deaf"
        elif action == "kick":
            transfer.member_flags.pop(target, None)

    store.record(
        "member_control",
        {"tid": transfer.tid, "action": action, "member": target,
         "status": result["status"], "body": result.get("body")},
    )
    return {"action": action, "member": target, "status": result["status"],
            "ok": result.ok, "body": result.get("body")}


@app.post("/api/dial-human")
async def dial_human(request: Request):
    """
    Start the handover from the console, without waiting for the AI to decide.

    Useful for testing: it exercises the same `dial_agents` path the
    `transfer_to_human` tool uses, so the briefing and accept flow are identical.
    """
    body = await request.json()
    transfer = store.get(body.get("tid", "")) or store.latest()
    if not transfer:
        return JSONResponse({"error": "no transfer"}, status_code=404)

    targets = [t for t in (transfer.agent_target or "").split(",") if t]
    if not targets:
        return JSONResponse({"error": "no agent target configured"}, status_code=400)

    transfer.reason = transfer.reason or "operator started the handover"
    transfer.summary = transfer.summary or (
        "Handover started from the console. "
        + " ".join(t["text"] for t in transfer.transcript
                   if t.get("leg") == "customer" and t.get("role") == "caller")[:400]
    )
    transfer.mark("tool_fired")               # stand the watchdog down
    transfer.marks.pop("dialing_started", None)   # an operator press always dials
    await flow_conference.dial_agents(transfer, targets)
    return {"ok": True, "state": transfer.state, "targets": targets}


@app.get("/api/panel")
async def panel_state():
    """Everything the live console needs, in one poll."""
    transfer = None
    for t in sorted(store.TRANSFERS.values(), key=lambda t: t.created_at, reverse=True):
        if store.members(t.room):
            transfer = t
            break
    if transfer is None:
        transfer = store.latest()
    if transfer is None:
        return {"tid": None}

    members = []
    for mid in store.members(transfer.room):
        role = ("customer" if mid == transfer.customer_member
                else "human agent" if mid == transfer.agent_member else "unknown")
        flags = transfer.member_flags.get(mid, {})
        members.append({"id": mid, "role": role,
                        "muted": bool(flags.get("muted")),
                        "deaf": bool(flags.get("deaf"))})

    in_room = {m["id"] for m in members}
    pending = []
    if transfer.agent_uuid and transfer.agent_member not in in_room:
        # answered, being briefed, not yet in the room — no member id exists yet,
        # so no per-member control is possible on them.
        pending.append({"role": "human agent", "state": "being briefed"})
    elif transfer.agent_uuids and not transfer.agent_uuid:
        pending.append({"role": "human agent", "state": "ringing"})

    return {
        "tid": transfer.tid,
        "room": transfer.room,
        "state": transfer.state,
        "members": members,
        "pending": pending,
        # Enabled whenever a target is configured and no human has answered yet.
        # Deliberately permissive: this is an explicit operator action, and the
        # old condition (state == bot_active) went dead as soon as the AI fired
        # its own tool or an attempt failed.
        "can_dial": bool(transfer.agent_target) and not transfer.agent_uuid
                    and transfer.state not in (store.BRIDGED, store.ENDED),
        "transcript": transfer.transcript[-40:],
    }


@app.get("/panel")
async def panel_page():
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).parent / "static" / "panel.html")


@app.get("/api/events")
async def events(limit: int = 500):
    return store.EVENTS[-limit:]


@app.post("/api/reset")
async def reset():
    store.reset()
    return {"ok": True}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def catch_all(path: str, request: Request):
    """
    Answer 200 to anything unrouted. A callback we did not anticipate is far more
    useful captured than 404'd, and Vobiz retries non-200s.
    """
    return Response(content="<Response></Response>", media_type="application/xml")


app.add_middleware(WebhookCapture)


def _banner():
    base = runtime.public_url()
    print()
    print("  Vobiz warm call transfer — conference")
    print(f"  public url        {base}")
    print(f"  AI_MODE           {flow_conference.AI_MODE}")
    print(f"  gemini            {gemini_live.GEMINI_MODEL}  voice={gemini_live.GEMINI_VOICE}"
          f"/{gemini_live.GEMINI_BRIEF_VOICE}  lang={gemini_live.GEMINI_LANGUAGE or 'auto'}")
    print(f"  vobiz             {'credentials set' if vobiz.configured() else 'CREDENTIALS MISSING'}"
          f"  from={vobiz.FROM_NUMBER}")
    print()
    print("  customer answer   " + runtime.url("/d1/answer/customer"))
    print("  agent answer      " + runtime.url("/d1/answer/agent"))
    print("  conference events " + runtime.url("/d1/conf-events"))
    print("  media socket      " + runtime.ws_url("/ws"))
    print()
    print("  next:  python selftest.py     then     python call.py")
    print()


if __name__ == "__main__":
    public = runtime.public_url()
    if not public:
        print("PUBLIC_URL is empty — opening an ngrok tunnel")
        public = runtime.start_ngrok()
    runtime.set_public_url(public)
    _banner()
    uvicorn.run(app, host="0.0.0.0", port=runtime.HTTP_PORT, log_level="warning")
