# Contributing to delapan

Thanks for your interest. A few ground rules keep the engine clean and the
open/closed boundary intact.

## Developer Certificate of Origin (DCO)

By submitting a pull request you agree to the [DCO](https://developercertificate.org/).
Sign off every commit:

```bash
git commit -s -m "your message"
```

## Dev setup

```bash
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev,local]"
pytest && ruff check .
```

## Architecture rules

- **The engine calls `get_store()` — never import a backend client directly.** Any
  module that reaches for `core/clients/supabase.py` (or any concrete backend) instead
  of the `Store` protocol will be rejected.
- **New backends implement `store/base.py::Store`.** Don't add cloud-only code, auth,
  or multi-tenant logic to `core/` — that belongs behind a `Store` implementation.
- **Imports are `from delapan.*`.** Keep the package self-contained.
- **Best-effort, fails silent for background work.** Context-gathering features
  (exploration, graph population) must degrade to "do nothing visible" on failure and
  never break the caller's flow.

## Tests

Every behavioral change needs a test. Run `pytest` before opening a PR; CI runs the
same plus `ruff` and `pyright`.
