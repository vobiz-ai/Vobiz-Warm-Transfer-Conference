# Conference member controls — mute, unmute, deaf, undeaf, and the rest

Every control Vobiz offers over a participant, what it actually does to the
audio, the status code it really returns, and how to exercise it on a live call.

Base path — note it is **per member**, always:

```
/api/v1/Account/{auth_id}/Conference/{conference_name}/Member/{member_id}/…
```

`member_id` accepts a single id, a comma-separated list (`101,402`), or the
literal string `all`.

---

## The controls

| Control | Method + suffix | Status | Effect on audio | Evidence |
|---|---|---|---|---|
| **Mute** | `POST …/Mute/` | **202** | Others stop hearing this member. They still hear everyone. | **✓ confirmed** — also measured objectively |
| **Unmute** | `DELETE …/Mute/` | **204** | Reverses mute. | **✓ confirmed** |
| **Deaf** | `POST …/Deaf/` | **202** | This member stops hearing the room. They can still be heard. | **✓ confirmed with two humans** |
| **Undeaf** | `DELETE …/Deaf/` | **204** | Reverses deaf. | **✓ confirmed** |
| **Play** | `POST …/Play/` `{"url": …}` | **202** | Audio file, heard **only** by the members named. | ✓ executed on both members |
| **Stop play** | `DELETE …/Play/` | **204** | Stops that playback. | ✓ executed on both members |
| **Speak** | `POST …/Speak/` `{"text": …}` | **202** | Vobiz TTS, heard **only** by the members named. | ✓ executed on both members |
| **Kick** | `POST …/Kick/` | **202** | Removes them from the room; their XML continues at the next element. | **REST ✓ · effect ✓** — `ConferenceExit` fired in the same millisecond |
| **Hang up member** | `DELETE …/Member/{id}/` | **204** | Ends their call outright. | **untested** |
| **Hang up room** | `DELETE /Conference/{name}/` | **204** | Ends the conference for everyone. | 404 on an empty room only |

Mute and deaf are independent. Setting both isolates a member completely:
they neither hear nor are heard, but stay connected.

All eight verified live on 2026-09-02 —
live testing.


## Four things that will bite you

**1. The status codes are not what the docs say.** `POST`-based controls answer
**202 Accepted**, `DELETE`-based ones answer **204 No Content** with an empty
body. Test for any 2xx, never `== 200`.

**2. `member_id` comes back as an array**, even for one member, and `all` is
echoed back literally rather than expanded:

```json
POST …/Member/101/Mute/   →  202  {"message":"muted","member_id":["101"]}
POST …/Member/all/Deaf/   →  202  {"message":"deaf","member_id":["all"]}
DELETE …/Member/101/Mute/ →  204  (empty)
```

**3. Member IDs can only come from callbacks.** The REST read path is broken.

There are internal POST list routes, but neither is reachable through
`api.vobiz.ai`. The GET routes below are served, and answer like this:

```
GET /Conference/                 →  200  {"conferences": []}    with live rooms
GET /Conference/{name}/          →  200  {"error": "failed"}    with live members
```

Both answer **HTTP 200 carrying an error**, so ordinary error handling misses
it. Build the roster from `ConferenceEnter` / `ConferenceExit`, which carry
`ConferenceMemberID`. That is what `store.ROOM_MEMBERS` is for.

**4. There is no room-wide inject.** `POST /Conference/{room}/Play/` and
`/Speak/` return the gateway's generic `{"message":"Not Found"}`, while the
per-member routes return `{"error":"conference not found"}` — that difference is
how you tell which routes exist. Reaching everyone means fanning out one request
per member, or passing `all`.

This is why a **private whisper is the natural primitive here**, not a special
case: audio addressed to one member cannot leak, because there is no room-wide
channel for it to leak into.

---

## The AI's voice does not travel through the conference mix

**Verified live, 2026-09-02.** This is the single most important thing to
understand before testing `deaf`, and it invalidates the obvious test.

The AI is attached to a call leg as a media bug. What it says is injected into
**that leg** with `playAudio` over the WebSocket. It never enters the room mix.

```
AI speaking  ──playAudio──▶  the leg it is attached to        (never the room)
room audio   ──mixer─────▶  every member, subject to `deaf`
```

Consequences:

* **`deaf` cannot silence the AI.** Deafening the customer blocks room audio,
  and the AI is not room audio. Observed exactly this: `deaf` returned 202 and
  the customer went on conversing with the AI for 36 seconds.
* **Testing `deaf` requires two humans.** Deafen one, have the *other* speak.
  There is no way to verify it against the AI.
* **The AI can only address the leg it rides.** It cannot speak to the room. That
  is why the `AI_MODE=tap` fallback has to fan out per-member REST `Speak` — and
  why the briefing in this flow happens on the human's own leg.

Useful corollary: a deafened customer can still be addressed privately by the AI.

## Deafening what the AI hears

The one interaction specific to this project.

Separate from the above, and only about the AI's **ears**.

The AI rides the customer's leg with `audio_track: "inbound"`.
**`mute` on that member provably silences the AI's input** — measured live: a
10.3 s mute window produced zero transcribed turns, and transcription resumed
2.3 s after unmute.

Whether `deaf` on that member also blocks what the AI hears from *other* members
is **untested** — it needs a second human in the room and has not been run.

Practical rule: `mute` on the AI's host leg blinds the AI. Treat `deaf` on that
leg as suspect until measured.

This is the reason the warm-transfer briefing happens on the human agent's leg
*before* they join the room, rather than by deafening the customer while the AI
whispers to them in-room. It avoids the interaction rather than working around
it.

---

## Testing it on a live call

The live console at `http://localhost:8100/panel` drives these against
whichever room currently has members — roster with roles, a button per control,
and the AI transcript beside it.

Headless, the same endpoints:

```bash
TID=$(curl -s localhost:8100/api/panel | python3 -c 'import json,sys;print(json.load(sys.stdin)["tid"])')
curl -s -X POST localhost:8100/api/control/mute \
  -H 'Content-Type: application/json' -d "{\"tid\":\"$TID\",\"member\":\"101\"}"
```

Press the buttons while a call is bridged and confirm by ear — the status code
only proves the platform accepted the command, not that it did anything.

What to listen for:

| Step | Customer hears | Agent hears |
|---|---|---|
| customer muted | everything | nothing from the customer |
| customer deafened | nothing | everything |
| whisper to agent | nothing | the whisper |
| both cleared | everything | everything |

## Automated coverage

```bash
python mock.py --case member-controls
```

Asserts every control against a bridged room, including the exact status codes
and the array-shaped `member_id`.


## Kick has a sharp edge

Kick removes a member but their **XML flow continues** at the next element —
which in both flows here is `<Hangup/>`, so they leave the call. If you want
them removed but still under XML control, put something after the `<Conference>`
element.

A kicked member who rejoins **reuses the same member id**, and a member hangup
issued after a kick-and-rejoin has been observed to return 204 without actually
disconnecting anyone. Confirm removal from the `ConferenceExit` callback rather
than trusting the HTTP response.
