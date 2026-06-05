"""Finding dedup + confidence scoring.

Ported verbatim from delapan's `features/exploration/merger.py` (logic is
provider-agnostic). Clusters findings by category using fuzzy title matching,
unions content/provenance, and recomputes confidence from unique source count.
"""

from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher

from delapan.core.exploration.models import Finding


def confidence_from_sources(source_count: int) -> float:
    """Diminishing-returns confidence: 1 src=0.40, 2=0.64, 3=0.78, 5=0.92."""
    return 1.0 - (0.6**source_count)


def blended_confidence(source_count: int, quality: float) -> float:
    """Source-count confidence scaled by the reflection critic's quality signal.
    A finding's confidence reflects *both* how many sources agreed and how dense
    the content is — `quality` defaults to 1.0 (unevaluated), leaving the curve
    untouched, so this is a no-op when evaluation is disabled."""
    return confidence_from_sources(source_count) * quality


class FindingMerger:
    def __init__(self, fuzzy_threshold: float, min_confidence: float) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.min_confidence = min_confidence

    def merge_findings(self, findings: list[Finding]) -> list[Finding]:
        """Merge Finding objects by category using fuzzy title matching."""
        by_category: dict[str, list[Finding]] = {}
        for f in findings:
            by_category.setdefault(f.category, []).append(f)

        merged: list[Finding] = []
        for _category, group in by_category.items():
            clusters: list[list[Finding]] = []
            for finding in group:
                placed = False
                for cluster in clusters:
                    if self._similar(finding.title, cluster[0].title):
                        cluster.append(finding)
                        placed = True
                        break
                if not placed:
                    clusters.append([finding])

            for cluster in clusters:
                if len(cluster) == 1:
                    f = cluster[0]
                    source_count = len({p["url"] for p in f.provenance if p.get("url")}) or 1
                    merged.append(
                        f.model_copy(
                            update={
                                "source_count": source_count,
                                "confidence": blended_confidence(source_count, f.quality),
                            }
                        )
                    )
                else:
                    best = max(
                        cluster,
                        key=lambda f: sum(1 for v in f.content.values() if v is not None),
                    )
                    merged_content = dict(best.content)
                    merged_provenance = list(best.provenance)
                    for other in cluster:
                        if other is best:
                            continue
                        for k, v in other.content.items():
                            if k not in merged_content or merged_content[k] is None:
                                merged_content[k] = v
                        merged_provenance.extend(other.provenance)

                    seen_urls: set[str] = set()
                    deduped_provenance: list[dict] = []
                    for p in merged_provenance:
                        url = p.get("url", "")
                        if url not in seen_urls:
                            seen_urls.add(url)
                            deduped_provenance.append(p)

                    source_count = len(seen_urls) or 1
                    earliest_created = min(f.created_at for f in cluster)

                    merged.append(
                        best.model_copy(
                            update={
                                "content": merged_content,
                                "provenance": deduped_provenance,
                                "source_count": source_count,
                                "confidence": blended_confidence(source_count, best.quality),
                                "created_at": earliest_created,
                                "updated_at": datetime.now(timezone.utc),
                            }
                        )
                    )

        # Final quality gate: drop low-signal findings below the configured floor.
        # `min_confidence=0` (or a permissive threshold) keeps everything.
        return [f for f in merged if f.confidence >= self.min_confidence]

    def _similar(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= self.fuzzy_threshold
