# Arena public leaderboard (sub-project D, issue #410) — design

Status: approved in brainstorming 2026-10-16. Roadmap: #407 (A, local battle
CLI) → #408 (B, vote upload) → #409 (C, aggregation) → **#410 (D, this)** →
#411 (E, training).

## Intent

People open the leaderboard to learn which local models other people rate
highly, **especially people whose machine is like theirs**. How often a model
shows up in battles is a secondary "how much is it used" hint. The user chose
"ratings from people on similar machines" over "speed on similar machines"
because it is the more practical answer to "what should I run here?".

That one choice reaches back into B and C. Today a vote row carries no
hardware at all (B kept it out on purpose, and C's efficiency axis never needed
it), so a per-machine-group ranking cannot be computed. D therefore has four
parts:

1. **B change**: a vote row gains a coarse memory-group field.
2. **C change**: the aggregation ranks every group that has enough data, adds
   pairwise win counts, and publishes a signed status-only file before the
   first real board exists.
3. **CLI**: `omm arena --leaderboard`.
4. **Web**: a leaderboard page on omm.run.

Doing the B change now is cheap: the live `/votes` node holds 0 rows (deployed
2026-10-16), so there is nothing to migrate.

## Goals

- Rank models by user votes per memory group, plus one overall ranking.
- Show it in the terminal for this machine's group, with numbered rows that
  feed straight into existing commands (`omm install 2`, `omm arena 1 5`).
- Show it on omm.run with the full layout approved in brainstorming (below).
- Before enough votes exist, show the real collection state honestly instead
  of nothing and instead of a thin, misleading ranking.

## Non-goals

- Per-category (coding / translation / writing) boards. B never sends the
  prompt or anything derived from it, so this is structurally impossible; it
  would need its own issue adding a category question to the CLI.
- Speed-on-similar-hardware views from the `telemetry` node.
- Feeding votes into recommend-model training (that is #411).
- Detecting the visitor's hardware on the web page.

## Anti-goals (would make this a failure even if it works)

- **A ranking that one or two machines can set.** The user's first instinct
  for the empty period was "just publish whatever there is"; we explicitly
  chose not to. Publishing a ranking below C's gate is the failure C's gate
  exists to prevent, and it stays forbidden for groups too.
- **One blended number.** Quality tier is the primary sort; efficiency only
  orders models inside a tier, and only within one efficiency `component`
  (C's rule). Never combine them into a single score.
- **A fast-but-bad model looking good.** Same reason as above.
- **Unverified data shown as verified.** Both surfaces show rankings only from
  an artifact whose Ed25519 signature verified.
- **Invented numbers on omm.run.** The site's DIRECTION.md §5.10 requires every
  number to be real; mockup numbers are placeholders only.

## Decisions fixed by the user

1. Ratings by memory group, not speed by hardware.
2. **Mac and PC are grouped differently.** Mac (unified memory) groups by RAM
   size. PC groups by GPU VRAM size, because that is where the model actually
   runs. A PC with no usable GPU is its own "CPU only" family grouped by RAM.
3. **Linux goes with Windows** under "PC". What matters is GPU VRAM, not the OS.
4. A group without enough data shows "표본 부족" and falls back to the overall
   ranking.
5. The web page is on omm.run, layout as approved (mockup v4, see "Web page").
6. The CLI shows this machine's group only, short, grouped by tier (option A),
   with **`[1]`, `[2]` labels** usable by other `omm` commands.
7. No re-consent for users already on "always" (see Part 1).
8. Before the first real board: show the real counts ("37 votes from 4
   machines, more votes needed") and point to `omm arena`. Not a thin ranking.

## Part 1 — B: memory group on the vote row

New optional fields on the wire payload (`arena_upload.build_payload`), the
Worker validator (`validateVoteEvent`), and the private `votes` rules block:

- `memory_kind`: `"unified"` | `"vram"` | `"cpu"`.
- `memory_bucket`: a bucket label string from a fixed table (below).

Both present or both absent (rules enforce the pair like the other symmetric
fields). `schema_version` stays 1: a row without the pair is still valid and
counts toward the overall ranking only. That keeps any already-queued beta
client rows valid. (Builder may bump to 2 if it finds a reason; the overall-only
fallback must hold either way.)

Classification, from `hardware.py`'s `HardwareInfo` at vote time:

- `unified_memory` true → `unified`, bucket by total RAM.
- else a GPU with known `vram_total_gb` → `vram`, bucket by VRAM.
- else → `cpu`, bucket by total RAM.

Intel Macs therefore land in `vram` or `cpu` by their real hardware, which is
the correct behaviour even though the label family says "PC" (they are rare).

**Buckets.** A fixed table of labels named after **marketed sizes**, so a
person finds their machine without arithmetic. The reading is the OS-reported
size in GiB (a 16 GB machine reads ~15.8, an 8 GB card ~7.98), so classify by
rounding to the nearest whole GiB first, then match a closed range:

| `memory_kind` | bucket labels (closed ranges, GiB after rounding) |
|---|---|
| `unified` (Apple Silicon) | `8` (≤ 8) · `16` (9–16) · `18-24` · `32-36` · `48-64` · `96-128` · `192+` |
| `vram` (PC GPU) | `≤6` · `8` (7–8) · `10-12` · `16` (13–16) · `20-24` · `32+` |
| `cpu` (no GPU, RAM) | `≤8` · `16` (9–16) · `32` (17–32) · `64+` |

These are **not** `usage.py`'s buckets: those use half-open `<` edges on the
raw reading, which put a real 16 GB machine in `8-16` and a 24 GB card in
`24+` next to 80 GB cards. The label set is fixed in `validate.ts` and
`database.rules.json` and lives on in stored rows, so changing it later means a
redeploy and mixed data — tests must pin that 8/16/18/24/32/36/48/64/96/128 GB
Macs, 4/6/8/10/12/16/20/24/32/48 GB cards, and 8/16/32/64 GB CPU machines each
land where the table says. (My call: the user only said "by memory, Mac and PC
separately".)

**GPU not detected.** `hardware.py` reads VRAM through NVML or Windows
CIM/registry only, so a Linux AMD machine that Ollama runs on the GPU reports
no GPU. Label `cpu` only where "no GPU" is trustworthy (macOS, Windows); on
Linux with no NVML GPU send **no group at all** — the row still counts in the
overall ranking. Mislabelling GPU machines as "CPU 전용" would break "people
whose machine is like theirs". (My call.)

Privacy: the `usage` channel already sends the same `client_id` together with
bucketed RAM/VRAM and GPU vendor, so pairing a bucket with a vote adds almost
nothing new. Still update, in the same change: `PRIVACY.md`'s votes section,
the session-end consent text in `cli.py`, `validate.ts`, `database.rules.json`
(and redeploy the Worker and rules), and CLAUDE.md's Telemetry paragraph.

**No re-consent (user decision).** Users whose `arena_vote_send_policy` is
already `"always"` are not asked again; they start sending the group fields
with the updated wording only in `PRIVACY.md` and `omm setting upload votes`.
Accepted because the live node holds 0 rows and the channel is beta-only, so
almost nobody is affected. No consent-version mechanism.

**Deploy order.** The Worker validator and RTDB rules must accept the new pair
before any client sends it, or beta clients' rows are rejected and dropped.
So: deploy Worker + rules first (they accept rows with and without the pair),
verify with one real row as was done for #408, then ship the client change.
Deploying is an outward action; the builder confirms with the user at that
step.

## Part 2 — C: groups, pairwise counts, status file

Changes to `scripts/arena_score.py`, `scripts/arena_quality_gate.py`,
`scripts/aggregate_arena_votes.py`, and the artifact.

**Groups.** A group key is `<memory_kind>:<memory_bucket>`. Run the existing
scoring (validation, weighting, Bradley-Terry, tiers, both_bad, efficiency)
once on all rows (= overall, as today) and once per group on that group's rows.
Rows without a group count only in overall.

**Group gate.** A group's ranking is published only when that group alone
passes a gate shaped like the overall one: effective votes, largest client
share ≤ 0.5, and a minimum number of non-provisional models. Thresholds are the
builder's call, lower than overall (my suggestion: 100 effective votes, 3
ranked models), named as constants beside the existing ones. A failing group is
still listed with its vote and machine counts and `"ranked": false`, so both
surfaces can show "표본 부족 · N표".

**Gates are per scope.** Overall and every group are gated independently. A
group can be ranked while overall is still blocked (an early Mac-heavy corpus
can do that): it is published, and the overall scope shows its collecting
counts. "Keep the last good board" also applies per scope: a scope that ranked
before and is blocked now keeps its previous ranking; a scope that newly
passes is published even if another scope is blocked. Top-level `status` is
`ranked` when at least one scope is ranked, else `collecting`. On both
surfaces, an unranked scope falls back to overall if overall is ranked;
otherwise it shows its own counts. (My call.)

**Within-tier ordering — fix C.** `build_artifact`'s `order()` currently sorts
inside a tier by `-efficiency.rating` across all models, but a rating is a
log-ratio centred inside its own efficiency `component` and is meaningless
across components (C's own rule). Fix: inside a tier, order by rating only
among models of the tier's largest component; models of other components (and
those with `efficiency: null`) follow, ordered by quality strength, then key.

**Pairwise counts.** For the win-rate heatmap and the "most-beaten opponent"
line: per scope (overall and each ranked group), per unordered model pair,
`wins` for each side, `both_bad`, and `battles`, after C's existing weighting
and per-client cap. Use effective (weighted) counts and also raw battle counts
so a tooltip can show how many battles a percentage rests on.

**Artifact shape.** `schema_version` becomes 2. The overall board keeps today's
top-level fields so a reader of v1 semantics is easy to port; add:

```json
{
  "schema_version": 2,
  "status": "ranked",
  "groups": [
    {"key": "unified:16-32", "memory_kind": "unified", "memory_bucket": "16-32",
     "ranked": true, "effective_votes": 340.0, "client_count": 12,
     "models": [ /* same per-model shape as the overall list */ ],
     "tiers": [], "provisional_models": []},
    {"key": "vram:16-24", "memory_kind": "vram", "memory_bucket": "16-24",
     "ranked": false, "effective_votes": 40.0, "client_count": 5}
  ],
  "pairs": {
    "overall": [{"a": "sha256:…", "b": "sha256:…", "wins_a": 12.0, "wins_b": 8.5,
                 "both_bad": 2.0, "battles": 25}],
    "unified:16-32": []
  }
}
```

Field names and nesting are the builder's call; the content is not.

**Status-only file (before the first board).** Today a blocked gate publishes
nothing, so there is no file to read. New rule:

- If no ranked board has ever been published, a blocked run publishes a signed
  file with `"status": "collecting"`, the corpus counts (votes, effective
  votes, machines), per-group counts, and the gate's reasons in plain words.
  It has no `models`.
- Once a ranked board exists, a later blocked run keeps the last good board
  (today's behaviour). A collecting file must never overwrite a ranked one —
  for example, the "ranked models collapsed" check firing must not blank the
  public page.
- It is signed and PR'd exactly like a ranked board, and only when its content
  changed, so a quiet day makes no commit.

**Credentials.** Unchanged from the 2026-10-16 fix: the workflow mints a token
from `LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON` (read-only service account
`omm-votes-reader`, role Firebase Realtime Database Viewer) each run.

## Part 3 — CLI `omm arena --leaderboard`

A flag on the existing `arena` command, as the issue says. Mutually exclusive
with the positional models and the battle flags; error clearly if combined.
No new top-level verb (CLI minimalism). Update the docs trio (`cli.py` help,
`docs/commands.json` via `scripts/export_command_reference.py`, README Usage).

Behaviour:

1. Fetch `published/arena-leaderboard.json` + `arena-leaderboard.manifest.json`
   from the same `raw.githubusercontent.com/omm-hippo/omm/main/` base the
   recommend artifacts use, verify with `catalog.py:verify_signed_artifact`
   and `config.py`'s `catalog_public_key`. A verification failure prints an
   error and shows no ranking. Cache like the recommend artifacts do.
2. Classify this machine with the same function Part 1 uses.
3. If this machine's group is ranked, show it. Otherwise one line ("Your group
   (Mac · unified memory 16–32GB) needs more votes — showing all machines")
   and show overall.
4. If the file is `collecting`, show the real counts and point to `omm arena`.
5. If there is no file at all (404 — before the arena work reaches `main` and
   the first nightly run lands), say the leaderboard has not been published
   yet and point to `omm arena`. Same on the web page. Not an error.

**Dependency on `main`.** A, B, C and this work live on `beta`. The nightly
`arena-aggregate.yml` only runs from the default branch and both readers fetch
from `main`, so nothing is published until the arena line is merged into
`main` (planned: once, after D, via PR). Readers point at `main` regardless.

Output (approved; English, like the rest of the CLI):

```
$ omm arena --leaderboard
Arena leaderboard · Mac · unified memory 16–32GB (this machine)
340 votes from 12 machines · updated 2026-10-16 04:00 UTC · signature verified

TIER 1                                quality   efficiency  battles
  [1] Qwen2.5 7B Instruct   Q4_K_M    0.42±0.31        1.4×       57   installed
  [2] Llama 3.1 8B Instruct Q4_K_M    0.38±0.33        1.1×       44   ! often fails
TIER 2
  [3] Gemma 2 9B            Q5_K_M    0.11±0.28        0.8×       31
  [4] Mistral Nemo 12B      Q4_K_M    0.06±0.30           —       27
TIER 3
  [5] Phi-3.5 mini          Q4_K_M   -0.29±0.26        1.9×       39   installed

6 more models need more votes. Full board: https://omm.run/leaderboard

Next: omm install 2   install a model by number
      omm arena 1 5   battle two installed models yourself
      omm info 3      details before installing
```

- Numbers run across tiers from 1. Provisional models get no number.
- **Numbered refs** go into `session_cache.record_results`, the same store
  `omm search` / `omm list` use, so `cli._resolve_ref` resolves them. For an
  installed model the ref is its registry filename (so `omm arena` / `omm
  info` / `omm uninstall` work, `_arena_resolve` already maps numbers that
  way); for a not-installed model it is an installable `repo_id` +
  quantization ref. A model with no `repo_id` (key `filename:…`) still gets a
  number, and `omm install N` on it says plainly it can't be installed from
  the board. Builder confirms each command in the "Next" block really accepts
  the ref it will get, and drops any line that doesn't apply (no installed
  pair → no `arena` line, nothing uninstalled → no `install` line).
- "installed" is matched by `model_digest`/sha256 first, filename second.
- **Efficiency column = relative efficiency**, `exp(rating)` printed as a
  multiplier (`1.3×` = 30% more tok/s per GB than the average model it was
  compared against). That is the machine-independent number C fits; the
  mockups' `tok/s·GB` values were placeholders. Shown only for models in the
  tier's largest component; others print `—` with a short footnote ("measured
  on a disjoint set of battles — not comparable"). `raw_median_tok_s_per_gb`
  is never a column or a sort key (C: it measures the machines, not the
  model); it appears only in the web drawer, labelled "참고: 측정 기기 중앙값".
  The same applies to the web table, the drawer, and the scatter's x-axis
  (scatter plots only models with a comparable rating). (My call.)
- `--json` emits the verified artifact scope being shown plus the resolved
  group, with the same shape whether or not the machine's group was ranked
  (repo rule: JSON shape stays identical across flags).

## Part 4 — Web page on omm.run

Lives in `~/Project/omm-site` (repo `omm-hippo/omm.run`, Next 16 + OpenNext on
Cloudflare), both locales. Must obey that repo's `design/DIRECTION.md`: dark
only, zero-chroma neutrals, exactly one accent (#FFB000), radius ≤ 8px, no
shadows or glass, no emoji, no 3-column icon-card grids, measurable asymmetry,
every number real. Mockup v4 was built from those tokens.

**Data path.** Fetch the artifact + manifest at request time from
`raw.githubusercontent.com/omm-hippo/omm/main/published/` and hold it in the
Cloudflare runtime cache, exactly like `src/lib/omm-docs/source.ts` does for
the README (the OpenNext incremental cache is build-time only). Verify the
Ed25519 signature server-side (WebCrypto in the Worker) with the same public
key as `config.py`'s `catalog_public_key`; add that key to the repo's existing
`check:omm-sync` script so a key rotation can't silently break the page. If
verification fails, show an error state, never the data.

**Layout (approved, top to bottom):**

1. **Hero.** Title ("내 기기에서 사람들이 고른 모델"), one sentence on how
   votes are collected, a KPI strip (votes · machines · ranked models), and
   one mono line "updated … · Ed25519 signature verified".
2. **Board** on a raised surface (`--bg-1`), three columns:
   - **Left: scope nav** on `--bg-0`. "전체" first, then "Mac · 통합 메모리"
     buckets, then "PC · GPU VRAM" buckets and "CPU 전용", each with its vote
     count. Unranked groups are dimmed with a "표본 부족" tag and, when picked,
     show a one-line notice plus the overall board. Default scope: 전체 (the
     page can't know the visitor's machine). Scope belongs in the URL so a
     link can point at a group.
   - **Center: the table.** Big scope title ("Mac · 통합 메모리 16–32GB") and a
     line "이 구간 기기 12대가 남긴 투표 340건". Rows grouped under tier bands
     (no per-row tier column); tier 1 band and rows on `--accent-wash` with a
     2px accent edge and larger names. Columns: model (name + org · quant),
     quality as a number ± CI half-width, relative efficiency (`1.3×`, see
     Part 3), battles, and a
     "실패 잦음" flag when `quality_warning`. No per-row CI bars. Provisional
     models collapsed at the bottom ("+ 표본 부족 모델 6개").
   - **Right: detail drawer** for the selected row on `--bg-2`: name, repo ·
     quant, quality tier and score, 95% interval, both_bad rate, efficiency,
     battles (effective), most-beaten opponent with percentage, a copyable
     `$ omm install <ref>` line, and one accent button "omm arena 로 직접
     비교".
3. **Analysis**, one full-width row each, text and chart alternating sides,
   each with one headline number:
   - **기기별 순위**: models × scopes rank matrix; each scope's #1 cell gets
     the accent fill; unranked cells "—"; current scope's column highlighted.
   - **맞대결 승률**: pairwise heatmap, row beats column; wins on an amber
     ramp, losses on neutral steps; tooltip with battle count.
   - **품질 × 효율**: scatter, quality up, efficiency right, tier by
     accent / ink-1 / ink-3, labelled points.
   The separate confidence-interval chart was dropped by the user.
4. **Collecting state** (before the first ranked board): hero KPIs with the
   real counts, a plain sentence that ranking starts once enough machines have
   voted, per-group counts in the nav, and the `omm arena` call to action. No
   table, no charts.

Copy is Korean on `/ko` and English on the default locale; the user approved
the Korean wording in the mockup as a starting point.

## Testing expectations

- Part 1: bucket boundaries at marketed sizes (see above); payload/validator/
  rules field sets stay identical (the 3-way check B already does);
  a row with only one of the pair is rejected; a row without the pair is
  accepted. Emulator rules test still passes.
- Part 2: per-group scoring equals running the existing scorer on that
  group's rows; group gate blocks a group dominated by one client; pairwise
  counts sum to the scope's battles; a collecting file is never written over a
  ranked file; unchanged corpus → byte-identical output.
- Part 3: verification failure shows no ranking; ranked / unranked-group /
  collecting outputs; numbered refs resolve through `omm install` and
  `omm arena`; `--json` shape identical across cases; `--leaderboard` combined
  with models errors.
- Part 4: signature verification server-side with a tampered artifact →
  error state; collecting state renders with no table; the repo's own QA
  greps (palette lock, radius, no emoji) pass.

## Left to the builder

Exact field names and nesting in the artifact; group gate thresholds; the
bucket rounding rule (pinned by tests); caching TTLs; component structure on
the site; how the drawer behaves on mobile (board columns stack on narrow
screens); chart implementation (inline SVG preferred over a charting library,
to stay inside the palette lock).

## Calls I made (not discussed with the user)

- Optional pair of fields, `schema_version` 1 kept on the vote row.
- Intel Macs classified by real hardware, not by OS.
- Marketed-size bucket table (not `usage.py`'s), rounding then closed ranges.
- Linux with no detected GPU sends no group (overall only), not `cpu`.
- Per-scope gates; a group can rank while overall is collecting.
- Within-tier ordering fixed to stay inside one efficiency component.
- Efficiency shown as relative `exp(rating)` multiplier; raw tok/s·GB only as
  a labelled reference in the web drawer.
- A missing artifact (404) is a "not published yet" state, not an error.
- Group gate thresholds lower than overall.
- Pairwise counts per ranked scope, weighted and raw.
- A collecting file never overwrites a ranked board.
- Web default scope = 전체, scope in the URL.
- Server-side signature verification on the site, key added to `check:omm-sync`.
- Numbered ref = registry filename if installed, else `repo_id`+quant.
