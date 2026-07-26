---
name: delapan-model
description: Use when the user wants to switch the delapan engine's LLM lineup by optimization profile (cheap/efficiency vs research/quality vs frontier/max-quality), asks which models the engine is using, or wants to cut pipeline cost or maximize extraction quality. Edits model knobs in config.yaml.
---

# Delapan Model

Switch the engine's model lineup by optimization profile. All knobs live in
`config.yaml`; precedence is defaults < yaml < `DLP_<SECTION>__<FIELD>` env.

## Workflow

1. **No profile given?** Report the current lineup:
   `grep -n "model:" config.yaml` plus any `DLP_*` overrides in `.env`.
2. **Profile given** → apply the column below with surgical Edit calls to
   `config.yaml` (keep comments intact). `default` → restore via
   `git checkout -- config.yaml`.
3. **Restart to take effect.** Config is read at process start — restart the MCP
   server / run pipelines in a fresh process.
4. **Verify**: re-grep the changed keys; if unsure a gateway slug exists, check
   `GET /v1/models` with the `AI_GATEWAY_API_KEY`.

## Profiles

| Key | `efficiency` (cost floor) | `research` (quality ceiling) | `frontier` (max quality, premium cost) |
|---|---|---|---|
| `exploration.planner_model` | google/gemini-3.5-flash | anthropic/claude-sonnet-4.6 | anthropic/claude-sonnet-4.6 |
| `exploration.extraction_model` | google/gemini-3.5-flash | google/gemini-3.1-pro-preview | anthropic/claude-fable-5 |
| `exploration.reasoning_effort` | low | high | high |
| `memory.resolution_model` | google/gemini-3.5-flash | anthropic/claude-sonnet-4.6 | anthropic/claude-sonnet-4.6 |
| `knowledge_graph.extraction_model` | anthropic/claude-sonnet-4.6 | anthropic/claude-opus-4.8 | anthropic/claude-fable-5 |
| `deepen.decompose_model` / `critic_model` | google/gemini-3.5-flash | anthropic/claude-opus-4.8 | anthropic/claude-fable-5 |
| `okf.model`, `canvas.answer_model`, `concepts.extract_model` | google/gemini-3.5-flash | anthropic/claude-sonnet-4.6 | anthropic/claude-opus-4.8 |
| `agent.model` | claude-haiku-4-5 | claude-sonnet-4-6 | claude-fable-5 |

High-volume stages (planner, extraction, memory resolution) dominate cost;
low-frequency artifacts (KG, deepen) are where quality pays.

`frontier` reserves Claude Fable 5 (`claude-fable-5` / gateway
`anthropic/claude-fable-5`) for the lowest-frequency, highest-stakes stages
(KG extraction, deepen's critic, chat) — it is Anthropic's top publicly
available tier, priced at $10/$50 per MTok (2x Opus 4.8's $5/$25), confirmed
GA via a deep-research pass on 2026-07-20. Do not default to it: apply only
when the user explicitly asks for max quality regardless of cost.

**Keys not in the table = leave unchanged.** That is deliberate:
`*_fallback_model` keys fire only on failure (negligible volume);
`narration.model` and `synopsis.model` are already cheap-tier;
`agent.fast_model` stays as the designated cheap subtask model even when
`agent.model` equals it under `efficiency`.

## Guardrails

- **Never change `embedding.model` here.** Tier bands (`tiers.*`) are calibrated
  per embedding model and do not transfer; the pgvector dim is pinned. Changing
  it requires re-running `scripts/calibrate_bands.py` — a separate, deliberate task.
- **Two slug namespaces.** Pipeline sections use AI Gateway slugs with DOTS
  (`anthropic/claude-sonnet-4.6`); `agent.*` uses Anthropic API ids with DASHES
  (`claude-sonnet-4-6`). Never mix them.
- Leave non-model knobs (temperatures, caps, thresholds) untouched unless asked.
