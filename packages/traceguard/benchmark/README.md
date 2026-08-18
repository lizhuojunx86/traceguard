# Cache-audit benchmark

A corpus of prompt-cache behaviour from more than one organisation.

Right now it holds **1 submission**, and that one is mine.

## Read this before you quote anything from here

**Under 20 submissions, every cross-organisation number in this directory is an
anecdote. Not a baseline, not an industry figure, not a benchmark in any sense
that word normally carries.** If you put "the average cache hit rate is 96%" in
a slide deck because this directory said so, you will be quoting my laptop.
Nobody's cache behaviour generalises from one store, including mine, and the
axis this corpus exists to measure — how often a session comes back on a
different model after being idle — is exactly the kind of thing that depends on
how a team actually works.

Twenty is not a magic number. It is the point at which a median stops being one
person's habits. Until then, read individual files, compare them to your own,
and say "n=4" out loud when you cite them.

I would rather this directory stay empty than fill up with numbers people trust
more than they should.

## What a submission is

One JSON file from `traceguard`'s cache audit:

```bash
# 1. fill a store from your Claude Code transcripts (once, then incrementally)
python -m traceguard.routing_audit.ingest --write --db sqlite:///traces_routing_audit.db

# 2. look at what you would be sending, in full, before you send it
python -m traceguard.routing_audit.cache_audit \
  --db sqlite:///traces_routing_audit.db \
  --since 2026-05-01 --until 2026-07-31 \
  --show-share

# 3. write it
python -m traceguard.routing_audit.cache_audit \
  --db sqlite:///traces_routing_audit.db \
  --since 2026-05-01 --until 2026-07-31 \
  --emit-share my-cache-audit.json
```

Step 2 is the important one and it is not optional in spirit. `--show-share`
prints the exact bytes `--emit-share` writes, so there is nothing to discover
after the fact. It is about 250 lines. Read all of it.

The tool makes no network calls and has no upload path. You send the file
yourself, to whoever you decide, or not at all.

## What is in the file, and what is not

Aggregates only: per-model token counts and hit rates, gap-bucket counts and
costs, the cross-model switch rate cut by gap length, the keep-alive cap band,
and both ends of the net-benefit range.

Never: prompt text, answer text, file paths, session ids, per-trace timestamps,
project or component names, error messages, or free-form strings of any kind.
The export is built so that every string in it is one of five things — a
constant from the schema, a model id on the published-price whitelist, one of
the two window bounds, the installed traceguard version, or a decimal money
literal. There is a test that fills a store with sentinel strings in every one
of those places and asserts none of them survive:
`test_emit_share_leaks_no_sentinel_from_prompts_paths_sessions_or_model_names`
in `tests/test_cache_share.py`. Two more tests exist only to prove that one can
still fail.

Model ids are whitelisted rather than passed through. If your store has a model
id the tool does not publish a price for, it goes out as `(unrecognized)` with
its token counts intact and its name dropped. That includes public models the
price sheet has not caught up with, so you may see `(unrecognized)` on a
submission that has nothing to hide. That is the cost of the rule and I am not
going to make it smarter.

Dollar figures are list price computed from the token counts in the same file.
They tell a reader nothing the token counts did not already tell them.

## The window must be closed

This is the one hard requirement, and the export refuses rather than warns.

Both `--since` and `--until`, or `--benchmark` for the frozen window. An
open-ended window is rejected with an error.

The reason is that every rate and every dollar amount in the file scales with
how long you looked. Expired gaps per session, switch rate, ping cost, net
benefit — all of them. A directory where each submission measured "all time"
over its own history is not comparable with itself, and no amount of
normalisation afterwards fixes it, because "all time" does not record how long
that was. Two runs of my own store minutes apart already disagreed: the
expired-gap count moved 429 → 432 during one afternoon of editing.

Pick a window, state it, keep it.

## Inclusion criteria

A submission goes in `data/` if all of these hold:

1. **Closed window**, per above. Enforced by the tool.
2. **At least 30 expired gaps.** Below that the cap sweep is fitting noise and
   the decile cut has fewer than 3 gaps per decile. Submit anyway if you like,
   but it lands in `data/thin/` and stays out of any aggregate.
3. **Undecidable share under 0.5.** `data_quality.undecidable_share` is the
   fraction of expired gaps where a NULL `model_id` makes the cross-model
   question unanswerable. Past half, the file cannot speak to the one axis this
   corpus is for. It is still welcome; it is labelled.
4. **Produced by an unmodified released traceguard.** `tool_version` is read
   from installed package metadata, not from a string anyone can edit, so a
   submission cannot claim a version it was not produced by.
5. **You looked at it.** I cannot check this one. Ask yourself whether you would
   be comfortable if the file were public, because in this directory it is.

Window length, model mix and traffic volume are not criteria. Small stores are
useful. A store with one model is useful. A store that shows keep-alive pings
losing money is more useful than another one agreeing with mine.

## How to submit

Open a pull request adding your file to `data/`, named
`NNN-<something-you-pick>.json`. Or send it to me and I will open the PR, in
which case say whether you want the filename to identify you.

Include one sentence about what generated the traffic — "solo dev, mostly
refactoring", "12-person team, CI runs a nightly agent". That sentence is the
only free text in the whole process and it goes in the PR description, not in
the file. It matters because it is the thing the JSON cannot say and the thing
you will need to interpret anyone else's numbers.

If you find your file says something that contradicts what this repo claims,
that is the most valuable submission of the lot. Send it.

## Layout

```
benchmark/
  README.md              you are here
  schema/share-v1.json   field-by-field specification of the export
  data/                  one JSON file per submission
```

`schema/share-v1.json` is a specification document, not a runtime validator.
`traceguard` takes no `jsonschema` dependency to check its own output. Fields
may be added to v1; an existing field never changes name, type or meaning. A
breaking change gets a v2 file next to it and leaves v1 readable.
