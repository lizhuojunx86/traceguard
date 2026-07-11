# Blind-eval protocol v2 (design record — not implemented)

Contract-external (SPEC §6.6 opt-in tooling). This records what a second round
of the routing_audit blind eval **should** fix, based on the three flaws the
v1 round exposed. It is a design note only — none of the three legs are built
here.

## Why v2 exists — the v1 flaws (all self-inflicted, all disclosed in report §1)

v1 compared, for 12 self-contained advisor consults, the original Fable answer
(A/B-blinded) against an Opus 4.8 cold replay, with the author as reviewer.
Three confounds made the result only weakly interpretable:

1. **Recognition leak — 4/12 ≈ 33%.** The reviewer was the author judging their
   own recent conversations and recognised the original in 4 pairs (hard=3,
   soft=1). Those pairs were excluded from the main conclusion, leaving only 8.
2. **Position imbalance — 9/12 originals in slot B.** The deterministic-hash A/B
   assignment was skewed, so "winner" is collinear with "slot B" and with
   "original answer" — the three cannot be separated from this round.
3. **Cache-state mismatch — the other half of the 100× lesson.** The original
   answers ran warm (accumulated context + prompt cache); the replays ran cold
   (no cache, prompt body only). That is why the cost estimate was ~110× the
   actual, and it also means the two arms were never quality-comparable on
   equal footing: the replay lacked the very context the original relied on.

## The three legs of v2 (record only — do not implement in this cut)

### Leg 1 — third-party reviewer (removes the 33% recognition leak)
Route the A/B sheet to someone who did **not** author the sessions, or add a
long enough delay that the author no longer recognises their own answers.
Success metric: recognition rate falls to ~0; the main conclusion can then use
all pairs, not a post-hoc subset.

### Leg 2 — balanced position assignment (fixes the 9/12 skew)
Replace the deterministic content hash with an assignment that **guarantees**
~50/50 original-in-A vs original-in-B across the batch (e.g. balanced
randomisation or explicit counterbalancing), and store the mapping. Success
metric: original-answer position is uncorrelated with the verdict, so "model
quality" is no longer confounded with "slot" / "original effect".

### Leg 3 — cache-state alignment between the two arms (cold-vs-cold or warm-vs-warm)
Give both arms the **same** context/cache condition:
- **cold-vs-cold**: replay BOTH models from the self-contained prompt only, no
  accumulated context — measures pure single-shot quality; or
- **warm-vs-warm**: replay the candidate model **inside the same conversation
  context** the original had — measures quality under the real working
  condition.
Mixing them (v1's warm original vs cold replay) measures neither cleanly and
inflates both the cost gap and the apparent quality gap. Success metric: the
two arms differ only in the model, not in the context they were given.

## Interpretation rule carried over from v1 (still binding)

The asymmetry stays: **replay-wins-or-ties** is evidence a downgrade is safe;
**original-wins is NOT evidence the premium is justified** until the
context/cache confound (Leg 3) is removed. Until v2 runs, §5b's conclusion
remains "context confound not excluded".
