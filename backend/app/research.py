"""Grounded research: the non-negotiable rule lives here.

Every fact leaving this module is a dict:
    {"claim": ..., "source_url": ..., "snippet": ...}
No source URL -> the fact is dropped. The draft node never sees raw web text,
only this fact list, which is what makes the verification pass meaningful.

Also here: domain normalization (dedup), and the (domain, date) research cache
so re-runs don't re-pay for search + scraping + extraction.
"""
import asyncio
import datetime as dt
import json
import logging
import re

import httpx
import trafilatura
from sqlalchemy import select

from .config import settings
from .db import ResearchCache, SessionLocal
from .llm import complete, parse_json
from .ratelimit import RateLimitedError, TokenBucket, with_backoff

log = logging.getLogger("leadloom.research")

search_bucket = TokenBucket(settings.search_requests_per_minute, name="search")


# ------------------------------------------------------------- normalization
def normalize_domain(raw: str) -> str:
    """'https://www.Acme.com/about' -> 'acme.com'; 'Acme Inc' -> '' (no domain)."""
    raw = raw.strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^www\.", "", raw)
    raw = raw.split("/")[0].split("?")[0]
    return raw if "." in raw else ""


def normalize_company(name: str) -> str:
    """'Acme, Inc.' -> 'acme' — collapses vendor-name variants for dedup."""
    n = name.strip().lower()
    n = re.sub(r"[,.]", "", n)
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|gmbh|pvt|limited)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


# ------------------------------------------------------------------- search
async def tavily_search(query: str, max_results: int = 4) -> list[dict]:
    """Returns [{'title', 'url', 'content'}]. Rate-limited + retried."""
    if settings.mock_mode:
        return _mock_search(query)

    await search_bucket.acquire()

    async def _call():
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post("https://api.tavily.com/search", json={
                "api_key": settings.tavily_api_key, "query": query,
                "max_results": max_results, "search_depth": "basic",
            })
            if r.status_code == 429 or r.status_code >= 500:
                raise RateLimitedError(f"tavily {r.status_code}")
            r.raise_for_status()
            return r.json().get("results", [])

    try:
        return await with_backoff(_call, max_retries=settings.max_retries, what="tavily")
    except Exception as e:
        log.warning("search failed for %r: %s", query, e)
        return []


# ------------------------------------------------------------------ scraping
async def scrape(url: str) -> str:
    """Fetch a page and extract readable text. Empty string on any failure."""
    if settings.mock_mode:
        return _mock_page(url)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "LeadLoomBot/0.1 (research assistant)"}) as http:
            r = await http.get(url)
            r.raise_for_status()
            text = trafilatura.extract(r.text) or ""
            return text[:8000]
    except Exception as e:
        log.debug("scrape failed %s: %s", url, e)
        return ""


# --------------------------------------------------------------------- cache
async def cache_get(domain: str) -> list[dict] | None:
    day = dt.date.today().isoformat()
    async with SessionLocal() as session:
        row = (await session.execute(
            select(ResearchCache).where(ResearchCache.domain == domain,
                                        ResearchCache.day == day))).scalar_one_or_none()
        return row.facts() if row else None


async def cache_put(domain: str, facts: list[dict]) -> None:
    day = dt.date.today().isoformat()
    async with SessionLocal() as session:
        row = (await session.execute(
            select(ResearchCache).where(ResearchCache.domain == domain,
                                        ResearchCache.day == day))).scalar_one_or_none()
        if row:
            row.facts_json = json.dumps(facts)
        else:
            session.add(ResearchCache(domain=domain, day=day, facts_json=json.dumps(facts)))
        await session.commit()


# ------------------------------------------------------- the research routine
EXTRACT_SYSTEM = """You extract factual claims about a company from raw source text.
Return ONLY a JSON array. Each item: {"claim": "<one specific, verifiable fact>"}.
Rules: only facts explicitly stated in the text; no inference, no praise, no vague
statements; max 5 facts; prefer recent/specific facts (launches, funding, hires,
metrics, named products)."""


async def research_lead(*, lead_id: int, company_name: str, domain: str,
                        contact_name: str, contact_role: str) -> tuple[list[dict], bool]:
    """Returns (facts, cache_hit). Facts always carry source_url + snippet."""
    if domain:
        cached = await cache_get(domain)
        if cached is not None:
            log.info("cache hit for %s", domain)
            return cached, True

    sources: list[dict] = []  # {'url', 'text'}

    # 1. company site: homepage + about/team pages
    if domain:
        pages = [f"https://{domain}", f"https://{domain}/about"]
        texts = await asyncio.gather(*(scrape(u) for u in pages[:settings.max_pages_scraped_per_lead]))
        sources += [{"url": u, "text": t} for u, t in zip(pages, texts) if t]

    # 2. recent news + funding via search
    for query in (f"{company_name} news", f"{company_name} funding announcement"):
        for hit in await tavily_search(query, max_results=3):
            if hit.get("content"):
                sources.append({"url": hit["url"], "text": hit["content"][:3000]})

    facts: list[dict] = []
    for src in sources:
        if len(facts) >= settings.max_facts_per_lead:
            break
        try:
            raw, _ = await complete(
                model=settings.extract_model, purpose="extract", lead_id=lead_id,
                system=EXTRACT_SYSTEM,
                user=f"Company: {company_name}\nContact: {contact_name} ({contact_role})\n"
                     f"Source URL: {src['url']}\n\nSource text:\n{src['text'][:4000]}",
                max_tokens=600)
            for item in parse_json(raw):
                claim = (item.get("claim") or "").strip()
                if claim:
                    # THE rule: a fact without a source is not a fact.
                    facts.append({"claim": claim, "source_url": src["url"],
                                  "snippet": src["text"][:400]})
        except Exception as e:
            log.warning("extraction failed for %s: %s", src["url"], e)

    facts = facts[:settings.max_facts_per_lead]
    if domain and facts:
        await cache_put(domain, facts)
    return facts, False


# ----------------------------------------------------------------- mock data
def _mock_search(query: str) -> list[dict]:
    name = query.replace(" news", "").replace(" funding announcement", "")
    return [{"title": f"{name} raises Series A", "url": f"https://news.example.com/{name.replace(' ', '-')}-series-a",
             "content": f"{name} announced a $6M Series A led by Example Ventures to expand its "
                        f"engineering team. The company also launched a self-serve tier last month."}]


def _mock_page(url: str) -> str:
    return (f"About us — we build workflow software for revenue teams. Founded in 2023, "
            f"headquartered in Bengaluru. Our platform (see {url}) serves 120+ customers "
            f"and recently shipped an AI assistant for pipeline reviews.")
