from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper
import random
from datetime import datetime
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from openai import OpenAI
from tqdm import tqdm
from .venue_rank import VenueRanker
from .citation_tracker import CitationTracker, CitationReport, tracked_from_zotero

SKIP_ITEM_TYPES = {"attachment", "note", "annotation"}


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config:DictConfig):
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)
        self.venue_cfg = config.get("venue_filter") or {}
        self.citation_cfg = config.get("citation_alert") or {}
        self._ranker = None

    @property
    def ranker(self) -> VenueRanker:
        if self._ranker is None:
            self._ranker = VenueRanker.from_config(self.venue_cfg)
        return self._ranker

    def filter_by_venue(self, papers):
        """Label every paper with its CCF / SJR rating and drop the ones below threshold."""
        kept, dropped = [], []
        for p in papers:
            rating = self.ranker.rate(
                venue_names=[],
                free_text=[p.comment] if p.comment else [],
                journal_refs=[p.journal_ref] if p.journal_ref else [],
            )
            p.venue_label = rating.label()
            (kept if self.ranker.passes(rating) else dropped).append(p)
        for p in dropped:
            logger.info(f"Dropped by venue filter: {p.title} [{p.venue_label}]")
        logger.info(f"Venue filter kept {len(kept)} of {len(papers)} papers")
        return kept

    def fetch_tracked_items(self) -> list[dict]:
        """Zotero items (any type) inside the collections listed in citation_alert.include_path."""
        patterns = normalize_path_patterns(self.citation_cfg.get("include_path"), "citation_alert.include_path")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = {c['key']: c for c in zot.everything(zot.collections())}

        def path_of(col_key: str) -> str:
            col = collections[col_key]['data']
            if col.get('parentCollection'):
                return path_of(col['parentCollection']) + '/' + col['name']
            return col['name']

        items = []
        for it in zot.everything(zot.top()):
            if it['data'].get('itemType') in SKIP_ITEM_TYPES:
                continue
            paths = [path_of(k) for k in it['data'].get('collections', []) if k in collections]
            if patterns is None or any(glob_match(p, pat) for p in paths for pat in patterns):
                items.append(it)
        logger.info(f"Tracking citations of {len(items)} zotero papers")
        return items

    def run_citation_alerts(self) -> CitationReport | None:
        if not self.citation_cfg.get("enabled", False):
            return None
        try:
            tracked = tracked_from_zotero(self.fetch_tracked_items())
            self.citation_tracker = CitationTracker(
                self.ranker if self.citation_cfg.get("venue_filter", True) else None,
                alert_on_first_run=self.citation_cfg.get("alert_on_first_run", False),
            )
            # The state is written only after the email is sent, so a failed send
            # does not swallow today's new citations.
            return self.citation_tracker.run(tracked, save=False)
        except Exception as exc:
            logger.exception(f"Citation tracking failed: {exc}")
            return None

    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = zot.everything(zot.collections())
        collections = {c['key']:c for c in collections}
        corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
        corpus = [c for c in corpus if c['data']['abstractNote'] != '']
        def get_collection_path(col_key:str) -> str:
            if p := collections[col_key]['data']['parentCollection']:
                return get_collection_path(p) + '/' + collections[col_key]['data']['name']
            else:
                return collections[col_key]['data']['name']
        for c in corpus:
            paths = [get_collection_path(col) for col in c['data']['collections']]
            c['paths'] = paths
        logger.info(f"Fetched {len(corpus)} zotero papers")
        return [CorpusPaper(
            title=c['data']['title'],
            abstract=c['data']['abstractNote'],
            added_date=datetime.strptime(c['data']['dateAdded'], '%Y-%m-%dT%H:%M:%SZ'),
            paths=c['paths']
        ) for c in corpus]
    
    def filter_corpus(self, corpus:list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            logger.info(f"Selecting zotero papers matching include_path: {self.include_path_patterns}")
            corpus = [
                c for c in corpus
                if any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.include_path_patterns
                )
            ]
        if self.ignore_path_patterns:
            logger.info(f"Excluding zotero papers matching ignore_path: {self.ignore_path_patterns}")
            corpus = [
                c for c in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.ignore_path_patterns
                )
            ]
        if self.include_path_patterns or self.ignore_path_patterns:
            samples = random.sample(corpus, min(5, len(corpus)))
            samples = '\n'.join([c.title + ' - ' + '\n'.join(c.paths) for c in samples])
            logger.info(f"Selected {len(corpus)} zotero papers:\n{samples}\n...")
        return corpus

    
    def run(self):
        corpus = self.fetch_zotero_corpus()
        corpus = self.filter_corpus(corpus)
        reranked_papers = []
        if len(corpus) == 0:
            logger.error(f"No zotero papers found. Please check your zotero settings:\n{self.config.zotero}")
        else:
            all_papers = []
            for source, retriever in self.retrievers.items():
                logger.info(f"Retrieving {source} papers...")
                papers = retriever.retrieve_papers()
                if len(papers) == 0:
                    logger.info(f"No {source} papers found")
                    continue
                logger.info(f"Retrieved {len(papers)} {source} papers")
                all_papers.extend(papers)
            logger.info(f"Total {len(all_papers)} papers retrieved from all sources")
            if all_papers and self.venue_cfg.get("enabled", False):
                all_papers = self.filter_by_venue(all_papers)
            if len(all_papers) > 0:
                logger.info("Reranking papers...")
                reranked_papers = self.reranker.rerank(all_papers, corpus)
                reranked_papers = reranked_papers[:self.config.executor.max_paper_num]
                logger.info("Generating TLDR and affiliations...")
                for p in tqdm(reranked_papers):
                    p.generate_tldr(self.openai_client, self.config.llm)
                    p.generate_affiliations(self.openai_client, self.config.llm)

        citation_report = self.run_citation_alerts()
        has_citations = citation_report is not None and citation_report.has_content
        if not reranked_papers and not has_citations and not self.config.executor.send_empty:
            logger.info("No new papers or citations found. No email will be sent.")
        else:
            logger.info("Sending email...")
            email_content = render_email(reranked_papers, citation_report)
            send_email(self.config, email_content)
            logger.info("Email sent successfully")
        if citation_report is not None:
            self.citation_tracker.commit(citation_report)
