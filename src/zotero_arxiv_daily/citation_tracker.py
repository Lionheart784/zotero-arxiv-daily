"""Daily "new citation" alerts for a set of Zotero papers.

Google Scholar has no public API and blocks automated queries, so citations come from
Semantic Scholar (primary) and OpenAlex (fallback).  Every run compares today's citing
papers with the ones recorded in ``state/citation_state.json``; papers not seen before
are reported as new citations.  The first time a paper is tracked its existing citations
are recorded as a baseline and not reported.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests
from loguru import logger

from .venue_rank import VenueRanker, VenueRating

S2_API = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API = "https://api.openalex.org"
STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "citation_state.json"

S2_CITATION_FIELDS = (
    "paperId,title,authors,year,publicationDate,venue,publicationVenue,journal,externalIds,url"
)
OPENALEX_SELECT = "id,doi,display_name,publication_year,publication_date,primary_location,authorships,ids"


@dataclass
class TrackedPaper:
    key: str  # stable key in the state file (DOI if available, else Zotero key)
    zotero_key: str
    title: str
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None


@dataclass
class CitingPaper:
    key: str
    title: str
    authors: list[str]
    year: Optional[int]
    url: Optional[str]
    venue_names: list[str] = field(default_factory=list)
    issns: list[str] = field(default_factory=list)
    dblp_key: Optional[str] = None
    source: str = "semantic_scholar"


@dataclass
class CitationAlert:
    citing: CitingPaper
    cited_titles: list[str]
    rating: VenueRating
    kept: bool


@dataclass
class CitationReport:
    alerts: list[CitationAlert] = field(default_factory=list)
    baselined: dict[str, int] = field(default_factory=dict)  # title -> number of existing citations
    failed: list[str] = field(default_factory=list)
    tracked_count: int = 0
    pending_state: Optional[dict[str, Any]] = None  # saved only after the email went out

    @property
    def kept_alerts(self) -> list[CitationAlert]:
        return [a for a in self.alerts if a.kept]

    @property
    def dropped_alerts(self) -> list[CitationAlert]:
        return [a for a in self.alerts if not a.kept]

    @property
    def has_content(self) -> bool:
        return bool(self.kept_alerts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARXIV_RE = re.compile(r"(?:arxiv[:/ ]|abs/|10\.48550/arxiv\.)(\d{4}\.\d{4,5})", re.IGNORECASE)


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)
    return doi or None


def extract_arxiv_id(*texts: Optional[str]) -> Optional[str]:
    for t in texts:
        if t and (m := _ARXIV_RE.search(t)):
            return m.group(1)
    return None


def _title_key(title: str) -> str:
    return "title:" + re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def citing_key(doi: Optional[str], arxiv_id: Optional[str], title: str) -> str:
    if doi := normalize_doi(doi):
        # arXiv DOIs and the arXiv id describe the same preprint
        if m := re.match(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", doi):
            return f"arxiv:{m.group(1)}"
        return f"doi:{doi}"
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    return _title_key(title)


def tracked_from_zotero(items: list[dict]) -> list[TrackedPaper]:
    tracked = []
    for it in items:
        data = it["data"]
        doi = normalize_doi(data.get("DOI"))
        arxiv_id = extract_arxiv_id(data.get("url"), data.get("extra"), data.get("DOI"), data.get("archiveID"))
        key = f"doi:{doi}" if doi else f"zotero:{data['key']}"
        tracked.append(TrackedPaper(key, data["key"], data.get("title") or "(untitled)", doi, arxiv_id))
    return tracked


class _Http:
    def __init__(self, headers: dict[str, str] | None = None, min_interval: float = 1.1):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "zotero-arxiv-daily citation tracker"})
        if headers:
            self.session.headers.update(headers)
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, params: dict | None = None, retries: int = 5) -> Optional[dict]:
        delay = 3.0
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                logger.debug(f"GET {url} failed: {exc}")
                resp = None
            if resp is not None:
                if resp.status_code == 404:
                    return None
                if resp.ok:
                    return resp.json()
                if resp.status_code not in (429, 500, 502, 503, 504):
                    logger.warning(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
                    return None
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"GET {url} kept failing after {retries} attempts")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class SemanticScholarSource:
    name = "semantic_scholar"

    def __init__(self, api_key: Optional[str] = None):
        headers = {"x-api-key": api_key} if api_key else None
        self.http = _Http(headers, min_interval=1.1 if api_key else 3.0)

    def resolve(self, paper: TrackedPaper) -> Optional[str]:
        ids = []
        if paper.doi:
            ids.append(f"DOI:{paper.doi}")
        if paper.arxiv_id:
            ids.append(f"ARXIV:{paper.arxiv_id}")
        for pid in ids:
            data = self.http.get(f"{S2_API}/paper/{pid}", params={"fields": "paperId,title"})
            if data and data.get("paperId"):
                return data["paperId"]
        data = self.http.get(
            f"{S2_API}/paper/search/match", params={"query": paper.title, "fields": "paperId,title"}
        )
        if data and data.get("data"):
            hit = data["data"][0]
            if _title_key(hit.get("title", "")) == _title_key(paper.title):
                return hit["paperId"]
        return None

    def citations(self, paper_id: str) -> list[CitingPaper]:
        out, offset = [], 0
        while offset is not None:
            data = self.http.get(
                f"{S2_API}/paper/{paper_id}/citations",
                params={"fields": S2_CITATION_FIELDS, "limit": 1000, "offset": offset},
            )
            if data is None:
                break
            for row in data.get("data", []):
                c = row.get("citingPaper") or {}
                if not c.get("title"):
                    continue
                ext = c.get("externalIds") or {}
                pv = c.get("publicationVenue") or {}
                journal = c.get("journal") or {}
                venues = [pv.get("name"), journal.get("name"), c.get("venue"), *(pv.get("alternate_names") or [])]
                issns = [pv.get("issn")] + list(pv.get("alternate_issns") or [])
                out.append(
                    CitingPaper(
                        key=citing_key(ext.get("DOI"), ext.get("ArXiv"), c["title"]),
                        title=c["title"],
                        authors=[a.get("name", "") for a in (c.get("authors") or [])],
                        year=c.get("year"),
                        url=(f"https://doi.org/{ext['DOI']}" if ext.get("DOI") else c.get("url")),
                        venue_names=[v for v in venues if v],
                        issns=[i for i in issns if i],
                        dblp_key=ext.get("DBLP"),
                        source=self.name,
                    )
                )
            offset = data.get("next")
        return out


class OpenAlexSource:
    name = "openalex"

    def __init__(self, api_key: Optional[str] = None, mailto: Optional[str] = None):
        self.http = _Http(min_interval=0.2)
        self.params = {}
        if api_key:
            self.params["api_key"] = api_key
        if mailto:
            self.params["mailto"] = mailto

    def resolve(self, paper: TrackedPaper) -> Optional[str]:
        if paper.doi:
            data = self.http.get(f"{OPENALEX_API}/works/doi:{paper.doi}", params=self.params)
            if data and data.get("id"):
                return data["id"].rsplit("/", 1)[-1]
        data = self.http.get(
            f"{OPENALEX_API}/works",
            params={**self.params, "filter": f"title.search:{paper.title}", "per-page": 5},
        )
        for hit in (data or {}).get("results", []):
            if _title_key(hit.get("display_name", "")) == _title_key(paper.title):
                return hit["id"].rsplit("/", 1)[-1]
        return None

    def citations(self, work_id: str) -> list[CitingPaper]:
        out, cursor = [], "*"
        while cursor:
            data = self.http.get(
                f"{OPENALEX_API}/works",
                params={**self.params, "filter": f"cites:{work_id}", "per-page": 200,
                        "cursor": cursor, "select": OPENALEX_SELECT},
            )
            if data is None:
                break
            for w in data.get("results", []):
                title = w.get("display_name")
                if not title:
                    continue
                src = ((w.get("primary_location") or {}).get("source")) or {}
                ids = w.get("ids") or {}
                arxiv_id = extract_arxiv_id(ids.get("arxiv"), (w.get("primary_location") or {}).get("landing_page_url"))
                out.append(
                    CitingPaper(
                        key=citing_key(w.get("doi"), arxiv_id, title),
                        title=title,
                        authors=[(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])],
                        year=w.get("publication_year"),
                        url=w.get("doi") or w.get("id"),
                        venue_names=[src.get("display_name")] if src.get("display_name") else [],
                        issns=list(src.get("issn") or []),
                        source=self.name,
                    )
                )
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not data.get("results"):
                break
        return out


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class CitationTracker:
    def __init__(self, ranker: Optional[VenueRanker], state_path: Optional[Path] = None,
                 sources: Optional[list] = None, alert_on_first_run: bool = False):
        self.ranker = ranker
        self.state_path = state_path or STATE_PATH
        self.alert_on_first_run = alert_on_first_run
        if sources is None:
            sources = [
                SemanticScholarSource(os.environ.get("S2_API_KEY") or None),
                OpenAlexSource(os.environ.get("OPENALEX_API_KEY") or None, os.environ.get("OPENALEX_MAILTO") or None),
            ]
        self.sources = sources

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(f"{self.state_path} is corrupted; starting a new citation baseline")
        return {"version": 1, "tracked": {}}

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    def _fetch_citations(self, paper: TrackedPaper) -> Optional[list[CitingPaper]]:
        for source in self.sources:
            try:
                pid = source.resolve(paper)
                if pid is None:
                    logger.info(f"[{source.name}] could not find '{paper.title}'")
                    continue
                cites = source.citations(pid)
                logger.info(f"[{source.name}] '{paper.title}': {len(cites)} citing papers")
                return cites
            except Exception as exc:
                logger.warning(f"[{source.name}] failed for '{paper.title}': {exc}")
        return None

    def commit(self, report: CitationReport) -> None:
        """Persist the citations seen in ``report`` (call after the email was sent)."""
        if report.pending_state is not None:
            self.save_state(report.pending_state)
            report.pending_state = None

    def run(self, tracked: list[TrackedPaper], save: bool = True) -> CitationReport:
        state = self.load_state()
        report = CitationReport(tracked_count=len(tracked))
        new_by_key: dict[str, CitationAlert] = {}
        today = date.today().isoformat()

        for paper in tracked:
            cites = self._fetch_citations(paper)
            if cites is None:
                report.failed.append(paper.title)
                continue  # keep yesterday's state untouched
            entry = state["tracked"].get(paper.key)
            current = {c.key: c for c in cites}
            if entry is None:
                state["tracked"][paper.key] = {"title": paper.title, "since": today, "seen": sorted(current)}
                report.baselined[paper.title] = len(current)
                if not self.alert_on_first_run:
                    continue
                new_keys = list(current)
            else:
                seen = set(entry.get("seen", []))
                new_keys = [k for k in current if k not in seen]
                entry["seen"] = sorted(seen | set(current))
                entry["title"] = paper.title
            for k in new_keys:
                c = current[k]
                if k in new_by_key:
                    if paper.title not in new_by_key[k].cited_titles:
                        new_by_key[k].cited_titles.append(paper.title)
                    continue
                rating = (
                    self.ranker.rate(venue_names=c.venue_names, issns=c.issns, dblp_key=c.dblp_key)
                    if self.ranker else VenueRating()
                )
                kept = self.ranker.passes(rating) if self.ranker else True
                new_by_key[k] = CitationAlert(c, [paper.title], rating, kept)

        state["last_run"] = today
        report.pending_state = state
        if save:
            self.commit(report)
        report.alerts = list(new_by_key.values())
        logger.info(
            f"Citation tracking: {len(report.kept_alerts)} new citations kept, "
            f"{len(report.dropped_alerts)} dropped by venue filter, {len(report.failed)} papers failed"
        )
        return report
