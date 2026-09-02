# Vobiz warm call transfer — conference

[![ci](https://github.com/vobiz-ai/Vobiz-Warm-Transfer-Conference/actions/workflows/ci.yml/badge.svg)](https://github.com/vobiz-ai/Vobiz-Warm-Transfer-Conference/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An AI agent takes a call, and when it needs a human it **briefs that human
privately first** — then merges everyone. The customer never hears the briefing,
and the AI stays on the line afterwards, listening.

```
        ┌── customer is in a conference room from the moment they answer
        │
        │   AI attached to their leg as a media bug ── talks, listens
        │
        │   "can I speak to someone?"
        │        │
        │        └── a colleague is dialled on a SEPARATE call
        │                 │
        │                 ├── AI briefs them, two-way, in private
        │                 │   (they are not in the room yet — nothing can leak)
        │                 │
        │                 └── "yes, I'll take it"
        │                          │
        └──────────────────────────┴── their leg walks into the room
                                       all three connected, AI goes quiet
```

---

## How it works

**The customer never moves.** Their answer XML *is* `<Conference>`, so they are
in a room, alone, from the first second. The "transfer" is the human **joining
the room the customer is already in** — no redirect, no Transfer API, and no gap
in the customer's audio.

The sequence:

```
 StartApp           customer answers
 ConferenceEnter    they are in the room, alone
 ─ POST /Call/{uuid}/Stream/ ─── the AI attaches to their leg
 StartApp           colleague answers — a separate call
 StartStream        their private briefing begins
 DroppedStream      briefing socket closes
 ConferenceEnter    colleague is in the room, ~0.1s later
```

### The customer's XML

```xml
<Response>
  <Record fileFormat="mp3" recordSession="true" redirect="false"
          playBeep="false" maxLength="3600"
          action="https://…/d1/record-action" callbackUrl="https://…/d1/record-callback"/>
  <Conference stayAlone="true" startConferenceOnEnter="true"
              endConferenceOnExit="true" relayDTMF="false" timeLimit="1800"
              waitSound="https://…/d1/wait" waitMethod="POST"
              callbackUrl="https://…/d1/conf-events" callbackMethod="POST">ROOM</Conference>
  <Hangup/>
</Response>
```

Set `stayAlone="true"`. It initialises `false`, and a member alone in a room is
kicked straight back out — which is exactly what the customer is for the first
several seconds.

### The colleague's XML — where the briefing lives

```xml
<Response>
  <Record … />
  <Stream bidirectional="true" audioTrack="inbound" keepCallAlive="true"
          contentType="audio/x-l16;rate=16000"
          extraHeaders="tidX-VH=…"
          statusCallbackUrl="https://…/d1/stream-status">wss://…/ws?role=agent&amp;tid=…</Stream>
  <Conference stayAlone="true" startConferenceOnEnter="true"
              endConferenceOnExit="false" relayDTMF="false" timeLimit="1800"
              callbackUrl="https://…/d1/conf-events">ROOM</Conference>
  <Hangup/>
</Response>
```

The two elements are sequential, and that is the whole design.
`keepCallAlive="true"` holds the leg on the stream until the socket closes, then
the document resumes at the next element. So the AI briefs the colleague while
they are **outside** the room, and closing the socket releases them into it.

The privacy is structural, not configured — there is no mute or deaf to get
wrong, because the colleague simply is not in the room while the briefing runs.

### Putting the AI in the room

`<Stream>` and `<Conference>` cannot run at the same time. XML verbs execute in
order on a leg, and a bidirectional stream blocks it, so a `<Conference>` below
one is never reached. Use the **Stream REST API** instead, which attaches the
stream as a media bug on the channel, outside XML sequencing:

```http
POST /api/v1/Account/{auth_id}/Call/{call_uuid}/Stream/
{
  "service_url": "wss://…/ws?role=customer&tid=…",
  "bidirectional": "true",
  "audio_track": "inbound",
  "content_type": "audio/x-mulaw;rate=8000",
  "stream_timeout": 3600
}
→ 202 {"message": "audio streaming started", "stream_id": "…"}
```

`DELETE /Call/{uuid}/Stream/{stream_id}/` detaches it — that is how the AI drops
off without ending the call.

Send `bidirectional` as the **string** `"true"`. A JSON boolean is accepted and
silently gives you a one-way stream. And wait about two seconds after
`ConferenceEnter` before attaching, or the stream receives no audio.

### If the model will not act

The AI is told to call `transfer_to_human`. Models sometimes *announce* a
colleague without calling the function, which means nobody is dialled and the
caller waits for someone who was never contacted — a failure that is invisible
from the caller's side.

So the server checks rather than trusts. When either the caller asks for a human
or the AI claims a transfer, a watchdog arms: **+6 s** nudge the model, **+8 s
more** dial anyway with a summary salvaged from the transcript.

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env       # then fill it in
```

You need Vobiz credentials, a Vobiz DID to call from, and a
[Gemini API key](https://aistudio.google.com/apikey). Live model names change
often, so check which your key can use before debugging an opaque close.

## Run it

Always in this order. The first two steps cost nothing, and a malformed answer
document costs a live call to diagnose.

```bash
.venv/bin/python selftest.py               # parse every XML document offline
VOBIZ_MOCK=1 .venv/bin/python app.py       # no real calls can be placed
.venv/bin/python mock.py --all             # nine scenarios, no phone calls
```

`mock.py` impersonates Vobiz: it posts the real webhooks to the real endpoints
in the order the platform sends them, and asserts the handover reaches the
bridge. It covers the happy path, a declined transfer, no-answer, two colleagues
racing, the customer hanging up mid-ring, a model that will not call the tool,
and the member controls. It refuses to run against a server that is not in mock
mode, because `/api/start` places a real call.

Then, for real:

```bash
.venv/bin/python app.py                    # ngrok starts if PUBLIC_URL is empty
.venv/bin/python call.py                   # rings the numbers in .env
.venv/bin/python call.py --agent sip       # reach the colleague on a SIP/WebRTC endpoint
.venv/bin/python call.py --agent both      # ring both, first to accept wins
```

While a call is up, open **`http://localhost:8100/panel`** — a live console with
the roster, the member controls, and the AI transcript as it happens.

Afterwards:

```bash
.venv/bin/python report.py                 # webhooks, per-leg cost, recordings
.venv/bin/python report.py --download      # fetch the audio and split the channels
```

## Files

| File | Role |
|---|---|
| `app.py` | the server: routes, capture middleware, `/ws`, `/panel`, read API |
| `flow_conference.py` | the call flow — orchestration, watchdog, cleanup |
| `xml_docs.py` | every answer document, importable so `selftest.py` can parse them |
| `gemini_live.py` | Vobiz ⇄ Gemini Live bridge, one persona per leg |
| `audio.py` | μ-law ⇄ PCM16 and resampling, pure Python |
| `vobiz.py` | REST client — calls, streams, conference members, CDR, recordings |
| `store.py` | roster, handoff state machine, capture |
| `runtime.py` | public-URL resolution, URL builders, request helpers |
| `selftest.py` | parse every document offline |
| `mock.py` | nine scenarios end to end, no phone calls |
| `call.py` | place one call |
| `report.py` | webhooks, per-leg pricing, recordings — `--download` fetches and splits the audio |
| `static/panel.html` | the live console |

## Recording

Conference recording does not produce a file — neither
`<Conference record="true">` nor `POST /Conference/{room}/Record/`, which
returns HTTP 200 and writes nothing.

Use per-leg session recording instead, as a self-closing sibling placed **before**
the element it should record alongside:

```xml
<Record fileFormat="mp3" recordSession="true" redirect="false" … />
```

`recordSession="true"` captures the whole leg including anything bridged into it,
and survives a transfer. Both legs are recorded here, so the colleague's briefing
is kept as well as the conversation. Output is stereo with one party per channel;
`report.py --download` splits them.

With `redirect="false"` the action URL must return an empty `<Response>`, or it
interrupts a flow that has already moved on.

## Cost

```
total_cost = (0.45 x pulses) + (0.20 x pulses, if the leg streamed)
```

Pulses are 60 seconds, rounded up. A warm transfer is two legs, so a 90-second
handover runs about **1.95 INR**.

Cost never appears in a webhook — `TotalCost` is `0.00000` on every event. The
CDR has it, settled within about 120 ms of hangup, and `total_cost` **already
includes** `streaming_cost`, so adding the two double-counts. Fetch a CDR per
leg: the customer leg alone reports roughly half the real figure.

## Gotchas

- **`stayAlone="true"` is mandatory** wherever a member can briefly be alone.
- **`bidirectional` must be the string `"true"`** on the Stream REST API.
- **Wait ~2 s after `ConferenceEnter`** before attaching the media bug.
- **The AI's voice never enters the conference mix.** It is injected into its
  host leg by `playAudio`, so `deaf` cannot silence it and the AI cannot address
  the room — only the leg it rides.
- **`mute` on the AI's host leg blinds the AI**, because the media bug taps that
  leg's inbound audio.
- **Member controls return 202 and 204, not 200**, and `member_id` comes back as
  an **array** even for one member.
- **Member IDs come only from callbacks.** `GET /Conference/{name}/` answers
  HTTP 200 with `{"error":"failed"}` on a live room.
- **There is no room-wide Play or Speak** — only `Member/{id}/…` routes exist.
- **`endConferenceOnExit` kicks everyone**, ignoring `stayAlone` on the
  remaining members. The kicked legs report `HangupSource=Callee`, so a platform
  teardown looks like the participant hanging up.
- **`ConferenceLastMember` does not mean "last to leave"** — it is
  `Conference-Size == 0` at that event. Callback delivery order is not event
  order either, so two exits can arrive reversed.
- **Malformed answer XML is not an HTTP error.** The call answers, then dies a
  second later, and only the CDR says `Invalid Answer XML` / code 8011. A URL
  with two query parameters contains a bare `&`. Run `selftest.py`.

## Configuration

Everything is env-driven — see [`.env.example`](.env.example). The ones that
change behaviour rather than credentials:

| Key | Default | What it does |
|---|---|---|
| `AI_MODE` | `duplex` | `duplex` = Stream REST media bug. `tap` = non-blocking `<Stream audioTrack="both">` plus per-member Speak — half-duplex, and in Vobiz's TTS voice rather than the model's |
| `RECORD_ROOM` | `true` | per-leg session recording on both legs |
| `RING_TIMEOUT` | `25` | how long the colleague rings before the customer is told |
| `SETTLE_DELAY` | `2.0` | pause between `ConferenceEnter` and attaching the media bug |
| `ACCEPT_JOIN_TIMEOUT` | `3.0` | cap on the wait between accepting and joining the room |
| `AI_DROP_AFTER_BRIDGE` | `false` | detach the AI once the colleague is in, instead of leaving it listening |
| `VOBIZ_MOCK` | unset | intercept every REST call; nothing leaves the process |

## What never belongs in the repo

`.gitignore` covers all of it, but know what you are keeping locally:

| Path | Contents |
|---|---|
| `.env` | your Gemini key and Vobiz auth token |
| `data/`, `events/` | captured webhooks — real phone numbers and call UUIDs |
| `recordings/` | recorded audio of real conversations |
| `*.log` | answer XML, callback payloads, phone numbers |

Transcripts and recordings are personal data in most jurisdictions. If you add
storage, decide on retention before you add the feature, and tell callers.

## Deploying

Any host with a public HTTPS URL and WebSocket support. Set `PUBLIC_URL` and
ngrok is skipped:

```bash
PUBLIC_URL=https://voice.example.com .venv/bin/python app.py
```

Behind a reverse proxy, WebSocket upgrades on `/ws` must be forwarded, and the
idle timeout has to exceed your longest call.

## Docs

- [Conference member controls](docs/member-controls.md) — every control, the
  status codes they really return, and why `deaf` needs two humans to test
- [Events and webhooks](docs/events-and-webhooks.md) — every event, the fields
  that actually arrive, and the ones that do not
- [Billing](docs/billing.md) — the rate card, per-leg reconciliation, and why
  cost is CDR-only
- [Vobiz `<Conference>`](https://vobiz.ai/docs/xml/conference) ·
  [`<Stream>`](https://vobiz.ai/docs/xml/stream) ·
  [Gemini Live API](https://ai.google.dev/gemini-api/docs/live)

## Contributing

Contributions are genuinely welcome. Bug reports, a format combination you got
working, a provider you bridged to instead of Gemini, a deployment recipe: all
useful.

Two asks before a pull request:

- run `python selftest.py`, and if the change touches the flow run
  `python mock.py --all` too;
- keep real call data out of the diff — no transcripts, recordings, call UUIDs,
  account IDs or phone numbers.

## Maintainer

Managed and maintained by
**[Piyush Sahoo](https://www.linkedin.com/in/piyush-s713/)** at
[Vobiz](https://vobiz.ai).

## License

[MIT](LICENSE) — use it commercially, fork it, ship it. No warranty.
