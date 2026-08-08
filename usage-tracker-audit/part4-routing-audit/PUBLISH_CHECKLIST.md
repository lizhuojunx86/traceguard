# Part 4 — PUBLISHED 2026-08-08

**https://dev.to/lizhuojunx86/my-routing-policy-and-my-traces-disagreed-96-times-never-once-on-the-main-thread-ffp**

Verified live: no unpublished banner, series header reads **"Auditing AI usage
trackers (4 Part Series)"**, 6 tables rendering, tags `ai` `llm` `devops`
`opensource`.

Note the slug: it ends `-ffp`, not the `-temp-slug-7694868` the draft carried.
**Read the final slug off the published page.** Part five links here; do not
reconstruct it.

Source: `DEVTO_DRAFT_part4_stated-vs-revealed.md`, 3,301 words.

---

## Part five, when it happens

Do this at publish time, not later: paste the URL into this file the same day.
Part three's link had to be scraped off the live profile because that step was
skipped last round, and the slug I guessed in the meantime was a 404.

## Resolved 2026-08-08

- [x] **Part three's URL.** Read off the live profile page:
      `https://dev.to/lizhuojunx86/the-vendor-documents-this-bug-a-30k-star-repo-shipped-it-anyway-27pb`
      Already substituted into the draft. (The slug I had guessed ended `-4h1o`;
      the real one ends `-27pb`. Guessed slugs are 404s, which is why this was
      flagged rather than shipped.)
- [x] **Series name verified against the live site**, not against memory. The
      existing series is `Auditing AI usage trackers`, id 42824, currently
      showing "(3 Part Series)". The draft's front matter matches exactly, so
      publishing will extend it to 4 rather than fork a new one.

## Draft created 2026-08-08 — one click left

**Edit / publish here:**
`https://dev.to/lizhuojunx86/my-routing-policy-and-my-traces-disagreed-96-times-never-once-on-the-main-thread-2ip7-temp-slug-7694868/edit`

State as saved:

| | |
|---|---|
| status | **Unpublished draft.** The URL is public but unlisted |
| title | set |
| tags | `ai` `llm` `opensource` `devops` |
| series | **attached to the existing "Auditing AI usage trackers"**, not a new one — the banner renders "(3 Part Series)" with parts 1–3 listed above the body |
| body | 18,918 characters, byte-identical to the local file |

Rendering checked on the saved draft, not assumed:

- 6 tables, all rendering as tables
- 1 code block, the `grep -L '^model:'` one-liner, intact
- 12 `<h2>` sections, matching the file
- 0 links containing `TODO` or `PART_3` — the part three link resolves
- signed `Li Zhuojun` at the end

- [ ] **Read it, then press Publish.** That button is yours. Publishing under
      your name is your action, the same as sending the email.

## Should decide before publishing (2 items, both "ship without" is fine)

- [ ] **§6 `outcome` disposition.** All 96 deviations carry a `reason`;
      `outcome` is unset on all 425 rows of the frozen audit. The post never
      claims otherwise, so this does not block. Recommendation: ship. Waiting is
      how this post got deferred twice.
- [ ] **The agent lint.** The post closes on a `grep -L '^model:'` one-liner,
      which works today and needs nothing built. If you would rather ship a real
      `traceguard lint-agents` subcommand alongside, that is a stronger CTA and
      a day of work. The grep is not a placeholder — it is a complete answer to
      "do I have this problem", and it is honest that it does not answer "what
      did it cost".

## Publishing steps

1. dev.to → **Create Post** → the pencil/markdown editor → paste the file
   **including the front matter block**. dev.to reads it directly:

   ```
   ---
   title: My routing policy and my traces disagreed 96 times. Never once on the main thread.
   published: false
   tags: ai, llm, opensource, devops
   series: Auditing AI usage trackers
   ---
   ```

3. **`series` must match byte for byte.** Verified against the live site: the
   existing series is `Auditing AI usage trackers` (id 42824). A one-character
   difference silently creates a second series and the post loses its place in
   the sequence.
4. Save as draft first and read the rendered preview. Things to look at
   specifically: the six tables, the `mid → frontier` arrows, and the code
   block at the end.
5. Cover image — match whatever the first three used, or none if they had none.
6. Set `published: true` (or use the Publish button) when the preview is right.

## After publishing

- [ ] Paste the URL back into this file, so part five can link to it without
      anyone reconstructing a slug. **This is the step that was missing and it
      is why part three's link is a TODO above.**
- [ ] Update the TraceGuard README's series list if it carries one.

## What the post commits you to

It ends on a runnable one-liner rather than a conclusion, which is the series'
pattern. It does not promise a part five. If you want one, the strongest
unclaimed thread is the derived-data governance story — annotating a deviation
exempted it from the policy — but that belongs in Series B with the data-source
material, not here. HKEX stays out of both.
