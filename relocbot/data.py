"""Загрузка и проверка каталога статусов из data/statuses_my.json."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "statuses_my.json"
CONFIDENCE_LEVELS = {"high", "medium", "low"}
STATUS_KINDS = {"document", "trip"}
SOURCE_KINDS = {"official", "university", "secondary", "forum"}


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    kind: str


@dataclass(frozen=True)
class Status:
    id: str
    kind: str
    title: str
    summary: str
    date_prompt: str
    leads: tuple[int, ...]
    lead_notes: dict[int, str]
    facts: tuple[str, ...]
    warnings: tuple[str, ...]
    checklist: tuple[str, ...]
    confidence: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class Tax:
    threshold_days: int
    notes: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class Catalog:
    country: str
    verified_on: str
    disclaimer: str
    statuses: dict[str, Status]
    sources: dict[str, Source]
    tax: Tax

    def documents(self) -> list[Status]:
        return [s for s in self.statuses.values() if s.kind == "document"]

    def trips(self) -> list[Status]:
        return [s for s in self.statuses.values() if s.kind == "trip"]

    def sources_for(self, ids) -> list[Source]:
        return [self.sources[i] for i in ids]


def _check_sources(owner: str, ids, sources: dict[str, Source]) -> None:
    for sid in ids:
        if sid not in sources:
            raise ValueError(f"{owner}: неизвестный источник {sid!r}")


def load_catalog(path: Path | str = DEFAULT_PATH) -> Catalog:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    sources: dict[str, Source] = {}
    for sid, s in raw["sources"].items():
        if s["kind"] not in SOURCE_KINDS:
            raise ValueError(f"источник {sid}: неизвестный тип {s['kind']!r}")
        if not s["url"].startswith("https://") and not s["url"].startswith("http://"):
            raise ValueError(f"источник {sid}: некорректный URL")
        sources[sid] = Source(sid, s["title"], s["url"], s["kind"])

    statuses: dict[str, Status] = {}
    for s in raw["statuses"]:
        sid = s["id"]
        if sid in statuses:
            raise ValueError(f"дублируется статус {sid}")
        if s["kind"] not in STATUS_KINDS:
            raise ValueError(f"{sid}: неизвестный kind {s['kind']!r}")
        if s["confidence"] not in CONFIDENCE_LEVELS:
            raise ValueError(f"{sid}: неизвестная confidence {s['confidence']!r}")
        leads = tuple(int(x) for x in s["leads"])
        if not leads or any(a <= b for a, b in zip(leads, leads[1:])):
            raise ValueError(f"{sid}: leads должны строго убывать")
        notes = {int(k): v for k, v in s.get("lead_notes", {}).items()}
        if not set(notes) <= set(leads):
            raise ValueError(f"{sid}: lead_notes содержит сроки, которых нет в leads")
        _check_sources(sid, s["source_ids"], sources)
        statuses[sid] = Status(
            id=sid,
            kind=s["kind"],
            title=s["title"],
            summary=s["summary"],
            date_prompt=s["date_prompt"],
            leads=leads,
            lead_notes=notes,
            facts=tuple(s["facts"]),
            warnings=tuple(s["warnings"]),
            checklist=tuple(s["checklist"]),
            confidence=s["confidence"],
            source_ids=tuple(s["source_ids"]),
        )

    t = raw["tax"]
    _check_sources("tax", t["source_ids"], sources)
    tax = Tax(int(t["threshold_days"]), tuple(t["notes"]), tuple(t["source_ids"]))

    meta = raw["meta"]
    return Catalog(meta["country"], meta["verified_on"], meta["disclaimer"], statuses, sources, tax)
