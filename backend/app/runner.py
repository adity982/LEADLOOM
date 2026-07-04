"""Batch runner: takes a run's leads and pushes each through the graph,
with a semaphore capping parallelism (the second half of the rate-limit story
— TokenBucket caps request *rate*, this caps lead *parallelism*)."""
import asyncio
import logging

from sqlalchemy import func, select

from .config import settings
from .db import Lead, LLMCall, Run, SessionLocal
from .pipeline.graph import process_lead
from .research import normalize_company, normalize_domain

log = logging.getLogger("leadloom.runner")


async def create_run(leads_in: list[dict], label: str = "") -> int:
    """Dedup on normalized domain (fall back to normalized company name),
    insert run + leads, return run id."""
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in leads_in:
        domain = normalize_domain(item.get("domain", ""))
        company = (item.get("company_name") or domain.split(".")[0] or "unknown").strip()
        key = domain or normalize_company(company)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append({"company_name": company, "domain": domain,
                        "contact_name": (item.get("contact_name") or "").strip(),
                        "contact_role": (item.get("contact_role") or "").strip()})

    async with SessionLocal() as session:
        run = Run(label=label, total_leads=len(cleaned))
        session.add(run)
        await session.flush()
        for c in cleaned:
            session.add(Lead(run_id=run.id, **c))
        await session.commit()
        return run.id


async def execute_run(run_id: int, icp_description: str = "") -> None:
    async with SessionLocal() as session:
        leads = (await session.execute(
            select(Lead).where(Lead.run_id == run_id))).scalars().all()
        payloads = [{"lead_id": l.id, "company_name": l.company_name, "domain": l.domain,
                     "contact_name": l.contact_name, "contact_role": l.contact_role,
                     "icp_description": icp_description} for l in leads]

    sem = asyncio.Semaphore(settings.max_concurrent_leads)

    async def _one(payload: dict) -> bool:
        async with sem:
            try:
                await process_lead(payload)
                return True
            except Exception:
                return False

    results = await asyncio.gather(*(_one(p) for p in payloads))

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        run.completed_leads = sum(results)
        run.failed_leads = len(results) - sum(results)
        run.status = "done"
        lead_ids = [p["lead_id"] for p in payloads]
        total = (await session.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
            .where(LLMCall.lead_id.in_(lead_ids)))).scalar()
        run.total_cost_usd = round(float(total), 6)
        await session.commit()
    log.info("run %d done: %d ok, %d failed, $%.4f",
             run_id, sum(results), len(results) - sum(results), total)
