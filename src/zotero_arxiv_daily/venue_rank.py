"""Venue quality ranking based on the CCF recommended list and SCImago Journal Rank (SJR).

Data sources
------------
* CCF list:  https://ccf.atom.im/  (HTML table, 2026 edition at time of writing)
* SJR list:  https://www.scimagojr.com/journalrank.php?out=xls  (semicolon separated CSV)

Each run tries to download fresh copies and falls back to the snapshots kept in
``data/`` (``data/ccf.tsv`` and ``data/scimagojr.csv``).  When a download works the
snapshot is refreshed so the next run (or the next GitHub Actions commit) keeps it.

A venue is identified by any of: a DBLP key (``conf/chi/...``), ISSNs, or free-text
venue names (journal title, proceedings title, arXiv comment / journal_ref).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

import requests
from loguru import logger

CCF_URL = "https://ccf.atom.im/"
SJR_URL = "https://www.scimagojr.com/journalrank.php?out=xls"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

CCF_ORDER = {"A": 3, "B": 2, "C": 1}
SJR_ORDER = {"Q1": 4, "Q2": 3, "Q3": 2, "Q4": 1}

# Words that carry no venue identity and are dropped before comparing names.
_STOP_PHRASES = re.compile(
    r"\b(proceedings of( the)?|proc\.?|the|annual|companion|extended abstracts( of)?|"
    r"\d{1,3}(st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"(19|20)\d{2}|volume \d+|vol\.? ?\d+|part [ivx\d]+)\b",
    flags=re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ALLOWED_TAIL = {"volume", "long", "short", "papers", "paper", "main", "conference", "track",
                 "full", "research", "proceedings", "i", "ii", "iii", "iv", "acm", "ieee", "press"}
_PUBLISHER_PREFIX = re.compile(r"^((acm|ieee|usenix|siam|ifip|acm ieee|ieee acm|acm sigda|acm siggraph|acm sigplan)\s+)+")

# arXiv comments: phrases that mean the venue is NOT (yet) where the paper appears.
_NEGATIVE_CONTEXT = re.compile(r"(under review|submitted to|in submission|workshop|tutorial|symposium on .*workshop)", re.IGNORECASE)
_POSITIVE_CONTEXT = re.compile(
    r"(accepted|to appear|appear(s|ed)? (in|at)|published|camera[- ]ready|presented at|in proceedings|main conference|findings)",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    name = name.replace("&", " and ")
    name = _STOP_PHRASES.sub(" ", name.lower())
    return _NON_ALNUM.sub(" ", name).strip()


def _compact(name: str) -> str:
    return normalize_name(name).replace(" ", "")


def normalize_issn(issn: str) -> Optional[str]:
    digits = re.sub(r"[^0-9Xx]", "", issn or "").upper()
    return digits if len(digits) == 8 else None


@dataclass
class CCFEntry:
    abbr: str
    full_name: str
    rank: str  # A/B/C
    kind: str  # 会议 / 期刊
    dblp: str  # e.g. "conf/chi" or "journals/tog"


@dataclass
class SJREntry:
    title: str
    quartile: Optional[str]  # Q1..Q4 or None
    sjr: Optional[float]
    issns: list[str]
    kind: str  # journal / conference and proceedings / book series ...


@dataclass
class VenueRating:
    ccf: Optional[str] = None
    ccf_venue: Optional[str] = None
    sjr_quartile: Optional[str] = None
    sjr_venue: Optional[str] = None
    venue_hint: Optional[str] = None  # raw venue text we tried to rate
    notes: list[str] = field(default_factory=list)

    @property
    def is_rated(self) -> bool:
        return self.ccf is not None or self.sjr_quartile is not None

    def label(self) -> str:
        parts = []
        if self.ccf:
            parts.append(f"CCF-{self.ccf} ({self.ccf_venue})")
        if self.sjr_quartile:
            parts.append(f"SJR {self.sjr_quartile} ({self.sjr_venue})")
        if not parts:
            return "未评级" + (f" · {self.venue_hint}" if self.venue_hint else "")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _CCFTableParser(HTMLParser):
    """Collects rows of the ccf.atom.im tables (``<tr class="item">``)."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[tuple[str, Optional[str]]]] = []
        self._row: Optional[list[tuple[str, Optional[str]]]] = None
        self._cell_text: Optional[list[str]] = None
        self._cell_href: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and "item" in (attrs.get("class") or "").split():
            self._row = []
        elif self._row is not None and tag in ("td", "th"):
            self._cell_text, self._cell_href = [], None
        elif self._cell_text is not None and tag == "a" and self._cell_href is None:
            self._cell_href = attrs.get("href")

    def handle_endtag(self, tag):
        if self._row is not None and tag in ("td", "th") and self._cell_text is not None:
            self._row.append((" ".join("".join(self._cell_text).split()), self._cell_href))
            self._cell_text = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell_text is not None:
            self._cell_text.append(data)


def dblp_prefix(url_or_key: str | None) -> Optional[str]:
    """``http://dblp.uni-trier.de/db/conf/ppopp/`` or ``conf/ppopp/Foo21`` -> ``conf/ppopp``."""
    if not url_or_key:
        return None
    m = re.search(r"(?:^|/)(conf|journals)/([A-Za-z0-9_\-]+)", url_or_key)
    return f"{m.group(1)}/{m.group(2).lower()}" if m else None


def parse_ccf_html(html: str) -> list[CCFEntry]:
    parser = _CCFTableParser()
    parser.feed(html)
    entries = []
    for row in parser.rows:
        # 序号, 简称, 全称(链接 DBLP), 分类, 类型, 专业领域
        if len(row) < 5:
            continue
        rank = row[3][0].strip().upper()
        if rank not in CCF_ORDER:
            continue
        entries.append(
            CCFEntry(
                abbr=row[1][0].strip(),
                full_name=row[2][0].strip(),
                rank=rank,
                kind=row[4][0].strip(),
                dblp=dblp_prefix(row[2][1]) or "",
            )
        )
    return entries


def ccf_to_tsv(entries: list[CCFEntry]) -> str:
    lines = ["abbr\tfull_name\trank\tkind\tdblp"]
    lines += [f"{e.abbr}\t{e.full_name}\t{e.rank}\t{e.kind}\t{e.dblp}" for e in entries]
    return "\n".join(lines) + "\n"


def parse_ccf_tsv(text: str) -> list[CCFEntry]:
    entries = []
    for i, line in enumerate(text.splitlines()):
        if i == 0 or not line.strip():
            continue
        cols = line.split("\t")
        cols += [""] * (5 - len(cols))
        entries.append(CCFEntry(cols[0], cols[1], cols[2].upper(), cols[3], dblp_prefix(cols[4]) or ""))
    return entries


def parse_sjr_csv(text: str) -> list[SJREntry]:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    entries = []
    for row in reader:
        title = (row.get("Title") or "").strip()
        if not title:
            continue
        quartile = (row.get("SJR Best Quartile") or "").strip().upper()
        quartile = quartile if quartile in SJR_ORDER else None
        try:
            sjr = float((row.get("SJR") or "").replace(",", "."))
        except ValueError:
            sjr = None
        issns = [i for i in (normalize_issn(x) for x in (row.get("Issn") or "").split(",")) if i]
        entries.append(SJREntry(title, quartile, sjr, issns, (row.get("Type") or "").strip()))
    return entries


# ---------------------------------------------------------------------------
# Ranker
# ---------------------------------------------------------------------------


def _download(url: str, timeout: int = 120) -> Optional[str]:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        text = resp.text
        if "Just a moment" in text[:2000] or "cf-challenge" in text[:5000]:
            logger.warning(f"{url} returned a bot-protection page; using the local snapshot instead")
            return None
        return text
    except Exception as exc:  # network errors are expected (Cloudflare, outages)
        logger.warning(f"Failed to download {url}: {exc}")
        return None


class VenueRanker:
    def __init__(
        self,
        ccf_entries: Iterable[CCFEntry],
        sjr_entries: Iterable[SJREntry],
        ccf_min: str = "C",
        sjr_min: str = "Q3",
        keep_unrated: bool = True,
    ):
        self.ccf_min = ccf_min.upper()
        self.sjr_min = sjr_min.upper()
        self.keep_unrated = keep_unrated

        self.ccf_entries = list(ccf_entries)
        self.ccf_by_dblp: dict[str, CCFEntry] = {}
        self.ccf_by_abbr: dict[str, list[CCFEntry]] = {}
        self.ccf_by_name: dict[str, CCFEntry] = {}
        self.ccf_by_spaced_name: dict[str, CCFEntry] = {}
        for e in self.ccf_entries:
            if e.dblp:
                self._keep_best(self.ccf_by_dblp, e.dblp, e)
            if e.abbr:
                self.ccf_by_abbr.setdefault(e.abbr, []).append(e)
            if e.full_name:
                spaced = normalize_name(e.full_name)
                self._keep_best(self.ccf_by_name, spaced.replace(" ", ""), e)
                self._keep_best(self.ccf_by_spaced_name, spaced, e)
                # "ACM Conference on Human Factors..." is usually cited without the publisher.
                bare = _PUBLISHER_PREFIX.sub("", spaced).strip()
                if bare and bare != spaced:
                    self._keep_best(self.ccf_by_name, bare.replace(" ", ""), e)
                    self._keep_best(self.ccf_by_spaced_name, bare, e)
        # Abbreviations sorted longest first so "ACM SIGOPS ATC" wins over "ATC".
        self._abbr_patterns = [
            (abbr, re.compile(r"(?<![A-Za-z0-9])" + re.escape(abbr) + r"(?![A-Za-z0-9])"))
            for abbr in sorted(self.ccf_by_abbr, key=len, reverse=True)
            if len(abbr) >= 3
        ]

        self.sjr_by_issn: dict[str, SJREntry] = {}
        self.sjr_by_title: dict[str, SJREntry] = {}
        for s in sjr_entries:
            for issn in s.issns:
                self.sjr_by_issn.setdefault(issn, s)
            self.sjr_by_title.setdefault(_compact(s.title), s)
        # Long titles only: "Frontiers in Psychology" is safe to find inside a sentence, "AI" is not.
        self._sjr_long_titles = sorted(
            ((k, v) for k, v in self.sjr_by_title.items() if len(k) >= 14 and v.quartile),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )

    @staticmethod
    def _keep_best(index: dict, key: str, entry: CCFEntry):
        old = index.get(key)
        if old is None or CCF_ORDER[entry.rank] > CCF_ORDER[old.rank]:
            index[key] = entry

    # -- construction -------------------------------------------------------

    @classmethod
    def from_config(cls, cfg, data_dir: Path = DATA_DIR, offline: bool = False) -> "VenueRanker":
        data_dir.mkdir(parents=True, exist_ok=True)
        ccf_path = data_dir / "ccf.tsv"
        sjr_path = data_dir / "scimagojr.csv"

        ccf_entries: list[CCFEntry] = []
        html = None if offline else _download(cfg.get("ccf_url", CCF_URL))
        if html:
            ccf_entries = parse_ccf_html(html)
            if len(ccf_entries) >= 300:
                ccf_path.write_text(ccf_to_tsv(ccf_entries), encoding="utf-8")
                logger.info(f"Loaded {len(ccf_entries)} CCF venues from {CCF_URL}")
            else:
                logger.warning(f"Only {len(ccf_entries)} CCF venues parsed online; falling back to snapshot")
                ccf_entries = []
        if not ccf_entries and ccf_path.exists():
            ccf_entries = parse_ccf_tsv(ccf_path.read_text(encoding="utf-8"))
            logger.info(f"Loaded {len(ccf_entries)} CCF venues from snapshot {ccf_path}")
        if not ccf_entries:
            logger.error("No CCF data available; CCF ranking disabled for this run")

        sjr_entries: list[SJREntry] = []
        text = None if offline else _download(cfg.get("sjr_url", SJR_URL), timeout=300)
        if text and text.lstrip("﻿").startswith("Rank;"):
            sjr_entries = parse_sjr_csv(text)
            if len(sjr_entries) >= 10000:
                sjr_path.write_text(text, encoding="utf-8")
                logger.info(f"Loaded {len(sjr_entries)} SJR sources from {SJR_URL}")
            else:
                sjr_entries = []
        if not sjr_entries and sjr_path.exists():
            sjr_entries = parse_sjr_csv(sjr_path.read_text(encoding="utf-8"))
            logger.info(f"Loaded {len(sjr_entries)} SJR sources from snapshot {sjr_path}")
        if not sjr_entries:
            logger.error("No SJR data available; SJR ranking disabled for this run")

        return cls(
            ccf_entries,
            sjr_entries,
            ccf_min=cfg.get("ccf_min", "C"),
            sjr_min=cfg.get("sjr_min", "Q3"),
            keep_unrated=cfg.get("keep_unrated", True),
        )

    # -- matching -----------------------------------------------------------

    def match_ccf_name(self, name: str) -> Optional[CCFEntry]:
        """Match a free-text venue name against CCF full names (and exact abbreviations)."""
        if not name:
            return None
        stripped = name.strip()
        # Exact abbreviation ("CHI", "NeurIPS").  Two-letter abbreviations are too ambiguous
        # (the MDPI journal "AI" is not CCF-A "Artificial Intelligence"), and a venue whose
        # name is literally an SJR journal title is that journal, not a CCF abbreviation.
        if (
            stripped in self.ccf_by_abbr
            and len(stripped) >= 3
            and _compact(stripped) not in self.sjr_by_title
        ):
            return max(self.ccf_by_abbr[stripped], key=lambda e: CCF_ORDER[e.rank])
        compact = _compact(stripped)
        if not compact:
            return None
        if compact in self.ccf_by_name:
            return self.ccf_by_name[compact]
        # Proceedings titles often wrap the official name: "Proceedings of the 36th Annual ACM
        # Symposium on User Interface Software and Technology" ends with the CCF full name.
        # Only filler may follow it, so "AAAI Conference on Artificial Intelligence and
        # Interactive Digital Entertainment" (AIIDE) does not count as AAAI.
        spaced = " " + normalize_name(stripped) + " "
        best = None
        for key, entry in self.ccf_by_spaced_name.items():
            if len(key) < 20:
                continue
            pos = spaced.find(" " + key + " ")
            if pos < 0:
                continue
            tail = spaced[pos + len(key) + 2:].split()
            if all(w in _ALLOWED_TAIL or w.isdigit() or len(w) == 1 for w in tail):
                if best is None or len(key) > len(best[0]):
                    best = (key, entry)
        return best[1] if best else None

    def match_ccf_in_text(self, text: str) -> Optional[CCFEntry]:
        """Find a CCF venue mentioned in an arXiv comment such as 'Accepted at ACL 2026'."""
        if not text:
            return None
        found: list[CCFEntry] = []
        for abbr, pattern in self._abbr_patterns:
            for m in pattern.finditer(text):
                window = text[max(0, m.start() - 60): m.end() + 40]
                if _NEGATIVE_CONTEXT.search(window):
                    continue
                after = text[m.end(): m.end() + 8]
                has_year = re.match(r"\s*'?(19|20)?\d{2}\b", after) is not None
                has_context = _POSITIVE_CONTEXT.search(text[max(0, m.start() - 60): m.start()]) is not None
                if has_year or has_context:
                    found.append(max(self.ccf_by_abbr[abbr], key=lambda e: CCF_ORDER[e.rank]))
                    break
            if found:
                break
        if found:
            return found[0]
        if _POSITIVE_CONTEXT.search(text) and not _NEGATIVE_CONTEXT.search(text):
            return self.match_ccf_name(text)
        return None

    def match_sjr(self, names: Iterable[str] = (), issns: Iterable[str] = ()) -> Optional[SJREntry]:
        for issn in issns:
            n = normalize_issn(issn)
            if n and n in self.sjr_by_issn:
                return self.sjr_by_issn[n]
        for name in names:
            if name and (key := _compact(name)) and key in self.sjr_by_title:
                return self.sjr_by_title[key]
        return None

    def match_sjr_in_text(self, text: str, require_context: bool = True) -> Optional[SJREntry]:
        """Find a journal title inside an arXiv comment or journal_ref."""
        if not text:
            return None
        if require_context and (not _POSITIVE_CONTEXT.search(text) or _NEGATIVE_CONTEXT.search(text)):
            return None
        compact = _compact(text)
        for key, entry in self._sjr_long_titles:
            if key in compact:
                return entry
        return None

    def rate(
        self,
        venue_names: Iterable[str] = (),
        issns: Iterable[str] = (),
        dblp_key: Optional[str] = None,
        free_text: Iterable[str] = (),
        journal_refs: Iterable[str] = (),
    ) -> VenueRating:
        """Rate a venue.

        ``venue_names``: clean venue names (journal / proceedings titles).
        ``journal_refs``: arXiv journal_ref strings (authoritative but noisy: volume, pages...).
        ``free_text``:   arXiv comments; only counted with wording like "accepted at".
        """
        venue_names = [v for v in venue_names if v and v.strip()]
        free_text = [t for t in free_text if t and t.strip()]
        journal_refs = [t for t in journal_refs if t and t.strip()]
        rating = VenueRating(venue_hint=(venue_names or journal_refs or [None])[0])

        ccf = None
        if (prefix := dblp_prefix(dblp_key)) and prefix in self.ccf_by_dblp:
            ccf = self.ccf_by_dblp[prefix]
        for name in venue_names:
            if ccf:
                break
            ccf = self.match_ccf_name(name)
        for ref in journal_refs:
            if ccf:
                break
            ccf = self.match_ccf_name(ref) or self.match_ccf_in_text(ref + " (accepted)")
        for text in free_text:
            if ccf:
                break
            ccf = self.match_ccf_in_text(text)
        if ccf:
            rating.ccf, rating.ccf_venue = ccf.rank, ccf.abbr or ccf.full_name

        sjr = self.match_sjr(venue_names, issns)
        for ref in journal_refs:
            if sjr:
                break
            # "Nature 612, 33-40 (2022)" -> "Nature": the title is what precedes volume/pages.
            head = re.split(r"[\d,(;]", ref, maxsplit=1)[0]
            sjr = self.match_sjr([head]) or self.match_sjr_in_text(ref, require_context=False)
        for text in free_text:
            if sjr:
                break
            sjr = self.match_sjr_in_text(text)
        if sjr and sjr.quartile:
            rating.sjr_quartile, rating.sjr_venue = sjr.quartile, sjr.title
        return rating

    def passes(self, rating: VenueRating) -> bool:
        """Keep a paper if any rated list puts it at or above the threshold.

        Unrated papers (preprints, venues on neither list) follow ``keep_unrated``.
        """
        if not rating.is_rated:
            return self.keep_unrated
        if rating.ccf and CCF_ORDER[rating.ccf] >= CCF_ORDER[self.ccf_min]:
            return True
        if rating.sjr_quartile and SJR_ORDER[rating.sjr_quartile] >= SJR_ORDER[self.sjr_min]:
            return True
        return False
