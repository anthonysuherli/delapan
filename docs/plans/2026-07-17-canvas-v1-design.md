# Canvas v1 — first releasable product (design)

> Status: DRAFT — pending ratification. Approach "A" (one-screen canvas product)
> approved 2026-07-17; the defaults below (local-first release, orb character,
> keep-gate governance, Vite frontend home) were recommended and carried, not
> individually ratified — veto in review.

One screen that packages the engine into a product loop:

```
search bar ──▶ explore(persist=False) ──▶ candidates + streamed answer
                                              │ user keeps
                                              ▼
                              core/memory resolver (ADD/UPDATE/NOOP/SUPERSEDE)
                                              │ events
                                              ▼
                     graph pane animates growth · character narrates events
```

The user searches the live web, chats with an on-screen character while doing
it, and watches kept knowledge land in their graph. This realizes vision End
Goal 3 (live growth) and a v1-scoped form of End Goal 4 (keep = the HITL gate;
full consequence preview deferred to v1.1). On ratification, `docs/truenorth/
vision.md` gets an amendment adding the canvas front door + character to the
ratified direction.

## What ships in v1

| Piece | Basis | Delta |
|---|---|---|
| `POST /canvas/search` (SSE) | port from `origin/dev` (June 2026 build) | re-diff against master; explore now fails loudly |
| `POST /canvas/keep` | port from `origin/dev` | route through `core/memory` resolver instead of `persist_candidate_rows` blind insert; return resolution events + finding ids |
| Canvas page | new route in `frontend/` (Vite/sigma app) | search bar · chat/answer/candidate pane · mini graph pane |
| Character overlay | new | SVG orb sprite + event-driven state machine |
| Loud-failure explore | bugfix on master | provider errors (e.g. Tavily HTTP 432) surface as failed runs / SSE error frames — never `count=0` marked completed |
| Local-first packaging | new | one-command run, BYO keys, README quickstart + demo video |

**Not** shipping in v1: consequence-preview diffs, persistent server-push event
stream (deltas ride the search/keep responses), LLM-voiced persona lines, TTS,
hosted/auth tier, br8n anything.

## Backend

**Fresh build on master.** The June 2026 canvas package on `origin/dev` was
the intended port basis, but the remote has no `dev` ref reachable from this repo.
Phase 1 implemented the same contract fresh on master's substrate, reusing
`run_exploration` (with `persist=False`) and `resolve_and_persist` for the
search and keep surfaces respectively.

**Keep → resolver.** `/canvas/keep` calls the mem0-style resolver per candidate.
Response: `{finding_ids, events: [{op: ADD|UPDATE|NOOP|SUPERSEDE, finding_id,
loser_id?}]}`. This is the character's script and the graph pane's delta feed.
Re-keeping overlapping candidates must produce UPDATE/NOOP, not duplicates.

**Loud failures (product trust bug, fix first).** Two known silent failures sit
on the demo path and must fail loudly before anything else is built on top:
1. explore returning `count=0` with status `completed` when the search provider
   rejects (Tavily quota, HTTP 432) — becomes status `failed` + typed error
   surfaced in the SSE stream and MCP result;
2. synopsis rebuild silently no-oping when `ANTHROPIC_API_KEY` is absent in the
   OpenAI-free setup — becomes a logged, surfaced error.

**Graph deltas without a new stream.** v1 animates the *finding-level* graph:
keep responses carry enough to add nodes immediately. KG entity extraction stays
the deeper library view on its existing cadence. A persistent SSE/websocket
delta channel (vision detour "live delta stream") is v1.1.

**Chat = grounded search thread, not an agent framework.** Each submission runs
preamble-grounded answer synthesis over the KB + fresh candidates; the client
passes thread history so follow-ups compose. No revival of `agent/graph.py`.

## Frontend

New `canvas` view beside the existing `graph | findings` toggle (this stays the
one app; the June Next.js `canvas-fe` is not resurrected).

- **Layout**: search bar top; left = conversation (streamed answer, candidate
  cards with keep buttons, character lines); right = mini graph pane reusing the
  sigma renderer with a session lens (nodes kept this session highlighted).
- **Candidate card**: title, source domain, confidence, snippet; keep / dismiss.
- **State rule** (QA lesson from the findings-view Critical bug): KB switch
  while canvas is open resets thread, tray, and session lens — test the
  in-view switch, not just entry paths.

### Character

The divergence `◌` orb, animated (SVG/Lottie sprite), fixed corner of the
canvas pane. **Reactive, never interruptive**: it only speaks in response to
user-initiated events or surfaced state — the anti-Clippy invariant.

| State | Trigger | Line source |
|---|---|---|
| idle | nothing in flight | none (float/blink only) |
| searching | SSE stream open | template ("digging…") |
| found | candidates arrive | template + count |
| conflict | keep event `SUPERSEDE`/`UPDATE` | template + finding titles ("this contradicts what you kept last week — retired the old one; undo?") |
| gap | resume/coverage band `gap`/`sparse` for the query | template + nudge to search |
| error | typed SSE error frame | template + actionable fix ("search provider quota — add a key") |

Lines are deterministic templates in v1 (no extra LLM calls; consistent voice;
zero added latency). Default voice: wry-but-kind, terse. Persona template set
and `character.enabled` live in `config.yaml` — config, never hardcoded — so an
LLM-voiced persona can slot in later as a config-gated upgrade.

## Config & release

- `canvas.*` knobs (candidate cap, keep caps, answer model) and `character.*`
  in `config.yaml`; secrets stay in `.env`. Precedence unchanged.
- **Local-first open-core**: BYO keys (LLM gateway, Tavily), SQLite default
  store (it has full write-primitive parity as of 2026-07-16), Supabase
  optional. No auth tier in v1.
- Release artifact: repo with one-command run (backend serves built frontend or
  a two-process `make run`), quickstart README, demo video (terminal-demo-video
  skill), hosted waitlist link only.

## Testing

- Backend: port canvas route tests from `dev`; new — keep→resolver integration
  (re-keep ⇒ UPDATE/NOOP, no dup rows); explore loud-failure tests (quota ⇒
  run recorded `failed`, error frame emitted, MCP result carries the error).
- Frontend: SSE frame parser units; character state-machine transition table;
  KB-switch-while-in-canvas state test.
- Manual: full loop against live keys — search → keep → node appears →
  character narrates a real SUPERSEDE.

## Build order (each its own plan)

1. **Hardening + port**: loud failures, canvas surface on master, keep→resolver.
2. **Canvas page**: layout, SSE client, candidate tray, session-lens graph pane.
3. **Character layer**: sprite, state machine, templates, config gates.
4. **Packaging**: one-command run, README, demo video, vision amendment.

## Risks

- `origin/dev` divergence makes the port bigger than it looks — re-review, don't
  trust June's green suites.
- Finding-level graph pane may feel sparser than the KG view; mitigated by the
  session lens + counts in character lines.
- Search provider quota remains a single point of failure for the whole demo
  path; BYO-keys README must make the failure mode and fix obvious.
