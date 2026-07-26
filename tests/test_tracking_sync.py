from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from delapan.tracking.models import BacklogItem, InitiativeRow
from delapan.tracking.sync import SyncPlan, apply_sync, plan_sync
from scripts import tracking_sync

FIXTURES = Path(__file__).parent / "fixtures" / "tracking"


def _initiative(slug: str) -> InitiativeRow:
    return InitiativeRow(
        slug=slug,
        title=slug.title(),
        status="active",
        repo="backend",
        updated="2026-07-17",
        body_md="Next step.\n",
    )


class RecordingQuery:
    def __init__(
        self,
        calls: list[tuple[Any, ...]],
        table: str,
        backlog_positions: list[int],
    ) -> None:
        self.calls = calls
        self.table = table
        self.backlog_positions = backlog_positions
        self.selected = False

    def upsert(self, rows: list[dict[str, Any]]) -> RecordingQuery:
        self.calls.append((self.table, "upsert", rows))
        return self

    def select(self, columns: str) -> RecordingQuery:
        self.calls.append((self.table, "select", columns))
        self.selected = True
        return self

    def delete(self) -> RecordingQuery:
        self.calls.append((self.table, "delete"))
        return self

    def eq(self, column: str, value: str | int) -> RecordingQuery:
        self.calls.append((self.table, "eq", column, value))
        return self

    def insert(self, rows: list[dict[str, Any]]) -> RecordingQuery:
        self.calls.append((self.table, "insert", rows))
        return self

    def execute(self) -> SimpleNamespace | None:
        self.calls.append((self.table, "execute"))
        if self.table == "tracking_backlog" and self.selected:
            return SimpleNamespace(data=[{"position": position} for position in self.backlog_positions])
        return None


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.backlog_positions = [0, 1, 7]

    def table(self, name: str) -> RecordingQuery:
        self.calls.append((name, "table"))
        return RecordingQuery(self.calls, name, self.backlog_positions)


class ExplodingClient:
    def table(self, name: str) -> None:
        raise AssertionError(f"dry-run accessed {name}")


class RemoteSlugClient:
    def table(self, name: str) -> RemoteSlugClient:
        assert name == "tracking_initiatives"
        return self

    def select(self, columns: str) -> RemoteSlugClient:
        assert columns == "slug"
        return self

    def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=[{"slug": "old"}])


def test_plan_upserts_all_and_deletes_orphans() -> None:
    backlog = [BacklogItem(1, "Ship sync", "both", None)]

    plan = plan_sync(
        local=[_initiative("a"), _initiative("b")],
        remote_slugs={"b", "c"},
        backlog=backlog,
    )

    assert isinstance(plan, SyncPlan)
    assert {initiative.slug for initiative in plan.upserts} == {"a", "b"}
    assert plan.delete_slugs == ["c"]
    assert plan.backlog == backlog


def test_dry_run_reports_plan_without_accessing_client(capsys: Any) -> None:
    plan = SyncPlan(
        upserts=[_initiative("a")],
        delete_slugs=["old"],
        backlog=[BacklogItem(1, "Ship sync")],
    )

    apply_sync(ExplodingClient(), plan, dry_run=True)

    assert capsys.readouterr().out.splitlines() == [
        "dry-run: upsert 1 initiatives",
        "dry-run: delete ['old']",
        "dry-run: rewrite backlog (1 items)",
    ]


def test_apply_sync_mirrors_initiatives_and_backlog() -> None:
    client = RecordingClient()
    initiative = _initiative("a")
    backlog = BacklogItem(1, "Ship sync", "frontend", "a")
    plan = SyncPlan(upserts=[initiative], delete_slugs=["old"], backlog=[backlog])

    apply_sync(client, plan, dry_run=False)

    upsert = next(call for call in client.calls if call[1] == "upsert")
    assert upsert[0] == "tracking_initiatives"
    assert upsert[2][0] | {"synced_at": None} == {
        "slug": "a",
        "title": "A",
        "status": "active",
        "repo": "backend",
        "blocked_by": [],
        "spec": None,
        "plan": None,
        "branch": None,
        "body_md": "Next step.\n",
        "updated": "2026-07-17",
        "synced_at": None,
    }
    assert ("tracking_initiatives", "eq", "slug", "old") in client.calls
    assert ("tracking_backlog", "select", "position") in client.calls
    assert ("tracking_backlog", "eq", "position", 0) in client.calls
    assert ("tracking_backlog", "eq", "position", 1) in client.calls
    assert ("tracking_backlog", "eq", "position", 7) in client.calls

    insert = next(call for call in client.calls if call[1] == "insert")
    assert insert[0] == "tracking_backlog"
    assert insert[2][0] | {"synced_at": None} == {
        "position": 1,
        "text": "Ship sync",
        "repo": "frontend",
        "initiative_slug": "a",
        "synced_at": None,
    }


def test_cli_dry_run_uses_fixture_without_writes(
    monkeypatch: Any,
    capsys: Any,
    tmp_path: Path,
) -> None:
    spec = tmp_path / "docs" / "superpowers" / "specs" / "example.md"
    spec.parent.mkdir(parents=True)
    spec.touch()
    monkeypatch.setattr(tracking_sync, "service_client", RemoteSlugClient)

    result = tracking_sync.main(
        [
            "--root",
            str(FIXTURES),
            "--repo-root",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out.splitlines() == [
        "dry-run: upsert 2 initiatives",
        "dry-run: delete ['old']",
        "dry-run: rewrite backlog (2 items)",
    ]


def test_cli_invalid_tracking_exits_before_creating_service_client(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    root = tmp_path / "tracking"
    initiatives = root / "initiatives"
    initiatives.mkdir(parents=True)
    (initiatives / "invalid.md").write_text(
        "---\n"
        "title: Invalid\n"
        "status: unknown\n"
        "repo: backend\n"
        "updated: 2026-07-17\n"
        "---\n",
        encoding="utf-8",
    )
    (root / "backlog.md").write_text("", encoding="utf-8")

    def fail_service_client() -> None:
        raise AssertionError("service_client must not be called for invalid tracking")

    monkeypatch.setattr(tracking_sync, "service_client", fail_service_client)

    result = tracking_sync.main(["--root", str(root), "--repo-root", str(tmp_path)])

    assert result == 1
