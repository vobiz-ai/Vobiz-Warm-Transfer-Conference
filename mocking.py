"""
mocking.py — a stand-in for a live Gemini session
==================================================

`mock.py` needs to exercise the orchestration: the webhook sequence, the state
machine, the race between two agent legs, the no-answer fallback and the
cleanup. None of that involves the model, and re-testing Gemini here would only
duplicate what ../vobiz-gemini-live already proves on a real call.

So the media socket is replaced by this stub. It records what the agent would
have said and runs deferred actions immediately, since there is no real
playback to wait for.
"""

from __future__ import annotations

import logging

import store

logger = logging.getLogger("mock")


class StubStream:
    def __init__(self, session):
        self.session = session
        self.stream_id = "stub"

    async def close(self):
        self.session.closed = True
        store.record("mock_socket_closed", {"tid": self.session.tid,
                                            "role": self.session.role})

    async def clear(self):
        return

    async def play(self, _pcm):
        return

    async def checkpoint(self, name: str = ""):
        return name


class StubSession:
    """Enough of GeminiLiveSession for the flow modules to drive."""

    def __init__(self, tid: str, role: str):
        self.tid = tid
        self.role = role
        self.vobiz = StubStream(self)
        self.closed = False
        self.spoken: list[str] = []
        self.on_tool = None

    async def say(self, text: str):
        self.spoken.append(text)
        store.record("mock_said", {"tid": self.tid, "role": self.role, "text": text})
        logger.info(f"[{self.tid}:{self.role}] would say: {text}")

    async def inform(self, text: str):
        store.record("mock_informed", {"tid": self.tid, "role": self.role, "text": text})

    def run_after_playback(self, coro_factory, timeout: float = 0.0):
        """
        Real sessions wait for Vobiz to confirm the audio was heard, bounded by
        `timeout`. There is no playback here, so the action runs straight away —
        the ordering the deferral protects is a human-audio concern, not a
        state-machine one. `timeout` is accepted and ignored, but it must be
        accepted: the real signature grew it and this stub silently broke the
        flow-2 bridge until it matched.
        """
        self._pending = coro_factory

    async def flush(self):
        pending = getattr(self, "_pending", None)
        self._pending = None
        if pending:
            await pending()


def session_for(flow_module, tid: str, role: str) -> StubSession:
    """Fetch or create the stub the flow module will look up by itself."""
    key = f"{tid}:{role}"
    existing = flow_module.SESSIONS.get(key)
    if isinstance(existing, StubSession):
        return existing
    session = StubSession(tid, role)
    flow_module.SESSIONS[key] = session
    return session
