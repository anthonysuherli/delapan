"""Reproducible run artifacts: manifest + raw per-question records.

    write_run ──► <dir>/manifest.json + records.json (sorted keys)
    load_run  ──► (manifest, records)  — reports render from this alone
"""

from __future__ import annotations

import json
from pathlib import Path


def write_run(out_dir: Path, manifest: dict, records: list[dict]) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out_dir / "records.json").write_text(json.dumps(records, indent=2, sort_keys=True))
    return out_dir


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    records = json.loads((run_dir / "records.json").read_text())
    return manifest, records
