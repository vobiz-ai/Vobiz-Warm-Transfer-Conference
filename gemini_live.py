"""
gemini_live.py — Vobiz bidirectional media <-> Gemini Live, one session per leg
===============================================================================

Adapted from ../vobiz-gemini-live/gemini_live.py, which is verified against a
real PSTN call. Two things changed for warm transfer:

  1. A **Persona**. The original had one system prompt and one tool baked in as
     module constants. A warm transfer runs two agents at once — one talking to
     the customer, one briefing the human agent — with different prompts,
     different greetings and different tools, in the same process.

  2. **Per-session audio format.** The customer leg in the <Stream> XML flow is
     L16/16k, but a leg inside a <Conference> gets its media bug attached over
     REST at mulaw/8k. Output format is therefore chosen per session instead of
     read from one env var.

Everything hard-won in the original is kept verbatim, and each of these cost a
debugging pass:

  * `session.receive()` yields ONE turn and stops. A bare `async for` exits the
    `async with`, closing the Gemini socket the instant the greeting ends. The
    outer `while` is what keeps the call alive.
  * Barge-in must forward Gemini's `interrupted` to Vobiz as `clearAudio`, or
    the caller keeps hearing a reply the model already abandoned.
  * A `checkpoint` after each turn is what lets a tool say its line *and then*
    act, instead of cutting the caller off mid-word.
  * Read `start.mediaFormat` on every connection — it is the authority on what
    Vobiz is actually sending, and it changes whenever the XML is edited.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load before the module-level reads below. This module is imported by
# `flow_conference` ahead of `runtime` and `vobiz`, so it cannot rely on either
# of them having populated the environment yet — without this the server starts
# with an empty API key and every call gets a silent agent.
load_dotenv(Path(__file__).parent / ".env")

import audio

logger = logging.getLogger("gemini")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Aoede")
GEMINI_BRIEF_VOICE = os.getenv("GEMINI_BRIEF_VOICE", "Charon")
# Pin this on a real Indian line. With detection on auto, background noise and
# accented speech get transcribed as whatever language fits best and the model
# answers in kind.
GEMINI_LANGUAGE = os.getenv("GEMINI_LANGUAGE", "en-IN").strip()

PLAY_CHUNK_MS = int(os.getenv("PLAY_CHUNK_MS", "20"))
VAD_SILENCE_MS = os.getenv("VAD_SILENCE_MS", "").strip()


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------

def _fn(name: str, description: str, props: dict, required: list[str] | None = None):
    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                k: types.Schema(type=types.Type.STRING, description=v)
                for k, v in props.items()
            },
            required=required or [],
        ),
    )


TRANSFER_TO_HUMAN = _fn(
    "transfer_to_human",
    "Dial a human colleague's phone and bring them onto this call. This function "
    "is the ONLY thing that reaches a human — describing a transfer out loud does "
    "nothing whatsoever. Call it the moment the caller asks for a person, or the "
    "request is beyond what you can do.",
    {
        "reason": "One short phrase for why a human is needed.",
        "summary": "A spoken briefing for the colleague: who is calling, what "
                   "they want, what has already been established, and what the "
                   "colleague needs to do. Three or four sentences.",
    },
    required=["reason", "summary"],
)

END_CALL = _fn(
    "end_call",
    "Hang up. Only after saying goodbye.",
    {"reason": "Short reason the call is ending."},
)

ACCEPT_TRANSFER = _fn(
    "accept_transfer",
    "Confirm you are taking this customer, and join the call with them. Call "
    "this once the colleague says yes, they will take it.",
    {},
)

REJECT_TRANSFER = _fn(
    "reject_transfer",
    "The colleague cannot take this customer. Call this if they decline or say "
    "it is not their area.",
    {"reason": "Short reason they cannot take it."},
)


@dataclass
class Persona:
    """One agent role: prompt, opening line, tools, and playback format."""

    name: str
    system_prompt: str
    greeting: str = ""
    tools: list = field(default_factory=list)
    voice: str = GEMINI_VOICE
    out_content_type: str = "audio/x-l16"
    out_rate: int = 24000


def customer_persona(out_content_type: str, out_rate: int) -> Persona:
    return Persona(
        name="customer",
        system_prompt=os.getenv(
            "CUSTOMER_PROMPT",
            "You are the first-line voice agent for a company, on a live phone "
            "call with a customer. Keep every reply to one or two short "
            "sentences. Speak plainly, never use markdown or emoji, and read "
            "numbers digit by digit. Answer what you can yourself.\n\n"
            "HANDING OVER TO A HUMAN — read this carefully.\n"
            "The moment the caller asks for a person, or wants something you "
            "cannot do, you MUST call the transfer_to_human function. Calling "
            "that function is what actually rings a colleague's phone. Saying "
            "the words is not the action and achieves nothing at all.\n"
            "Never say a colleague is joining, connecting, being brought in, or "
            "on their way unless you have called the function. If you announce a "
            "transfer without calling it, nobody is dialled, and the caller waits "
            "forever for someone who was never contacted. That is the worst "
            "outcome of this call.\n"
            "So: call transfer_to_human on the very same turn you first mention "
            "a colleague. Give it a summary a colleague could actually act on — "
            "who is calling, what they want, and what has already been "
            "established.\n\n"
            "While the colleague is being brought in, keep the caller company. "
            "Once they join, introduce them in one sentence and then stay quiet "
            "unless spoken to directly.",
        ),
        greeting=os.getenv(
            "CUSTOMER_GREETING",
            "Hi, thanks for calling. How can I help you today?",
        ),
        tools=[TRANSFER_TO_HUMAN, END_CALL],
        voice=GEMINI_VOICE,
        out_content_type=out_content_type,
        out_rate=out_rate,
    )


def brief_persona(summary: str, reason: str, out_content_type: str, out_rate: int) -> Persona:
    return Persona(
        name="brief",
        system_prompt=(
            "You are an AI assistant briefing a human colleague who has just "
            "picked up the phone. A customer is holding on another line. Your "
            "only job is to hand this over well.\n\n"
            f"Reason for the handover: {reason}\n"
            f"Briefing: {summary}\n\n"
            "Open by delivering that briefing in your own words, in two or "
            "three short sentences. Then ask whether they will take the "
            "customer. Answer any question they have about the case using only "
            "what is in the briefing; if you do not know, say so plainly. "
            "As soon as they agree, call accept_transfer. If they decline or "
            "say it is not their area, call reject_transfer. Be quick — the "
            "customer is waiting. Never use markdown or emoji."
        ),
        greeting=(
            "Greet the colleague briefly, then deliver the briefing and ask if "
            "they can take the customer."
        ),
        tools=[ACCEPT_TRANSFER, REJECT_TRANSFER],
        voice=GEMINI_BRIEF_VOICE,
        out_content_type=out_content_type,
        out_rate=out_rate,
    )


def status() -> dict:
    return {
        "model": GEMINI_MODEL,
        "voice": GEMINI_VOICE,
        "brief_voice": GEMINI_BRIEF_VOICE,
        "language": GEMINI_LANGUAGE or "auto",
        "api_key": "set" if GEMINI_API_KEY else "MISSING",
    }


def _live_config(persona: Persona) -> types.LiveConnectConfig:
    speech = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=persona.voice)
        )
    )
    if GEMINI_LANGUAGE:
        speech.language_code = GEMINI_LANGUAGE

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=speech,
        system_instruction=types.Content(
            role="user", parts=[types.Part(text=persona.system_prompt)]
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    if VAD_SILENCE_MS:
        config.realtime_input_config = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                silence_duration_ms=int(VAD_SILENCE_MS)
            )
        )
    if persona.tools:
        config.tools = [types.Tool(function_declarations=persona.tools)]
    return config


# ===========================================================================
#  The Vobiz half
# ===========================================================================

class VobizStream:
    """`playAudio` / `checkpoint` / `clearAudio` out, media frames in."""

    def __init__(self, ws, out_content_type: str, out_rate: int):
        self.ws = ws
        self.stream_id: str | None = None
        self.call_id: str | None = None
        self.in_encoding = "l16"
        self.in_rate = 16000
        self.out_content_type = out_content_type
        self.out_encoding, _ = audio.parse_content_type(out_content_type)
        self.out_rate = out_rate
        self.chunk = audio.frame_bytes(self.out_encoding, self.out_rate, PLAY_CHUNK_MS)
        self.checkpoints = 0
        self.played_bytes = 0

    async def send(self, payload: dict):
        data = json.dumps(payload)
        if hasattr(self.ws, "send_text"):
            await self.ws.send_text(data)     # Starlette / FastAPI
        else:
            await self.ws.send(data)          # websockets library

    def read_start(self, data: dict) -> tuple[str | None, str | None]:
        start = data.get("start", {}) or {}
        self.stream_id = data.get("streamId") or start.get("streamId")
        self.call_id = (
            data.get("callId")
            or start.get("callId")
            or start.get("callUUID")
            or data.get("CallUUID")
        )
        fmt = start.get("mediaFormat") or {}
        if fmt.get("encoding"):
            self.in_encoding, _ = audio.parse_content_type(fmt["encoding"])
        if fmt.get("sampleRate"):
            self.in_rate = int(fmt["sampleRate"])
        return self.stream_id, self.call_id

    async def play(self, pcm24: bytes):
        if not pcm24:
            return
        data = audio.from_gemini(pcm24, self.out_encoding, self.out_rate)
        self.played_bytes += len(data)
        for i in range(0, len(data), self.chunk):
            frame = {
                "event": "playAudio",
                "media": {
                    "contentType": self.out_content_type,
                    "sampleRate": self.out_rate,
                    "payload": base64.b64encode(data[i:i + self.chunk]).decode(),
                },
            }
            if self.stream_id:
                frame["streamId"] = self.stream_id
            await self.send(frame)

    async def checkpoint(self, name: str = "") -> str:
        self.checkpoints += 1
        label = name or f"turn-{self.checkpoints}"
        if self.stream_id:
            await self.send(
                {"event": "checkpoint", "streamId": self.stream_id, "name": label}
            )
        return label

    async def clear(self):
        if self.stream_id:
            await self.send({"event": "clearAudio", "streamId": self.stream_id})

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


# ===========================================================================
#  The bridge
# ===========================================================================

class GeminiLiveSession:
    """One Gemini Live session for the lifetime of one Vobiz WebSocket."""

    def __init__(self, ws, persona: Persona, on_turn=None, on_event=None, on_tool=None):
        self.persona = persona
        self.vobiz = VobizStream(ws, persona.out_content_type, persona.out_rate)
        self.on_turn = on_turn or (lambda role, text: None)
        self.on_event = on_event or (lambda name, data: None)
        self.on_tool = on_tool                       # async (name, args) -> dict | None

        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.session = None
        self.ready = asyncio.Event()
        self.runner: asyncio.Task | None = None
        self.pump: asyncio.Task | None = None
        self.closed = False

        # Audio arriving before Gemini finishes its handshake would otherwise be
        # dropped, and the caller's first words are usually the important ones.
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)

        self.said: list[str] = []
        self.heard: list[str] = []
        self.transcript: list[dict] = []
        self.frames_in = 0
        # Set by a tool handler that must speak before it acts; the action runs
        # when Vobiz confirms the audio actually played.
        self.after_playback = None
        # After a handover completes the agent must stop talking. It keeps
        # receiving room audio and transcribing — which is what "the AI stays on
        # the call" means — but nothing it generates is played any more. On a
        # live call it otherwise announced the colleague three times and kept
        # answering the customer over the top of the handoff.
        self.muted = False

    # -- Vobiz -> here ------------------------------------------------------

    async def handle(self, message: str):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Non-JSON frame from Vobiz")
            return

        event = data.get("event")

        if event == "start":
            stream_id, call_id = self.vobiz.read_start(data)
            logger.info(
                f"[{self.persona.name}] start — call {call_id} stream {stream_id} "
                f"in {self.vobiz.in_encoding}/{self.vobiz.in_rate} "
                f"out {self.vobiz.out_encoding}/{self.vobiz.out_rate}"
            )
            self.on_event("stream:start", data)
            await self.connect()

        elif event == "media":
            payload = (data.get("media") or {}).get("payload")
            if not payload:
                return
            self.frames_in += 1
            pcm = audio.to_gemini(
                base64.b64decode(payload), self.vobiz.in_encoding, self.vobiz.in_rate
            )
            try:
                self.inbound.put_nowait(pcm)
            except asyncio.QueueFull:
                # Drop the oldest rather than let the queue become a permanent
                # delay behind the live conversation.
                try:
                    self.inbound.get_nowait()
                    self.inbound.put_nowait(pcm)
                except asyncio.QueueEmpty:
                    pass

        elif event == "dtmf":
            digit = (data.get("dtmf") or {}).get("digit", "")
            self.on_event("stream:dtmf", data)
            if digit and self.session:
                await self.session.send_realtime_input(
                    text=f"The caller pressed the keypad digit {digit}."
                )

        elif event == "playedStream":
            self.on_event("stream:playedStream", data)
            action, self.after_playback = self.after_playback, None
            if action:
                await action()

        elif event == "clearedAudio":
            self.on_event("stream:clearedAudio", data)

        elif event == "stop":
            logger.info(f"[{self.persona.name}] stop — {self.frames_in} frames in")
            self.on_event("stream:stop", data)
            await self.cleanup()

        else:
            self.on_event(f"stream:{event or 'unknown'}", data)

    async def say(self, text: str):
        """Make the agent speak a specific line, mid-conversation."""
        if not self.session:
            return
        await self.session.send_client_content(
            turns=types.Content(
                role="user", parts=[types.Part(text=f"Say exactly this: {text}")]
            ),
            turn_complete=True,
        )

    async def inform(self, text: str):
        """Give the agent a fact without making it speak."""
        if self.session:
            await self.session.send_realtime_input(text=text)

    # -- Gemini ------------------------------------------------------------

    async def connect(self):
        if self.runner:
            return
        if not GEMINI_API_KEY:
            logger.error("GEMINI_API_KEY is not set — no agent on this leg")
            return
        self.runner = asyncio.create_task(self._run())
        self.pump = asyncio.create_task(self._pump_audio())

    async def _run(self):
        try:
            async with self.client.aio.live.connect(
                model=GEMINI_MODEL, config=_live_config(self.persona)
            ) as session:
                self.session = session
                self.ready.set()
                logger.info(f"[{self.persona.name}] Gemini connected — {GEMINI_MODEL}")
                self.on_event("gemini:connected", {"model": GEMINI_MODEL})

                if self.persona.greeting:
                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[types.Part(text=self.persona.greeting)],
                        ),
                        turn_complete=True,
                    )

                # See the module docstring: the outer loop is load-bearing.
                while not self.closed:
                    async for message in session.receive():
                        await self._on_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[{self.persona.name}] Gemini ended: {type(exc).__name__}: {exc}")
            self.on_event("gemini:error", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            self.ready.set()
            self.session = None

    async def _pump_audio(self):
        await self.ready.wait()
        while not self.closed:
            pcm = await self.inbound.get()
            if not self.session:
                continue
            try:
                await self.session.send_realtime_input(
                    audio=types.Blob(
                        data=pcm, mime_type=f"audio/pcm;rate={audio.GEMINI_INPUT_RATE}"
                    )
                )
            except Exception as exc:
                logger.error(f"[{self.persona.name}] send_realtime_input failed: {exc}")
                return

    def go_silent(self):
        """Stop speaking. Keep listening, keep transcribing."""
        self.muted = True

    async def _on_message(self, message):
        content = getattr(message, "server_content", None)

        if getattr(message, "data", None) and not self.muted:
            await self.vobiz.play(message.data)

        if content:
            if getattr(content, "interrupted", None):
                await self.vobiz.clear()
                self.on_event("gemini:interrupted", {})

            transcript = getattr(content, "input_transcription", None)
            if transcript and transcript.text:
                self.heard.append(transcript.text)

            transcript = getattr(content, "output_transcription", None)
            if transcript and transcript.text:
                self.said.append(transcript.text)

            if getattr(content, "turn_complete", None):
                self._flush_turns()
                await self.vobiz.checkpoint()

        tool_call = getattr(message, "tool_call", None)
        if tool_call and tool_call.function_calls:
            await self._on_tool_call(tool_call.function_calls)

        go_away = getattr(message, "go_away", None)
        if go_away:
            logger.warning(f"[{self.persona.name}] go_away — {go_away.time_left} left")
            self.on_event("gemini:go_away", {"time_left": str(go_away.time_left)})

    def _flush_turns(self):
        for role, buffer in (("caller", self.heard), ("agent", self.said)):
            if buffer:
                text = "".join(buffer).strip()
                buffer.clear()
                if text:
                    self.transcript.append({"role": role, "text": text})
                    self.on_turn(role, text)

    async def _on_tool_call(self, calls):
        responses = []
        for call in calls:
            args = dict(call.args or {})
            logger.info(f"[{self.persona.name}] tool {call.name}({args})")
            self.on_event("gemini:tool_call", {"name": call.name, "args": args})
            result = {"result": "ok"}
            if self.on_tool:
                try:
                    returned = await self.on_tool(call.name, args)
                    if isinstance(returned, dict):
                        result = returned
                except Exception as exc:
                    logger.exception(f"tool {call.name} failed")
                    result = {"result": "error", "error": str(exc)}
            responses.append(
                types.FunctionResponse(id=call.id, name=call.name, response=result)
            )
        if self.session and responses:
            await self.session.send_tool_response(function_responses=responses)

    def run_after_playback(self, coro_factory, timeout: float = 0.0):
        """
        Defer an action until Vobiz confirms the queued audio has been heard.

        Everything the agent has said is still sitting in the call leg's
        playback buffer when a tool fires, so acting immediately cuts the line
        off mid-word. The next `playedStream` is the signal that it landed.

        `timeout` bounds that wait. Without one the action waits on an agent
        that may simply keep talking: measured live, a human who accepted a
        transfer took **10.1 s** to reach the room because the AI carried on
        conversing past the accept, and the customer heard dead air for all of
        it. Past the timeout the action runs regardless.
        """
        self.after_playback = coro_factory
        if timeout > 0:
            asyncio.create_task(self._playback_deadline(coro_factory, timeout))

    async def _playback_deadline(self, coro_factory, timeout: float):
        await asyncio.sleep(timeout)
        if self.after_playback is coro_factory:
            self.after_playback = None
            logger.info(
                f"[{self.persona.name}] playback confirmation did not arrive in "
                f"{timeout}s — running the deferred action anyway"
            )
            await coro_factory()

    # -- Teardown ----------------------------------------------------------

    async def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self._flush_turns()
        for task in (self.pump, self.runner):
            if task:
                task.cancel()
        for task in (self.pump, self.runner):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info(
            f"[{self.persona.name}] closed — {self.frames_in} frames in, "
            f"{self.vobiz.played_bytes} bytes played, "
            f"{self.vobiz.checkpoints} checkpoints"
        )
