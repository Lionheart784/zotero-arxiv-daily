"""Tests for zotero_arxiv_daily.venue_rank (offline, uses small inline fixtures)."""

import pytest

from zotero_arxiv_daily.venue_rank import (
    VenueRanker,
    dblp_prefix,
    parse_ccf_html,
    parse_ccf_tsv,
    ccf_to_tsv,
    parse_sjr_csv,
)

CCF_HTML = """
<table><tr><th>序号</th><th>会议简称</th><th>会议全称</th><th>分类</th><th>类型</th><th>专业领域</th></tr>
<tr class="item" style="background-color:Wheat">
  <th scope="row">1</th><td>CHI</td>
  <td><a href="http://dblp.uni-trier.de/db/conf/chi/">ACM Conference on Human Factors in Computing Systems</a></td>
  <td>A</td><td>会议</td><td>人机交互与普适计算</td></tr>
<tr class="item"><th scope="row">2</th><td>AAAI</td>
  <td><a href="http://dblp.uni-trier.de/db/conf/aaai/">AAAI Conference on Artificial Intelligence</a></td>
  <td>A</td><td>会议</td><td>人工智能</td></tr>
<tr class="item"><th scope="row">3</th><td>ACL</td>
  <td><a href="http://dblp.uni-trier.de/db/conf/acl/">Annual Meeting of the Association for Computational Linguistics</a></td>
  <td>A</td><td>会议</td><td>人工智能</td></tr>
<tr class="item"><th scope="row">4</th><td>IUI</td>
  <td><a href="http://dblp.uni-trier.de/db/conf/iui/">ACM International Conference on Intelligent User Interfaces</a></td>
  <td>B</td><td>会议</td><td>人机交互与普适计算</td></tr>
<tr class="item"><th scope="row">5</th><td>AI</td>
  <td><a href="http://dblp.uni-trier.de/db/journals/ai/">Artificial Intelligence</a></td>
  <td>A</td><td>期刊</td><td>人工智能</td></tr>
<tr class="item"><th scope="row">6</th><td>TG</td>
  <td><a href="https://dblp.org/db/journals/tciaig/index.html">IEEE Transactions on Games</a></td>
  <td>C</td><td>期刊</td><td>人工智能</td></tr>
</table>
"""

SJR_CSV = (
    "Rank;Sourceid;Title;Type;Issn;Publisher;Open Access;Open Access Diamond;SJR;SJR Best Quartile;H index\n"
    '1;1;"AI (Switzerland)";journal;"26732688";"MDPI";Yes;No;0,900;Q1;20\n'
    '2;2;"Entertainment Computing";journal;"18759521";"Elsevier";No;No;0,700;Q2;40\n'
    '3;3;"IEEE Transactions on Games";journal;"24751502, 24751510";"IEEE";No;No;0,800;Q2;30\n'
    '4;4;"Journal of Obscure Studies";journal;"12345679";"Nobody";No;No;0,100;Q4;2\n'
)


@pytest.fixture()
def ranker():
    return VenueRanker(parse_ccf_html(CCF_HTML), parse_sjr_csv(SJR_CSV), ccf_min="C", sjr_min="Q3")


def test_parse_ccf_html_reads_rows_and_dblp_prefix():
    entries = parse_ccf_html(CCF_HTML)
    assert [e.abbr for e in entries] == ["CHI", "AAAI", "ACL", "IUI", "AI", "TG"]
    assert entries[0].rank == "A" and entries[0].kind == "会议" and entries[0].dblp == "conf/chi"
    assert entries[5].dblp == "journals/tciaig"


def test_ccf_tsv_round_trip():
    entries = parse_ccf_html(CCF_HTML)
    assert parse_ccf_tsv(ccf_to_tsv(entries)) == entries


def test_parse_sjr_csv_handles_comma_decimals_and_issns():
    entries = parse_sjr_csv(SJR_CSV)
    tog = entries[2]
    assert tog.quartile == "Q2" and tog.sjr == pytest.approx(0.8)
    assert tog.issns == ["24751502", "24751510"]


def test_dblp_prefix():
    assert dblp_prefix("conf/chi/SmithJ25") == "conf/chi"
    assert dblp_prefix("https://dblp.org/db/journals/tciaig/index.html") == "journals/tciaig"
    assert dblp_prefix(None) is None


@pytest.mark.parametrize(
    "venue, expected_ccf",
    [
        ("Proceedings of the Extended Abstracts of the CHI Conference on Human Factors in Computing Systems", "A"),
        ("Proceedings of the AAAI Conference on Artificial Intelligence", "A"),
        # AIIDE wraps the AAAI name but is a different conference
        ("Proceedings of the AAAI Conference on Artificial Intelligence and Interactive Digital Entertainment", None),
        ("Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)", "A"),
        ("Proceedings of the 31st International Conference on Intelligent User Interfaces", "B"),
        ("CHI", "A"),
    ],
)
def test_rate_proceedings_titles(ranker, venue, expected_ccf):
    assert ranker.rate(venue_names=[venue]).ccf == expected_ccf


def test_two_letter_journal_is_not_mistaken_for_ccf_abbreviation(ranker):
    rating = ranker.rate(venue_names=["AI"], issns=["2673-2688"])
    assert rating.ccf is None
    assert rating.sjr_quartile == "Q1"


def test_dblp_key_wins(ranker):
    rating = ranker.rate(venue_names=["Some odd venue string"], dblp_key="conf/acl/Foo25")
    assert rating.ccf == "A"


@pytest.mark.parametrize(
    "comment, expected",
    [
        ("Accepted at ACL 2026 main conference", "A"),
        ("12 pages. To appear in CHI '26", "A"),
        ("Under review at ACL 2026", None),
        ("CHI 2026 Workshop on Games", None),
        ("We evaluate on AI benchmarks", None),
    ],
)
def test_rate_arxiv_comments(ranker, comment, expected):
    assert ranker.rate(free_text=[comment]).ccf == expected


def test_journal_ref_matches_sjr_title(ranker):
    rating = ranker.rate(journal_refs=["Entertainment Computing 52 (2025) 100812"])
    assert rating.sjr_quartile == "Q2"


def test_passes_thresholds(ranker):
    assert ranker.passes(ranker.rate(venue_names=["IEEE Transactions on Games"]))  # CCF-C / Q2
    assert not ranker.passes(ranker.rate(venue_names=["Journal of Obscure Studies"]))  # Q4 only
    assert ranker.passes(ranker.rate(venue_names=["Unknown Workshop"]))  # unrated kept
    strict = VenueRanker(parse_ccf_html(CCF_HTML), parse_sjr_csv(SJR_CSV), keep_unrated=False)
    assert not strict.passes(strict.rate(venue_names=["Unknown Workshop"]))


def test_from_config_falls_back_to_snapshot(tmp_path):
    (tmp_path / "ccf.tsv").write_text(ccf_to_tsv(parse_ccf_html(CCF_HTML)), encoding="utf-8")
    (tmp_path / "scimagojr.csv").write_text(SJR_CSV, encoding="utf-8")
    ranker = VenueRanker.from_config({"ccf_min": "B"}, data_dir=tmp_path, offline=True)
    assert len(ranker.ccf_entries) == 6
    assert ranker.ccf_min == "B"
    assert ranker.rate(venue_names=["Entertainment Computing"]).sjr_quartile == "Q2"
