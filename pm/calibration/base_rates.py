"""Historical base-rate store, backed by config/base_rates.yaml.

The YAML format:

    base_rates:
      - name: fed_holds
        category: finance
        question_pattern: "Fed (holds|keeps).*rates"
        p: 0.62
        n_samples: 47
        source: "FedWatch + 2020-2025 FOMC actions"
        notes: "Fed has held in 62% of monitored meetings since 2020."

      - name: incumbent_wins_us_election
        category: politics
        question_pattern: "incumbent.*(wins|reelection)"
        p: 0.71
        n_samples: 18
        source: "Post-WW2 incumbent presidential elections"

A market question is matched against the first regex pattern that fires.
We deliberately use regex over learned embeddings: the rates are
hand-curated and traceable to specific historical samples.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class BaseRate:
    name: str
    category: str | None
    question_pattern: str
    p: float
    n_samples: int = 0
    source: str = ""
    notes: str = ""
    _compiled: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._compiled = re.compile(self.question_pattern, re.IGNORECASE)

    def matches(self, question: str, category: str | None = None) -> bool:
        if self.category and category and self.category.lower() != category.lower():
            return False
        return bool(self._compiled.search(question or ""))


def load(path: Path) -> list[BaseRate]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("base_rates") or []
    out: list[BaseRate] = []
    for entry in raw:
        try:
            out.append(BaseRate(
                name=str(entry["name"]),
                category=entry.get("category"),
                question_pattern=str(entry["question_pattern"]),
                p=float(entry["p"]),
                n_samples=int(entry.get("n_samples", 0)),
                source=str(entry.get("source", "")),
                notes=str(entry.get("notes", ""))))
        except (KeyError, ValueError, re.error) as e:
            log.warning("base_rate skipped (%s): %r", e, entry)
    return out


def first_match(rates: list[BaseRate], question: str,
                category: str | None = None) -> BaseRate | None:
    for r in rates:
        if r.matches(question, category):
            return r
    return None
