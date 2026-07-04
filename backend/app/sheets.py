"""Optional Google Sheets export (Phase 1's 'lowest-friction real users' output).

Requires: pip install gspread, a Google service-account JSON, and the target
sheet shared with the service-account email. CSV export needs none of this.
"""
import asyncio

from sqlalchemy import select

from .config import settings
from .db import Fact, Lead, SessionLocal


async def push_run_to_sheet(run_id: int) -> str:
    import gspread  # lazy import so gspread stays optional

    async with SessionLocal() as session:
        leads = (await session.execute(
            select(Lead).where(Lead.run_id == run_id))).scalars().all()
        facts = (await session.execute(
            select(Fact).where(Fact.lead_id.in_([l.id for l in leads])))).scalars().all()
    by_lead: dict[int, list[Fact]] = {}
    for f in facts:
        by_lead.setdefault(f.lead_id, []).append(f)

    rows = [["company", "domain", "contact", "role", "icp_score", "confidence",
             "unsupported_claims", "cost_usd", "draft", "sources"]]
    for l in leads:
        rows.append([l.company_name, l.domain, l.contact_name, l.contact_role,
                     l.icp_score, l.confidence_score, l.unsupported_claims, l.cost_usd,
                     l.draft_edited or l.draft,
                     "; ".join(sorted({f.source_url for f in by_lead.get(l.id, [])}))])

    def _sync() -> str:
        gc = gspread.service_account(filename=settings.google_service_account_json)
        try:
            sh = gc.open(settings.google_sheet_name)
        except gspread.SpreadsheetNotFound:
            sh = gc.create(settings.google_sheet_name)
        ws = sh.add_worksheet(title=f"run_{run_id}", rows=len(rows) + 5, cols=12)
        ws.update(values=rows, range_name="A1")
        return sh.url

    return await asyncio.to_thread(_sync)
