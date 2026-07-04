"""HTTP surface for the dashboard (and for curl in Phase 1).

POST /api/runs                 create + start a run (JSON leads or CSV text)
GET  /api/runs                 list runs
GET  /api/runs/{id}            run + its leads (dashboard polls this)
GET  /api/leads/{id}           full lead: facts, verification, per-call costs
PATCH /api/leads/{id}          edit draft / approve
GET  /api/runs/{id}/export.csv spreadsheet-ready output
POST /api/runs/{id}/export/sheets   optional gspread push
GET  /api/stats                global numbers: cost, hallucination rate, retries
"""
import csv
import io
import json
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from .config import settings
from .db import Fact, Lead, LLMCall, Run, SessionLocal, init_db
from .ratelimit import retry_stats
from .runner import create_run, execute_run

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="LeadLoom", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.on_event("startup")
async def _startup():
    await init_db()


# ------------------------------------------------------------------ schemas
class LeadIn(BaseModel):
    company_name: str = ""
    domain: str = ""
    contact_name: str = ""
    contact_role: str = ""


class RunIn(BaseModel):
    label: str = ""
    icp_description: str = ""
    leads: list[LeadIn] = []
    csv_text: str = ""  # alternative input: raw CSV with headers


class LeadPatch(BaseModel):
    draft_edited: str | None = None
    approved: bool | None = None


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.strip()))
    alias = {"company": "company_name", "company_name": "company_name", "name": "contact_name",
             "domain": "domain", "website": "domain", "url": "domain",
             "contact": "contact_name", "contact_name": "contact_name",
             "role": "contact_role", "title": "contact_role", "contact_role": "contact_role"}
    out = []
    for row in reader:
        item: dict = {}
        for k, v in row.items():
            key = alias.get((k or "").strip().lower())
            if key and v:
                item[key] = v
        if item:
            out.append(item)
    return out


# ------------------------------------------------------------------- routes
@app.post("/api/runs")
async def start_run(body: RunIn, background: BackgroundTasks):
    leads = [l.model_dump() for l in body.leads]
    if body.csv_text:
        leads += _parse_csv(body.csv_text)
    if not leads:
        raise HTTPException(400, "No leads provided. Send `leads` or `csv_text`.")
    run_id = await create_run(leads, label=body.label)
    background.add_task(execute_run, run_id, body.icp_description)
    return {"run_id": run_id}


@app.get("/api/runs")
async def list_runs():
    async with SessionLocal() as session:
        runs = (await session.execute(
            select(Run).order_by(Run.id.desc()))).scalars().all()
        return [{"id": r.id, "label": r.label, "status": r.status,
                 "created_at": r.created_at.isoformat(), "total_leads": r.total_leads,
                 "completed_leads": r.completed_leads, "failed_leads": r.failed_leads,
                 "total_cost_usd": r.total_cost_usd} for r in runs]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int):
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if not run:
            raise HTTPException(404)
        leads = (await session.execute(
            select(Lead).where(Lead.run_id == run_id).order_by(Lead.id))).scalars().all()
        done = sum(1 for l in leads if l.status in ("done", "failed"))
        return {"id": run.id, "label": run.label, "status": run.status,
                "total_leads": run.total_leads, "progress": done,
                "total_cost_usd": run.total_cost_usd,
                "leads": [{"id": l.id, "company_name": l.company_name, "domain": l.domain,
                           "contact_name": l.contact_name, "contact_role": l.contact_role,
                           "status": l.status, "icp_score": l.icp_score,
                           "confidence_score": l.confidence_score,
                           "unsupported_claims": l.unsupported_claims,
                           "fallback_used": l.fallback_used, "approved": l.approved,
                           "cache_hit": l.cache_hit, "cost_usd": l.cost_usd,
                           "error": l.error} for l in leads]}


@app.get("/api/leads/{lead_id}")
async def get_lead(lead_id: int):
    async with SessionLocal() as session:
        lead = await session.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404)
        facts = (await session.execute(
            select(Fact).where(Fact.lead_id == lead_id))).scalars().all()
        calls = (await session.execute(
            select(LLMCall).where(LLMCall.lead_id == lead_id))).scalars().all()
        return {"id": lead.id, "company_name": lead.company_name, "domain": lead.domain,
                "contact_name": lead.contact_name, "contact_role": lead.contact_role,
                "status": lead.status, "angle": lead.angle,
                "pain_hypothesis": lead.pain_hypothesis, "icp_score": lead.icp_score,
                "confidence_score": lead.confidence_score, "draft": lead.draft,
                "draft_edited": lead.draft_edited, "fallback_used": lead.fallback_used,
                "approved": lead.approved, "error": lead.error, "cost_usd": lead.cost_usd,
                "cache_hit": lead.cache_hit,
                "verification": json.loads(lead.verification_json or "[]"),
                "facts": [{"claim": f.claim, "source_url": f.source_url,
                           "snippet": f.snippet} for f in facts],
                "llm_calls": [{"purpose": c.purpose, "model": c.model,
                               "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                               "cost_usd": c.cost_usd} for c in calls]}


@app.patch("/api/leads/{lead_id}")
async def patch_lead(lead_id: int, body: LeadPatch):
    async with SessionLocal() as session:
        lead = await session.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404)
        if body.draft_edited is not None:
            lead.draft_edited = body.draft_edited
        if body.approved is not None:
            lead.approved = body.approved
        await session.commit()
        return {"ok": True}


@app.get("/api/runs/{run_id}/export.csv")
async def export_csv(run_id: int):
    async with SessionLocal() as session:
        leads = (await session.execute(
            select(Lead).where(Lead.run_id == run_id))).scalars().all()
        facts_by_lead: dict[int, list[Fact]] = {}
        for f in (await session.execute(
                select(Fact).where(Fact.lead_id.in_([l.id for l in leads])))).scalars().all():
            facts_by_lead.setdefault(f.lead_id, []).append(f)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "domain", "contact", "role", "icp_score", "confidence",
                "unsupported_claims", "fallback", "approved", "cost_usd", "draft", "sources"])
    for l in leads:
        srcs = "; ".join(sorted({f.source_url for f in facts_by_lead.get(l.id, [])}))
        w.writerow([l.company_name, l.domain, l.contact_name, l.contact_role,
                    l.icp_score, l.confidence_score, l.unsupported_claims,
                    l.fallback_used, l.approved, l.cost_usd,
                    l.draft_edited or l.draft, srcs])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition":
                                      f"attachment; filename=run_{run_id}.csv"})


@app.post("/api/runs/{run_id}/export/sheets")
async def export_sheets(run_id: int):
    if not settings.google_service_account_json:
        raise HTTPException(400, "Set GOOGLE_SERVICE_ACCOUNT_JSON to enable Sheets export. "
                                 "CSV export works without it.")
    from .sheets import push_run_to_sheet  # imported lazily; gspread optional
    url = await push_run_to_sheet(run_id)
    return {"sheet_url": url}


@app.get("/api/stats")
async def stats():
    async with SessionLocal() as session:
        total_leads = (await session.execute(select(func.count(Lead.id)))).scalar()
        done = (await session.execute(
            select(func.count(Lead.id)).where(Lead.status == "done"))).scalar()
        total_cost = (await session.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)))).scalar()
        # hallucination proxy: % of non-fallback drafts containing >=1 unsupported claim
        drafted = (await session.execute(select(func.count(Lead.id)).where(
            Lead.status == "done", Lead.fallback_used == False))).scalar()  # noqa: E712
        flagged = (await session.execute(select(func.count(Lead.id)).where(
            Lead.status == "done", Lead.fallback_used == False,             # noqa: E712
            Lead.unsupported_claims > 0))).scalar()
        cost_by_purpose = {row[0]: round(float(row[1]), 6) for row in (await session.execute(
            select(LLMCall.purpose, func.sum(LLMCall.cost_usd)).group_by(LLMCall.purpose))).all()}
    return {"total_leads": total_leads, "completed": done,
            "avg_cost_per_lead": round(float(total_cost) / done, 6) if done else 0,
            "total_cost_usd": round(float(total_cost), 6),
            "cost_by_purpose": cost_by_purpose,
            "drafts_with_unsupported_claims_pct":
                round(100 * flagged / drafted, 1) if drafted else 0,
            "rate_limit_hits": retry_stats.rate_limit_hits,
            "retries": retry_stats.retries}
