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

# 3. write it. --emit-share refuses to overwrite an existing file (see below)
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
both ends of the net-benefit range, and a fingerprint identifying which traces
the window loaded.

Never: prompt text, answer text, file paths, session ids, per-trace timestamps,
project or component names, error messages, or free-form strings of any kind.
The export is built so that every string in it is one of six things: a constant
from the schema, a model id on the published-price whitelist, one of the two
window bounds, the installed traceguard version, a decimal money literal, or
the corpus fingerprint. There is a test that fills a store with sentinel strings in every one
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

## The window must be closed, and that is not enough

Closing the window is the one hard requirement and the export refuses rather
than warns. Both `--since` and `--until`, or `--benchmark` for the frozen
window. An open-ended window is rejected with an error.

The reason is that every rate and every dollar amount in the file scales with
how long you looked. Expired gaps per session, switch rate, ping cost, net
benefit, all of them. A directory where each submission measured "all time" is
not comparable with itself, and no normalisation afterwards fixes it, because
"all time" does not record how long that was.

**An earlier version of this file stopped there and told you to pick a window,
state it, and keep it. That was a necessary condition sold as a sufficient
one, and it is wrong.** The window closes over timestamps. What actually gets
read is the store, and the store keeps growing inside a window that never
moves. `ingest` walks `~/.claude/projects`; a transcript file that only appears
later, or a session that gets resumed and rewritten, or a machine whose
transcripts you sync in next month, all carry messages timestamped well inside
a window you froze weeks ago. Re-running `--benchmark` then produces different
numbers off an identical command line.

That is not hypothetical and it is not small. On this repo's own reference
window, `2026-05-30 .. 2026-08-16`, unchanged to the second:

| | 02eeaa3, 2026-08-17 | 2026-08-18, under 24h later |
|---|---|---|
| sessions | 168 | 174 |
| expired gaps | 432 | 439 |
| argmax net (measured) | $811.30 | $806.82 |

Nothing about the window changed. The corpus did. Every number downstream of
the gap count moved with it, including the one the report puts in a box and
calls a recommendation.

So the window is stated **and** the corpus is identified. `corpus.fingerprint`
is a sha256 over one tuple per trace loaded in the window — session, timestamp,
model, prompt volume, output volume, source — sorted and length-delimited. Same
traffic gives the same digest on any machine; one more trace inside the window
gives a different one. Session ids go into the digest and none comes back out:
it is a single hash over the whole set, and there is no per-record digest
anywhere in the file to line one up against.

**Before you compare two submissions, compare `corpus.fingerprint` and the
`corpus.*` counts.** Two files with the same window and different fingerprints
were computed over different traffic and are not two measurements of one thing.
Two files from the same submitter with the same fingerprint are the same run,
so quoting both is double-counting.

The honest version of the old advice: pick a window, state it, keep it, and
publish the fingerprint so nobody has to take the first three on trust.

## Entries are immutable

`data/NNN-<source>-<first 8 of corpus.fingerprint>.json`, e.g.
`001-traceguard-self-b07cc061.json`.

The fingerprint is in the filename because **a re-run over traffic that has
grown is a new entry, not a new version of an old one.** Same organisation,
same window, more transcripts ingested since: different corpus, different
digest, different file, both kept. Nothing in `data/` is ever edited in place
and nothing is ever overwritten.

`--emit-share` enforces the second half of that: pointed at a path that already
exists, it refuses and exits 2 rather than writing. Delete the file yourself if
you know it was never published; the tool will not make that call for you.

The reason is not tidiness. These files get cited by path. This directory's
first entry is about to be referenced from a public article, as the thing that
replaces "just run it yourself and you will get the same numbers" — a claim
this repo tested and disproved on its own store, which is what the fingerprint
section above is about. A path whose contents can change underneath a citation
cannot carry that job. If `001-traceguard-self-b07cc061.json` said $806.82 the
day someone linked to it, it has to still say $806.82 the day someone follows
the link, even after the corpus behind it has moved on twice.

So: mutable artefacts are fine for dashboards and useless as evidence. If your
numbers change, add a file. Do not fix an old one.

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
`NNN-<something-you-pick>-<first 8 of corpus.fingerprint>.json`. The digest is
in the file; copy the first 8 characters into the name. Or send it to me and I
will open the PR, in which case say whether you want the middle part to
identify you.

If you later re-run over a grown corpus, that is a second entry with a second
fingerprint, not a replacement for the first. Send both if the change is
interesting; the pair is more informative than either alone, because it shows
how much this measurement moves without anyone touching the window.

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
  data/                  one JSON file per submission, named
                         NNN-<source>-<first 8 of fingerprint>.json,
                         never edited and never overwritten
```

`schema/share-v1.json` is a specification document, not a runtime validator.
`traceguard` takes no `jsonschema` dependency to check its own output. Fields
may be added to v1; an existing field never changes name, type or meaning. A
breaking change gets a v2 file next to it and leaves v1 readable.
