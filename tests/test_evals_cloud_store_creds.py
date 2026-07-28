"""Code acting for a resolved tenant must build its Store from that tenant's creds.

`resolve_tenant` returns a ctx carrying `access_token` + `org_id`; the store must
then be built as `get_store(ctx.access_token, org_id=ctx.org_id)`. A bare
`get_store()` is invisible on the local tier — SQLiteStore ignores both args —
but on cloud it reaches `SupabaseStore(None, org_id=None)` and postgrest raises
``ValueError: Neither bearer token or basic authentication scheme is provided``.

`get_store` is imported inside the function body in build.py/runner.py and at
module level in the adapters, so the behavioural tests patch
``delapan.store.get_store`` (the shared origin) and the sweep covers the rest.

The two behavioural tests are evals-specific; the AST sweep below is repo-wide
(``evals/`` + ``delapan/``) — the same bug surfaced in
``delapan/core/agent/synopsis.py``, so guarding only evals/ would miss its peers.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

from delapan.core.agent.state import TenantContext

REPO_ROOT = Path(__file__).parent.parent
EVALS_DIR = REPO_ROOT / "evals"

# Trees whose modules act on behalf of a resolved tenant. `scripts/` is out: its
# entry points deliberately ternary on `if ctx.access_token else get_store()`,
# and `tests/` is out because the suite runs on the local backend by design.
SWEPT_ROOTS = ("evals", "delapan")

# `delapan/mcp/tenancy.py` is the local/cloud fork itself: its bare calls sit
# inside `if active_backend() == "local":`, the one branch where dropping
# token/org is correct (SQLiteStore accepts neither). Exempt, not broken.
SWEEP_EXEMPT = frozenset({"delapan/mcp/tenancy.py"})

CTX = TenantContext(
    user_id="user-1",
    org_id="org-xyz",
    project_id="proj-1",
    kb_id="kb-1",
    thread_id="thread-1",
    access_token="jwt-abc",
)


class _FakeStore:
    """Minimal store stub — only what the entry points touch before/around get_store."""

    def count_findings(self, kb_id):
        return 0


@pytest.fixture()
def captured_get_store(monkeypatch):
    """Patch delapan.store.get_store, recording the args it was called with."""
    import delapan.store as store_mod

    captured: dict = {}

    def fake_get_store(access_token=None, *, org_id=None):
        captured["access_token"] = access_token
        captured["org_id"] = org_id
        return _FakeStore()

    monkeypatch.setattr(store_mod, "get_store", fake_get_store)
    return captured


def _assert_tenant_creds(captured: dict) -> None:
    assert captured, "get_store was never called"
    assert captured["access_token"] == CTX.access_token, (
        f"store built without the tenant's access_token — got {captured['access_token']!r}"
    )
    assert captured["org_id"] == CTX.org_id, (
        f"store built without the tenant's org_id — got {captured['org_id']!r}"
    )


async def test_build_corpus_builds_store_with_tenant_creds(
    tmp_path, monkeypatch, captured_get_store
):
    """evals/corpus/build.py — the reported cloud-backend failure."""
    import yaml

    from evals.corpus import build as build_mod

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(
        {"name": "test-corpus", "sources": [{"url": "https://example.invalid/a"}]}
    ))

    class _Extraction:
        findings: ClassVar[list] = [{"title": "t", "content": {"fact": "c"}, "confidence": 0.9}]

    class _Outcome:
        affected_finding_ids: ClassVar[list] = ["f1"]

    monkeypatch.setattr(build_mod, "LOCKFILE", tmp_path / "lockfile.json")
    monkeypatch.setattr(build_mod, "resolve_tenant", lambda *a, **k: CTX)
    monkeypatch.setattr(
        build_mod, "extract_findings", lambda *a, **k: _awaited(_Extraction())
    )
    monkeypatch.setattr(
        build_mod, "resolve_and_persist", lambda *a, **k: _awaited(_Outcome())
    )

    await build_mod.build_corpus(manifest, fetcher=lambda url: _awaited("<html/>"))

    _assert_tenant_creds(captured_get_store)


async def test_run_eval_builds_store_with_tenant_creds(
    tmp_path, monkeypatch, captured_get_store
):
    """evals/runner.py — same pattern, same cloud-tier failure."""
    from evals import runner as runner_mod

    monkeypatch.setattr(runner_mod, "resolve_tenant", lambda *a, **k: CTX)
    monkeypatch.setattr(runner_mod, "load_question_set", lambda p: ("empty-set", []))

    set_path = tmp_path / "set.yaml"  # only hashed into the manifest; contents unused
    set_path.write_text("")

    await runner_mod.run_eval(
        set_path=set_path,
        project="p",
        kb="k",
        arms=["production"],
        answer_model="m",
        judge_model="m",
        out_dir=tmp_path / "runs",
    )

    _assert_tenant_creds(captured_get_store)


def _awaited(value):
    """Wrap a value in an already-resolved awaitable (for stubbing async deps)."""

    async def _coro():
        return value

    return _coro()


@pytest.mark.parametrize("root", SWEPT_ROOTS)
def test_no_bare_get_store(root):
    """Sweep `root`: `get_store()` must never be called with no args.

    Catches the evals adapters (which import get_store at module level) and any
    future entry point that resolves a tenant and then drops its credentials —
    including the dormant kind, where every current caller happens to pass a
    store so the unauthenticated branch is never taken in production.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / root).rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SWEEP_EXEMPT:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_store"
                and not node.args
                and not node.keywords
            ):
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        f"bare get_store() in {root}/ — pass the resolved tenant's creds "
        f"(get_store(ctx.access_token, org_id=ctx.org_id)), or require an explicit "
        f"store from the caller if there is no ctx to source them from: {offenders}"
    )


def test_sweep_exemptions_still_exist():
    """A renamed/deleted exempt file would silently void its exemption, leaving
    the sweep quietly narrower than it reads."""
    missing = [rel for rel in SWEEP_EXEMPT if not (REPO_ROOT / rel).is_file()]
    assert not missing, f"SWEEP_EXEMPT names files that no longer exist: {missing}"
