"""Tests for zotero_arxiv_daily.citation_tracker using fake citation sources (offline)."""

import json

from zotero_arxiv_daily.citation_tracker import (
    CitationTracker,
    CitingPaper,
    TrackedPaper,
    citing_key,
    extract_arxiv_id,
    normalize_doi,
    tracked_from_zotero,
)
from zotero_arxiv_daily.construct_email import render_email
from zotero_arxiv_daily.venue_rank import VenueRanker, parse_sjr_csv

SJR_CSV = (
    "Rank;Sourceid;Title;Type;Issn;Publisher;Open Access;Open Access Diamond;SJR;SJR Best Quartile\n"
    '1;1;"Entertainment Computing";journal;"18759521";"Elsevier";No;No;0,700;Q2\n'
    '2;2;"Journal of Obscure Studies";journal;"12345679";"Nobody";No;No;0,100;Q4\n'
)


class FakeSource:
    name = "fake"

    def __init__(self, citations: dict[str, list[CitingPaper]], fail: set[str] = frozenset()):
        self.citations_by_id = citations
        self.fail = fail

    def resolve(self, paper):
        if paper.key in self.fail:
            raise RuntimeError("boom")
        return paper.key if paper.key in self.citations_by_id else None

    def citations(self, pid):
        return list(self.citations_by_id[pid])


def cp(key, title, venue=None):
    return CitingPaper(key=key, title=title, authors=["A. Author"], year=2026,
                       url=f"https://doi.org/{key}", venue_names=[venue] if venue else [])


def test_helpers():
    assert normalize_doi("https://doi.org/10.1145/ABC") == "10.1145/abc"
    assert extract_arxiv_id("http://arxiv.org/abs/2502.17878") == "2502.17878"
    assert citing_key("10.48550/arXiv.2502.17878", None, "x") == "arxiv:2502.17878"
    assert citing_key(None, None, "Hello, World!") == "title:helloworld"


def test_tracked_from_zotero():
    items = [
        {"data": {"key": "AAA", "title": "Paper A", "DOI": "10.1/X", "url": "", "extra": ""}},
        {"data": {"key": "BBB", "title": "Paper B", "DOI": "", "url": "http://arxiv.org/abs/2502.17878", "extra": ""}},
    ]
    tracked = tracked_from_zotero(items)
    assert tracked[0].key == "doi:10.1/x" and tracked[0].doi == "10.1/x"
    assert tracked[1].key == "zotero:BBB" and tracked[1].arxiv_id == "2502.17878"


def test_first_run_is_baseline_then_reports_new(tmp_path):
    state = tmp_path / "state.json"
    paper = TrackedPaper("doi:10.1/a", "AAA", "Paper A", "10.1/a")
    source = FakeSource({"doi:10.1/a": [cp("doi:old", "Old citing paper")]})
    ranker = VenueRanker([], parse_sjr_csv(SJR_CSV))

    first = CitationTracker(ranker, state, sources=[source]).run([paper])
    assert first.baselined == {"Paper A": 1}
    assert first.alerts == []

    source.citations_by_id["doi:10.1/a"] += [
        cp("doi:new1", "Good new paper", "Entertainment Computing"),
        cp("doi:new2", "Weak new paper", "Journal of Obscure Studies"),
        cp("arxiv:2601.00001", "Preprint citing us"),
    ]
    second = CitationTracker(ranker, state, sources=[source]).run([paper])
    kept = {a.citing.title for a in second.kept_alerts}
    assert kept == {"Good new paper", "Preprint citing us"}
    assert [a.citing.title for a in second.dropped_alerts] == ["Weak new paper"]

    # Nothing new on the third day, and dropped papers are not reported again.
    third = CitationTracker(ranker, state, sources=[source]).run([paper])
    assert third.alerts == []
    saved = json.loads(state.read_text())
    assert len(saved["tracked"]["doi:10.1/a"]["seen"]) == 4


def test_failure_keeps_previous_state_and_falls_back(tmp_path):
    state = tmp_path / "state.json"
    paper = TrackedPaper("doi:10.1/a", "AAA", "Paper A", "10.1/a")
    good = FakeSource({"doi:10.1/a": [cp("doi:old", "Old")]})
    CitationTracker(None, state, sources=[good]).run([paper])

    broken = FakeSource({}, fail={"doi:10.1/a"})
    report = CitationTracker(None, state, sources=[broken]).run([paper])
    assert report.failed == ["Paper A"]
    assert json.loads(state.read_text())["tracked"]["doi:10.1/a"]["seen"] == ["doi:old"]

    good.citations_by_id["doi:10.1/a"].append(cp("doi:new", "New"))
    report = CitationTracker(None, state, sources=[broken, good]).run([paper])
    assert [a.citing.title for a in report.kept_alerts] == ["New"]


def test_same_citing_paper_for_two_tracked_papers_is_merged(tmp_path):
    state = tmp_path / "state.json"
    a = TrackedPaper("doi:10.1/a", "AAA", "Paper A", "10.1/a")
    b = TrackedPaper("doi:10.1/b", "BBB", "Paper B", "10.1/b")
    source = FakeSource({"doi:10.1/a": [], "doi:10.1/b": []})
    CitationTracker(None, state, sources=[source]).run([a, b])
    shared = cp("doi:shared", "Cites both")
    source.citations_by_id["doi:10.1/a"].append(shared)
    source.citations_by_id["doi:10.1/b"].append(shared)
    report = CitationTracker(None, state, sources=[source]).run([a, b])
    assert len(report.alerts) == 1
    assert report.alerts[0].cited_titles == ["Paper A", "Paper B"]

    html = render_email([], report)
    assert "Cites both" in html and "Paper A" in html and "新增被引提醒" in html
