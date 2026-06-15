# Loop integration: evidence-gating self-improving loops

> Status: groundwork (v0.3). Minimal `traceguard.loop` helper + this guide. The
> goal is to show *where* time-integrity belongs in a self-improving loop, not to
> ship a full agent framework.

## The failure: a loop that contaminates itself

A self-improving / agentic loop typically writes its own outputs back into a
memory store and reads that memory on the next iteration:

```
step 1: model guesses  "Acme revenue = $1.2B"   (no source)
step 2: that guess is now in memory; the model cites it as established fact
step 3: a downstream feature "as of 2023" rests on a number the model invented
        — in 2025
```

Nothing here calls a future model or a future prompt, so the harness invariants
in [SPEC.md](SPEC.md) §5 do not fire. The leak is subtler: the *memory* has
acquired a "fact" that is not traceable to any source that existed before the
cutoff being simulated. Left unchecked, the loop launders its own speculation
into evidence. This is the loop-level form of look-ahead **kind 2** (harness
leakage); the [training_contamination](../examples/training_contamination.py)
example covers kind 1.

## The fix: evidence-gating

Admit a memory write **only if** its supporting evidence has a source timestamp
at or before the cutoff. Unsourced (self-generated) claims and claims sourced
after the cutoff are rejected. That single rule keeps the loop's memory honest:
everything in it could have been known at the simulated time.

`traceguard.loop.EvidenceGate` implements exactly that:

```python
from datetime import datetime, timezone
from traceguard.loop import EvidenceGate

UTC = timezone.utc
gate = EvidenceGate(cutoff=datetime(2023, 1, 1, tzinfo=UTC))

# A claim backed by a source that existed before the cutoff: admitted.
gate.admit(claim="Acme revenue = $1.2B",
           source_as_of=datetime(2022, 6, 30, tzinfo=UTC))   # -> True

# The model's own un-sourced guess: rejected.
gate.admit(claim="Acme revenue = $1.2B", source_as_of=None)  # -> False

# A real source, but dated after the cutoff: rejected.
gate.admit(claim="Acme revenue = $1.3B",
           source_as_of=datetime(2024, 2, 1, tzinfo=UTC))     # -> False
```

`gate.admitted` is the running list of admitted `Evidence`. Pass `strict=True`
to make `admit` raise `EvidenceRejected` instead of returning `False` — useful
in CI or backtests where any inadmissible write should fail loudly.

## Gating memory writes with a decorator

When the loop has a single "write to memory" function, wrap it so inadmissible
writes simply never happen. `claim_from` / `source_as_of_from` pull the claim and
its source timestamp out of the call arguments (the same callable-extractor
pattern as the tracer's `correlation_from` / `feature_as_of_from`):

```python
from traceguard.loop import EvidenceGate, evidence_gated

gate = EvidenceGate(cutoff=cutoff)
memory: list[str] = []

@evidence_gated(
    gate,
    claim_from=lambda claim, **kw: claim,
    source_as_of_from=lambda claim, *, source_as_of: source_as_of,
)
def remember(claim: str, *, source_as_of):
    memory.append(claim)
    return claim

remember("real fact", source_as_of=pre_cutoff_date)  # written
remember("invented fact", source_as_of=None)         # blocked, returns None
```

## Relationship to the invariants

Evidence-gating is the loop-level companion to **invariant 1**
(`feature_as_of` monotonicity): a downstream fact may not rest on inputs that did
not yet exist. Where `validate_feature_as_of` checks already-recorded traces,
`EvidenceGate` intercepts the write *before* the contaminated fact enters memory.
The two compose: gate writes at runtime, then assert the invariants in CI.

## See also

- Runnable sketch: [../examples/loop_self_contamination.py](../examples/loop_self_contamination.py)
- The two kinds of look-ahead: [POSITIONING.md](POSITIONING.md)
- Harness invariants: [SPEC.md](SPEC.md) §5
