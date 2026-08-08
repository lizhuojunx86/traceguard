# `traces_routing_audit.2026-07-04-audit.db`

Copied **2026-08-08 12:02** from the live `traces_routing_audit.db`, immediately
before the first `routing_decisions generate --write` since the published
report. `generate` upserts on `decision_id`, so the pre-regeneration decision
table exists nowhere else.

**Purpose: keep `report_en.md` §3 recomputable.**

## What this copy guarantees

`routing_decisions` reproduces the published §3 figures digit for digit:

| | value |
|---|---|
| decisions (unit × component) | **425** |
| cross-tier deviations | **96** (22.6%) |
| deviation cost | **$1,248.1327** |
| batches | `dec-20260704T090411Z-2e47de` (96), `dec-20260704T105940Z-82e18b` (329) |

## What this copy does NOT guarantee — read before recomputing anything

**`traces.cost_usd` in this file is wrong for `claude-opus-5`.**

The 5,992 opus-5 rows carry **$1,127.77**, the value written by the round-1
backfill (batch `rp-20260808T030632Z-ec404e`) using a `compute_cost_usd` that
could not read the flat `cache_creation_1h` key and so billed every 1-hour cache
write at the 5-minute 1.25x rate. The corrected figure is **$1,213.91**
(batch `rp-20260808T041211Z-e92e55`, in the live DB only).

| | this copy | live DB |
|---|---|---|
| opus-5 cost | $1,127.77 | **$1,213.91** |
| whole store | $13,716.01 | **$13,804.76** |

So this file is **neither** the 2026-07-04 published state (where those rows were
`NULL`, not yet backfilled) **nor** the current correct state. It is precisely
*the last state before regeneration* — which is what it needs to be, but the
filename does not say so.

**Recompute costs from this copy and you get the wrong opus-5 number.** It
guarantees the decision table, not trace costs.

## Do not re-copy

Re-copying would make this a post-fix artifact and destroy the only remaining
pre-regeneration `routing_decisions`. The cost discrepancy above is documented,
not repaired.

## Provenance

- Cost correction: `routing_audit_reprice_log`, batches
  `rp-20260808T030632Z-ec404e` (NULL → low) and `rp-20260808T041211Z-e92e55`
  (low → correct, carries real `old_cost_usd`). Both live-DB only.
- Pricing rule: `pricing.cache_creation_split`.
- Published figures: `report_en.md` §3.
