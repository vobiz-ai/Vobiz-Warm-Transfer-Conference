# Billing a warm transfer

The rate card, the per-leg arithmetic, and why cost never appears in a webhook.

---

## The rule that breaks webhook-only integrations

**Cost is not in any webhook.** `TotalCost` and `BillRate` are `0.00000` on
every event of every PSTN call — `StartApp`, `DialAnswer`, `DialConnected`,
`DialHangup`, `Hangup`.

This is not a timing problem. Probing the CDR at the instant the hangup webhook
arrived returned HTTP 200 in **116 ms** with `total_cost: 0.45`,
`billing_status: completed`. The money was already settled; it is simply not in
the payload.

So: no polling delay is needed, but **a CDR fetch is mandatory**.

```http
GET /api/v1/account/{auth_id}/cdr/{call_uuid}      ← lowercase path
```

One exception found in testing: on a **SIP-routed** leg (`RouteType=sip`) the
cost fields *do* populate (`BillRate 0.01`, `TotalCost 0.01000`). PSTN legs are
always zero. Do not build on the exception.

## And it must be done per leg

A handoff produces **two charges**, and the bridged period is billed on both
legs simultaneously. The A-leg's own report is roughly half the truth.

For a bridged pair of legs:

| | A-leg | B-leg |
|---|---|---|
| billsec / ring | 146 / 9 | 127 / 14 |
| pulses | 3 | 3 |
| cost | 1.35 INR | 1.35 INR |
| hangup webhook | **yes** | **none** |

**True cost 2.70 INR — double what the A-leg reports.**

The B-leg sends no hangup webhook at all. In a `<Dial>` bridge its UUID is
disclosed *only* on the Dial `callbackUrl` as `DialBLegUUID`, and the final Dial
`action` may return it empty even when the real-time callback carried it. Both
flows here create the agent leg with the make-call API instead, so the UUID
comes back in the 201 and is stored on the transfer record.

## Rate card

Read from the FreeSWITCH channel variables on account `MA_XXXXXXXX`:

| Variable | Value |
|---|---|
| `vobiz_stream_rate` | 0.3 per 60 s — the `<Stream>` surcharge |
| `vobiz_voice_input_rate` | 0.01 per 60 s |
| `vobiz_mpc_rate` | 0.01 per 60 s |
| `vobiz_cloud_rate`, `vobiz_carrier_rate` | 0.00000 |
| `billing_interval` | 60 |
| `max_conf_members` | 20 (overrides `maxParticipants`) |

Observed per-minute figures: **0.45 INR** on a plain voice leg, **0.65 INR** on a
streaming leg, plus a separate `streaming_cost` line item of **0.20**.

One unresolved discrepancy: `vobiz_stream_rate` says 0.3 per 60 s but the
observed `streaming_cost` was 0.20 on both a 50 s and a 20 s stream. Flagged in
the platform notes.

## The formula

```
billed_pulses(leg) = ceil(billsec / 60)          # rounded UP, 60s minimum
voice(leg)         = billed_pulses × per_minute_rate
stream(leg)        = streaming_cost              # CDR field, separate line item
handoff_total      = Σ over every leg of (voice + stream)
```

`duration = ring_time + billsec`. Ring time is not billable; `billsec` is what
pricing applies to. An unanswered leg still produces a CDR with `billsec: 0` and
`cost: 0` — recorded, not charged.

Worked example, customer 2 m 30 s, human joining at 1 min:

| Leg | billsec | pulses | voice | stream | total |
|---|---|---|---|---|---|
| customer | 150 | 3 | 1.35 | 0.20 | 1.55 |
| agent | 90 | 2 | 0.90 | 0.20 | 1.10 |
| | | | | | **2.65 INR** |

## Two other line items

**Recording** is metered separately: `recording_storage_rate ×
rounded_recording_duration`, retained for `recording_storage_duration` days,
with the same 60 s rounding — 11.96 s of audio stores as 60. Each executed
`<Record>` bills separately. Observed: `recording_storage_rate: 0.005`.

**Duplicate CDRs.** A call terminating on a SIP/WebRTC endpoint has been
observed writing a *third* CDR row from a second media instance, and that row is
billed again. Over 30 days this was 21,977 duplicate rows and ₹8,188 across 20+
accounts, including `MA_XXXXXXXX`. Reconcile against `bridge_uuid` and treat an
unexpected third row for one call as suspect rather than as usage.

## Currency

The CDR **list** endpoint reports `USD` and the **detail** endpoint reports
`INR` for the same call with the same numeric cost. The account balance is INR.
Read currency from the detail endpoint; `report.py` does.
