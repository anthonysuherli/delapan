"""resolve_project / resolve_kb keep find-or-create semantics on a lost create race."""

from __future__ import annotations

import pytest
from postgrest.exceptions import APIError

from delapan.store.supabase import SupabaseStore


def _dup_error() -> APIError:
    return APIError(
        {"message": "duplicate key value", "code": "23505", "hint": "", "details": ""}
    )


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, parent):
        self.parent = parent
        self.kind = "select"

    def select(self, *args):
        self.kind = "select"
        return self

    def insert(self, row):
        self.kind = "insert"
        return self

    def eq(self, *args):
        return self

    def limit(self, *args):
        return self

    def execute(self):
        if self.kind == "insert":
            self.parent.inserts += 1
            raise self.parent.insert_error
        # select: empty before the raced insert, the winner's row after it
        return _Result([self.parent.row] if self.parent.inserts else [])


class _RacingClient:
    """First select finds nothing, insert loses the race, re-select finds the winner."""

    def __init__(self, row, insert_error: APIError | None = None):
        self.row = row
        self.inserts = 0
        self.insert_error = insert_error or _dup_error()

    def table(self, name):
        return _Query(self)


def make_store(monkeypatch, fake) -> SupabaseStore:
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1")


def test_resolve_project_lost_race_reselects(monkeypatch):
    store = make_store(monkeypatch, _RacingClient({"id": "p1"}))
    assert store.resolve_project("repoA", create=True) == ("org1", "p1")


def test_resolve_kb_lost_race_reselects(monkeypatch):
    store = make_store(monkeypatch, _RacingClient({"id": "k1"}))
    assert store.resolve_kb("org1", "p1", "main", create=True) == "k1"


def test_non_conflict_apierror_still_raises(monkeypatch):
    err = APIError({"message": "boom", "code": "500", "hint": "", "details": ""})
    store = make_store(monkeypatch, _RacingClient({"id": "p1"}, insert_error=err))
    with pytest.raises(APIError):
        store.resolve_project("repoA", create=True)
