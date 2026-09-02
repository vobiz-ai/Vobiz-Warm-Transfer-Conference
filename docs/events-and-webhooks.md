# Events, webhooks and conference actions

Everything a warm transfer depends on, with the field sets that actually arrive
rather than the ones the docs list. Field observations come from live captures
on accounts `MA_XXXXXXXX` and `MA_YYYYYYYY`.

All voice webhooks are `application/x-www-form-urlencoded` with
`User-Agent: Vobiz`. Not JSON.

---

## Conference participant events

This is the trigger the whole handoff hangs on — the "participant join event".

`<Conference callbackUrl="…" callbackMethod="POST">` delivers **two** events.
There is no `start` or `end`; those have never been observed.

### `ConferenceEnter`

```
Event=ConferenceEnter
ConferenceAction=enter
ConferenceUUID=ec12a3bb-ba9b-43cb-9204-8b3c476c8fba
ConferenceName=ROOM
ConferenceMemberID=249
ConferenceFirstMember=true
CallUUID=60cb7365-d90e-49ae-8f31-50eada3efd64
ALegUUID=…  ALegRequestUUID=…  RequestUUID=…
From=91XXXXXXXXXX  To=91YYYYYYYYYY
Direction=outbound  CallStatus=in-progress
ParentAuthID=MA_XXXXXXXX  BillRate=0.00000
SessionStart=2026-08-31 20:08:21.073308
```

**`ConferenceLastMember` does not mean "this member was the last to leave."** It
is a raw passthrough of FreeSWITCH's `Conference-Size == 0` at the instant the
del-member event fired, i.e. *"the room was empty at this event"*. And exit
callbacks are dispatched with `gevent.spawn`, so **delivery order is not event
order** — two exits can arrive reversed. Reading them as ordered will invert the
sequence, which is exactly what happened to us on 2026-09-02.

### `ConferenceExit` — a much smaller field set

```
Event=ConferenceExit
ConferenceAction=exit
ConferenceUUID=…  ConferenceName=ROOM  ConferenceMemberID=249
ConferenceLastMember=false
CallUUID=…
```

**`From`, `To`, `Direction` and `CallStatus` are absent on exit.** Correlate on
`CallUUID` and `ConferenceMemberID`, never on the numbers.

`ConferenceCurrentSize`, `Timestamp` and `RecordingUrl` are not sent on either
event, despite being documented.

### `ConferenceRemoteSounds` — the wait sound

```
Event=ConferenceRemoteSounds
ConferenceAction=waitSound
ConferenceName=ROOM
ConferenceUUID=          ← empty
ConferenceMemberID=      ← empty
```

It fires on join whether or not the room has already been started by a
`startConferenceOnEnter="true"` member.

### Member IDs come from callbacks, never REST

```
GET /Conference/                 -> 200  {"conferences": []}      even with live rooms
GET /Conference/{name}/          -> 200  {"error": "failed"}      even with live members
GET /Conference/{name}/  (ended) -> 404  {"error": "conference not found"}
```

Every per-member command needs a member ID and REST will not give you one, so
the roster has to be built from `ConferenceEnter` / `ConferenceExit`. That is
what `store.ROOM_MEMBERS` is.

---

## Conference member actions

Base: `/api/v1/Account/{auth_id}/Conference/{name}/Member/{member_id}/…`
`member_id` accepts one ID, a comma-separated list, or the literal `all`.

| Action | Method + suffix | Status | Body |
|---|---|---|---|
| Mute | `POST …/Mute/` | **202** | `{"message":"muted","member_id":["2"]}` |
| Unmute | `DELETE …/Mute/` | **204** | empty |
| Deaf | `POST …/Deaf/` | **202** | `{"message":"deaf","member_id":["2"]}` |
| Undeaf | `DELETE …/Deaf/` | **204** | empty |
| Play (private) | `POST …/Play/` `{"url":…}` | **202** | `{"message":"play queued into conference"}` |
| Stop play | `DELETE …/Play/` | **204** | empty |
| Speak (private) | `POST …/Speak/` `{"text":…}` | **202** | — |
| Kick | `POST …/Kick/` | **202** | `{"message":"kicked","member_id":["2"]}` |
| Hang up member | `DELETE …/Member/{id}/` | **204** | empty |
| Hang up room | `DELETE /Conference/{name}/` | **204** | empty |

Three things to code around:

* **202 and 204, not 200.** Test for any 2xx.
* **`member_id` returns an array**, even for a single member, and echoes the
  literal string `"all"` rather than an expanded roster.
* **There is no room-wide inject.** `POST /Conference/{room}/Play/` and
  `/Speak/` return the gateway's generic `{"message":"Not Found"}`, while the
  per-member routes return `{"error":"conference not found"}`. That difference
  is how you tell which routes exist. Reaching everyone means fanning out one
  request per member — and it is why a private whisper is the natural primitive
  here rather than a special case.

Backend semantics: mute sets `can_speak=false`, deaf sets `can_hear=false`.

---

## `<Conference>` attributes

Confirmed working: `stayAlone`, `startConferenceOnEnter`, `endConferenceOnExit`,
`muted`, `waitSound`, `callbackUrl`, `callbackMethod`, `timeLimit`,
`relayDTMF`, `digitsMatch`, `hangupOnStar`, `action`, `method`.

Known not to behave as documented:

| Attribute | Behaviour |
|---|---|
| `maxParticipants` | parsed, but the account cap `max_conf_members = 20` wins |
| `waitMethod="GET"` | parsed, but the wait sound is always fetched by POST |
| `beep` | parsed; audible tones not guaranteed |
| `record="true"` | **produced zero recordings** on 31 Aug 2026. Use `POST /Conference/{name}/Record/` instead |
| `action` | fired in July, **never fired** on 31 Aug. Do not depend on it; use `callbackUrl` |

`stayAlone="true"` is required whenever a member can be alone in the room. It
initialises `false` and a lone member is kicked out immediately.

---

## Stream events

`<Stream statusCallbackUrl>` delivers these — note that none of them matches a
`started`/`stopped` naming:

```
StartStream  →  PlayedStream  →  ClearedAudio (on barge-in)  →  DroppedStream
```

`StopStream` fires **only when the application initiates the stop**. It does not
fire when the caller hangs up, when the call is killed mid-stream, or on a
Transfer. The `Hangup` webhook is the reliable end-of-call signal.

**Volume warning:** `ClearedAudio` fires once per barge-in, and one 125 s call
produced 15 stream-status callbacks. Size the endpoint accordingly.

WebSocket frames, which are separate from the HTTP callbacks:

| Direction | Events |
|---|---|
| Vobiz → app | `start`, `media`, `dtmf`, `playedStream`, `clearedAudio`, `stop` |
| app → Vobiz | `playAudio`, `checkpoint`, `clearAudio`, `stop` |

Read `start.mediaFormat` on every connection. It is the authority on what Vobiz
is actually sending and it changes the moment the XML is edited.

---

## Call webhooks

**`answer_url`** — `Event=StartApp`, not `Answer`. `CallStatus` is
`in-progress` outbound but **`ringing` inbound**, which is why an inbound leg
must be answered with `<Speak>`/`<Play>`/`<Wait>` before a `<Dial>`.

**`hangup_url`** — all 24 documented fields arrive. Fires **once per call, for
the A-leg only**; a bridged or transferred B-leg sends no hangup webhook at all.

Hangup causes seen in this work:

| Cause | Code | Name | Source |
|---|---|---|---|
| `NORMAL_CLEARING` | 4000 | Normal Hangup | Callee / Answer XML |
| `NORMAL_CLEARING` | 4010 | End Of XML Instructions | Vobiz |
| `NO_ANSWER` | 6010 | Ring Timeout Reached | Vobiz |
| `ALLOTTED_TIMEOUT` | 6000 | Scheduled Hangup | Vobiz |
| `LOSE_RACE` | 9000 | Lost Race | Vobiz |
| `USER_BUSY` | 3010 | Busy Line | Carrier |
| — | 8011 | **Invalid Answer XML** | Error |

That last one is the one to know about: **malformed answer XML is not an HTTP
error.** Vobiz takes the 200, answers, and drops the call about a second later.
Nothing in the webhooks explains it; only the CDR does. A URL with two query
parameters contains a bare `&`, which is enough. `selftest.py` exists for this.

### Field quirks worth handling

* `SessionStart` is **UTC** while `StartTime` / `AnswerTime` / `EndTime` in the
  same payload are **IST**. Only the CDR is self-consistent (ISO-8601 `Z`).
* Webhook `Duration` is answered seconds; CDR `duration` is wall clock including
  ring. Same call: 13 vs 20. Never compare them.
* `BillDuration` is a 60-second pulse, not a duration — a 13 s call reports 60.
* Leading `+` is stripped from `From`/`To` in webhooks, kept on CDR
  `caller_id_number`, dropped on CDR `destination_number`.
* `CountryCode` is empty and `RouteType` is `Not_Domestic` on India-to-India
  PSTN calls. On a SIP-routed leg they are `IN` and `sip`.

### `endConferenceOnExit` kicks everyone, unconditionally

When a member whose `endConferenceOnExit="true"` exits, the platform issues
`conference <room> kick all`. It **does not inspect `stayAlone` on the remaining
members** — that attribute only governs the becoming-alone case and is no
defence here.

The kicked legs get **no hangup channel variables set**, so they surface as
`NORMAL_CLEARING / Callee` — *a platform teardown reported as the participant
hanging up*. On conference legs `HangupSource` therefore cannot distinguish "the
human hung up" from "we dropped them" on a conference leg.

### Webhook signatures

**The documented scheme does not exist.** The real one signs the **URL plus a
nonce**, never the body:

```
X-Vobiz-Signature = base64(HMAC-SHA256(auth_token, base_url + nonce))
```

which is why the documented `timestamp + '.' + JSON.stringify(body)` recipe
could never be made to work, and why the form-encoded body was irrelevant.
Headers are emitted only when the callback URL has auth credentials configured —
that is why some accounts see six signature headers and others see none.

Two of those headers (`auth-token`, `parent-auth-token`) carry account
credentials **in plaintext on every signed callback**. Do not log raw callback
headers.

Historical note — the docs prescribe `x-vobiz-signature` + HMAC-SHA256 over the JSON body.
Captures show six `x-vobiz-signature*` headers present on some accounts and
**none at all** on others, and the body is form-encoded rather than JSON, so the
documented recipe cannot be implemented against real traffic. Until this is
settled, IP allowlisting is the only usable defence.

---
