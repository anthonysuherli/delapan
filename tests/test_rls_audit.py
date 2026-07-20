"""Pure-function tests for the RLS audit — no database."""

from __future__ import annotations

from scripts.rls_audit import evaluate


def _table(name, rls=True):
    return {"tablename": name, "rowsecurity": rls}


def _policy(table, cmd, qual="(org_id = something)", with_check=None):
    return {"tablename": table, "cmd": cmd, "qual": qual, "with_check": with_check}


def test_clean_table_passes():
    tables = [_table("findings")]
    policies = [
        _policy("findings", "SELECT"),
        _policy("findings", "INSERT", qual=None, with_check="(org_id = x)"),
    ]
    gaps = [g for g in evaluate(tables, policies) if "findings" in g]
    assert gaps == []


def test_rls_disabled_flagged():
    assert any("rowsecurity" in g for g in evaluate([_table("findings", rls=False)], []))


def test_missing_table_flagged():
    assert any("beta_members" in g for g in evaluate([], []))


def test_insert_without_with_check_flagged():
    tables = [_table("kg_nodes")]
    policies = [
        _policy("kg_nodes", "SELECT"),
        _policy("kg_nodes", "INSERT", qual=None, with_check=None),
    ]
    assert any("WITH CHECK" in g for g in evaluate(tables, policies))


def test_unscoped_qual_flagged():
    tables = [_table("kbs")]
    policies = [_policy("kbs", "SELECT", qual="true")]
    assert any("scope" in g.lower() for g in evaluate(tables, policies))
