#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
// Part of traceguard — https://github.com/lizhuojunx86/traceguard
/**
 * Decode a concatenation of independent Zstandard frames to stdout.
 *
 * The DSH JSONL backend writes one checksummed frame for the header plus one
 * per durable append batch (session-persistence-jsonl/README.md, "Physical
 * encoding"). A single-frame decode returns only the header, which reads as an
 * empty session rather than an error -- so split on the frame magic and decode
 * each frame independently, exactly as the backend's own recovery scan does.
 *
 * Fallback decoder for dsh_usage_probe.py when neither the `zstandard` Python
 * module nor a `zstd` binary is available. Node >= 22.15 has zlib.zstdDecompressSync.
 *
 *   node zstd_cat.mjs <file.jsonl.zstd> > out.jsonl
 */

import { readFileSync } from 'node:fs'
import { zstdDecompressSync } from 'node:zlib'

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

const path = process.argv[2]
if (!path) {
  process.stderr.write('usage: node zstd_cat.mjs <file.jsonl.zstd>\n')
  process.exit(2)
}

if (typeof zstdDecompressSync !== 'function') {
  process.stderr.write(
    `node ${process.version} has no zlib.zstdDecompressSync -- need >= 22.15, `
    + 'or install the `zstd` binary / the Python `zstandard` module.\n',
  )
  process.exit(3)
}

const raw = readFileSync(path)
const out = []
let pos = 0
let truncated = 0

while (pos < raw.length) {
  const next = raw.indexOf(MAGIC, pos + 4)
  const end = next === -1 ? raw.length : next
  try {
    out.push(zstdDecompressSync(raw.subarray(pos, end)))
  } catch {
    // An incomplete tail frame is crash-recovery territory. Report it on stderr
    // and stop; never guess at partial content.
    truncated += 1
    break
  }
  pos = end
}

if (truncated > 0) {
  process.stderr.write(`warning: ${truncated} incomplete tail frame(s) skipped in ${path}\n`)
}

process.stdout.write(Buffer.concat(out))
