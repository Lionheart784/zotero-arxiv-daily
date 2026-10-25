from __future__ import annotations
from html import escape
from typing import TYPE_CHECKING
from .protocol import Paper
import math

if TYPE_CHECKING:
    from .citation_tracker import CitationReport


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em; /* 调整星星大小 */
      line-height: 1; /* 确保垂直对齐 */
      display: inline-flex;
      align-items: center; /* 保持对齐 */
    }
    .half-star {
      display: inline-block;
      width: 0.5em; /* 半颗星的宽度 */
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>

<div>
    __CONTENT__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No Papers Today. Take a Rest!
    </td>
  </tr>
  </table>
  """
  return block_template

def get_block_html(title:str, authors:str, rate:str, tldr:str, pdf_url:str, affiliations:str=None, venue:str=None):
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
            {authors}
            <br>
            <i>{affiliations}</i>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>Relevance:</strong> {rate}{venue_row}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>TLDR:</strong> {tldr}
        </td>
    </tr>

    <tr>
        <td style="padding: 8px 0;">
            <a href="{pdf_url}" style="display: inline-block; text-decoration: none; font-size: 14px; font-weight: bold; color: #fff; background-color: #d9534f; padding: 8px 16px; border-radius: 4px;">PDF</a>
        </td>
    </tr>
</table>
"""
    venue_row = f"<br><strong>Venue:</strong> {escape(venue)}" if venue else ""
    return block_template.format(title=title, authors=authors,rate=rate, tldr=tldr, pdf_url=pdf_url, affiliations=affiliations, venue_row=venue_row)


def _format_authors(author_list: list[str]) -> str:
    if len(author_list) <= 5:
        return ', '.join(author_list)
    return ', '.join(author_list[:3] + ['...'] + author_list[-2:])


def get_citation_section_html(report: "CitationReport") -> str:
    rows = []
    for a in report.kept_alerts:
        c = a.citing
        title = escape(c.title)
        if c.url:
            title = f'<a href="{escape(c.url)}" style="color:#1a5fb4;">{title}</a>'
        meta = ", ".join(x for x in [_format_authors(c.authors), str(c.year) if c.year else ""] if x)
        cited = "；".join(escape(t) for t in a.cited_titles)
        rows.append(f"""
    <tr><td style="padding: 10px 0; border-top: 1px solid #e5e5e5; font-size: 14px; color: #333;">
        <div style="font-size: 16px; font-weight: bold;">{title}</div>
        <div style="color: #666; padding: 4px 0;">{escape(meta)}</div>
        <div><strong>Venue:</strong> {escape(a.rating.label())}</div>
        <div><strong>引用了:</strong> {cited}</div>
    </td></tr>""")
    notes = []
    if report.dropped_alerts:
        notes.append(f"另有 {len(report.dropped_alerts)} 篇新增施引文献因期刊/会议级别低于门槛被过滤。")
    if report.baselined:
        total = sum(report.baselined.values())
        notes.append(f"首次追踪 {len(report.baselined)} 篇文献，已记录现有的 {total} 篇施引文献作为基线，之后只提醒新增的。")
    if report.failed:
        notes.append("今天没能查到这些文献的引用（明天会重试）：" + "；".join(escape(t) for t in report.failed))
    note_html = "".join(f'<div style="font-size: 13px; color: #777; padding-top: 6px;">{n}</div>' for n in notes)
    body = "".join(rows) if rows else '<tr><td style="font-size: 14px; color: #666; padding: 8px 0;">今天没有新增引用。</td></tr>'
    return f"""
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f4f8fb;">
    <tr><td style="font-size: 20px; font-weight: bold; color: #333; padding-bottom: 6px;">
        新增被引提醒（{len(report.kept_alerts)} 篇）
    </td></tr>
    {body}
    <tr><td>{note_html}</td></tr>
  </table>
  """

def get_stars(score:float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    else:
        interval = (high-low) / 10
        star_num = math.ceil((score-low) / interval)
        full_star_num = int(star_num/2)
        half_star_num = star_num - full_star_num * 2
        return '<div class="star-wrapper">'+full_star * full_star_num + half_star * half_star_num + '</div>'


def render_email(papers:list[Paper], citation_report: "CitationReport | None" = None) -> str:
    parts = []
    if citation_report is not None and (citation_report.has_content or citation_report.baselined or citation_report.failed):
        parts.append(get_citation_section_html(citation_report))

    if len(papers) == 0 and not parts:
        return framework.replace('__CONTENT__', get_empty_html())

    for p in papers:
        #rate = get_stars(p.score)
        rate = round(p.score, 1) if p.score is not None else 'Unknown'
        authors = _format_authors([a for a in p.authors])
        if p.affiliations is not None:
            affiliations = p.affiliations[:5]
            affiliations = ', '.join(affiliations)
            if len(p.affiliations) > 5:
                affiliations += ', ...'
        else:
            affiliations = 'Unknown Affiliation'
        parts.append(get_block_html(p.title, authors, rate, p.tldr, p.pdf_url, affiliations, venue=p.venue_label))

    content = '<br>' + '</br><br>'.join(parts) + '</br>'
    return framework.replace('__CONTENT__', content)
