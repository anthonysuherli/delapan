# AI cost reduction: agent-driven ingest + pipeline trims

**Date:** 2026-07-27
**Status:** ratified design, pending implementation plan

**Vision goals served:** none directly — this is maintenance work. It exists to satisfy the
Invariant added 2026-07-27, *"AI spend is bounded and observable by construction"*, and it is
scoped so the Invariant *"The KG extraction model stays a frontier model by default"* is
preserved untouched.

---

## Problem

All engine LLM and embedding calls route through the Vercel AI Gateway on a prepaid credit
balance. Three facts established 2026-07-27 motivate this work:

1. **A $0 balance takes the whole engine down.** Every call returns HTTP 402 `insufficient_funds`
   ("a positive credit balance is required for all requests, including BYOK") — explore, synopsis,
   KG build, and chat alike. The failure is visible only in process logs; the explore tool reports
   a generic error. There is no budget ceiling anywhere, so a wedged run drains the balance.
2. **The cost console under-reports.** `usage_events` held 7 rows (last write 2026-07-24) and
   recorded **zero** events for three explores run that day via the fresh-process path. Metering
   fires on the HTTP API path only.
3. **The gateway is zero-markup.** Savings can only come from model choice, reasoning effort,
   token volume, and call volume — not from switching provider or gateway. Migration to direct
   APIs or OpenRouter is not cost-motivated.

Measured spend: fixed infra $52/mo (Vercel Pro seat $20, Supabase Pro $25, Railway $5, Fly $2);
metered AI ≈ $10 across the few days since metering went live, understated per (2). The largest
single metered line was `anthropic/claude-opus-4.8` at $3.53.

## Scope

Two workstreams. **A** cuts cost on the interactive Claude Code path by moving reasoning off the
paid gateway entirely. **B** trims the paid pipeline that the deployed/scheduled surfaces still
depend on, and installs the spend ceiling the new Invariant requires.

**Out of scope:** the metering gap from (2) above. That is being fixed separately (move
instrumentation down to `core/clients/ai_gateway.py` so every entry path is metered). This spec
*depends on* that work for the "observable" half of the Invariant but does not duplicate it.

---

## Workstream A — agent-driven ingest

### Rationale

MCP **sampling** (`sampling/createMessage`) was evaluated as a way to bill LLM work against the
user's Claude Code subscription. It is not viable, on five independent grounds:

- Claude Code does not implement sampling as an MCP client.
- Sampling was **deprecated June 2026** (SEP-2577, Final); the spec directs servers to integrate
  with provider APIs directly.
- The spec mandates a human able to deny each request — incompatible with dozens of calls per run.
- Sampling is completions-only; it cannot serve embeddings, which every finding requires.
- The only reliable explore path is the **fresh-process runner** (MCP-transport explore wedges),
  and a fresh process has no MCP client attached at all.

The viable inversion: rather than the server requesting completions from the client, let the agent
do the reasoning with its own tools and hand delapan the *results* to persist.

### Design

New MCP tool:

```
delapan_add_findings(project: str, kb: str, findings: list[dict]) -> dict
```

Each element of `findings` supplies: `title`, `category`, `content` (dict), `provenance`
(list of `{url, title?}`, **required, non-empty**), and optionally `tags`, `confidence`,
`entity_type`.

The tool builds `Finding` objects (`core/exploration/models.py`) and calls
`resolve_and_persist(ctx, store, candidates, cfg)` — already documented as *"the single
finding-persist path"*. Then it triggers the synopsis rebuild on the same schedule explore uses.

Paired with a skill that instructs Claude Code to research a topic with its own WebSearch /
WebFetch and then call the tool.

### Why this shape

- **Tier parity by construction.** No new `Store` method is introduced, so the Invariant "every
  `Store` method lands on both backends" cannot be violated — `resolve_and_persist` already runs
  identically on SQLite and Supabase, gated only by `memory.enabled`.
- **Dedup for free.** Candidates flow through the existing resolver, yielding
  ADD/UPDATE/NOOP/SUPERSEDE rather than append-only writes.
- **Grounding enforced at the boundary.** Rejecting empty `provenance` makes the `grounded_in`
  Invariant a precondition of the tool rather than a convention callers must remember.

### Cost effect

**Corrected 2026-07-28 during implementation — the original claim here was wrong.** It stated that
only `embed_batch` reaches the gateway, at roughly $0.002 per 70-finding run. That holds only for
findings with no similar existing content.

The actual behavior: `config.yaml` sets `memory.enabled: true`, and `resolve()`
(`core/memory/resolver.py`) defaults every candidate to ADD but escalates to a
`structured_completion` call on `anthropic/claude-sonnet-4.6` for any candidate with a neighbor
above `neighbor_min_similarity` (0.6). Calls are batched at `max_candidates_per_pass` (25), so ~70
overlapping findings cost roughly 3 resolution calls, not 70.

So the honest characterization is: **no pipeline LLM call for genuinely novel findings (embedding
only, ~$0.002); near-duplicates route through the resolution model — which is precisely what makes
dedup work.** Either way this remains dramatically cheaper than `delapan_explore`, which runs
planning, per-page extraction, and evaluation LLM calls throughout, plus deepen when invoked
(~$7 for the three explores measured 2026-07-27). The agent's *research* reasoning is billed to the
Claude Code subscription; only the resolver's dedup judgment stays on the gateway.

### Limits

Interactive Claude Code only. The Fly MCP connector, the Railway backend, and any scheduled run
still use the paid pipeline — which is why Workstream B is not optional.

---

## Workstream B — pipeline trims and spend ceiling

### B1. Deepen model split

`deepen.decompose_model`: `anthropic/claude-opus-4.8` → `google/gemini-3.1-pro-preview`.
`deepen.critic_model`: **unchanged** at `anthropic/claude-opus-4.8`.

Decomposition is structural fan-out (topic → 3-5 facets); the critic is the judgment gate deciding
coverage and when the loop stops. Deepen runs up to `depth_cap: 3` rounds × `facets_per_round: 4`,
so decompose is the higher-volume of the two. This roughly halves deepen cost while protecting the
step where quality actually pays.

### B2. Per-stage reasoning effort

`exploration.reasoning_effort` is today a single knob read by the planner
(`exploration/planner.py:81`), the page extractor (`exploration/extractor.py:51`), and deepen.
Split it:

| New knob | Code default | Target value in `config.yaml` | Rationale |
|---|---|---|---|
| `exploration.planner_reasoning_effort` | `high` (current value) | `high` | The planner decides query diversity. There is a known open defect where explore collapses all findings onto a single subtopic while coverage still reads "rich" — lowering planner effort risks worsening it. |
| `exploration.extraction_reasoning_effort` | `high` (current value) | `medium` | High-volume, per-page. Thinking tokens bill as full-price output, so this is the bulk of the saving. |

Both knobs take the **current** value (`high`) as their code default, so introducing them is a
pure no-op refactor. Lowering extraction to `medium` is a separate, reviewable `config.yaml` edit
in the same change — this keeps "did the split break anything" independently verifiable from "did
lowering effort hurt quality".

**Note — this does not touch KG extraction.** `knowledge_graph.reasoning_effort` is an independent
field, currently `null` (Opus 4.8 rejects the gateway's `reasoning_effort` param and thinks
adaptively). The frontier Invariant is unaffected.

### B3. Gateway budget keys

Three AI Gateway API keys, each with its own monthly budget, summing to ~$50/mo:

| Key | Covers | Budget |
|---|---|---|
| `explore` | exploration pipeline + deepen | $30/mo |
| `kg` | knowledge-graph build | $10/mo |
| `agent` | chat/agent, synopsis, canvas | $10/mo |

Created via `vercel ai-gateway api-keys create --budget <USD> --refresh-period monthly`. Budgets
are enforced **before every request**, making this the only real-time kill switch — and the
mechanism that would have contained the wedged explore run. Auto top-up stays **off**, so the
prepaid balance remains a natural hard ceiling.

This is what satisfies the "bounded" half of the new Invariant. Note that Vercel's team-level
Spend Management governs only hosting spend and **cannot see or cap AI Gateway spend** — the two
are separate pools.

### B4. Vercel dashboard sweep

- Pin builds to **Standard** machines (Elastic is default-on for new Pro teams and bills
  $0.0035/CPU-min; Standard at default concurrency is unbilled).
- Enable the free **WAF Bot Protection** ruleset and Deployment Protection on preview URLs. A
  documented incident had an AI scraper run $1,900 of function charges against a $200 budget on a
  *preview* URL with no alert firing.
- Delete the stray `clean-wt` project.
- Set Spend Management **alerts** (50/75/100%) but leave **auto-pause OFF** — auto-pause halts
  production across every team project with a `503 DEPLOYMENT_PAUSED` and never unpauses on its
  own, which is an outage switch on a live domain, not a safety net.

---

## Testing

Hermetic against the SQLite tier, per the Invariant that the offline suite needs no cloud access.

- `delapan_add_findings` rejects a finding with empty or missing `provenance`.
- `delapan_add_findings` persists through `resolve_and_persist`: re-ingesting overlapping content
  produces NOOP/UPDATE events, not duplicate rows (mirrors the vision's existing "resolution
  observably dedupes" acceptance criterion).
- Introducing the split effort knobs leaves planner and extractor behavior byte-for-byte unchanged
  at their introduction defaults.
- Persisted findings retain `provenance` end to end.

## Acceptance criteria

- A Claude Code session can research a topic and persist findings with **zero** gateway LLM spend
  (embeddings only), verified against the metering table once the metering fix lands.
- `deepen` runs with a Gemini decompose and an Opus critic, and produces a coverage verdict.
- Each of the three gateway keys hard-stops at its budget, verified by setting a $0.01 budget on a
  scratch key and confirming the request is refused pre-flight.
- `knowledge_graph.extraction_model` is still `anthropic/claude-opus-4.8` after all changes.

## Risks

- **Reproducibility.** Agent-driven findings vary with prompting in a way the deterministic
  pipeline does not. Mitigated by routing through the same resolver, so quality problems surface as
  dedup/confidence signals rather than silent duplication. Possible upside: agent-driven research
  is not bound to one planner call and may sidestep the single-subtopic collapse.
- **Split-path drift.** Two ingest paths mean two places findings can be created. Mitigated by both
  terminating at `resolve_and_persist`.
- **Budget too tight.** A $30 explore ceiling could hard-stop a heavy research burst mid-run.
  Budgets are config, adjustable once real metered volume is visible.
