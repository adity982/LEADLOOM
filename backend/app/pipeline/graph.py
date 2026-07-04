"""The per-lead state machine (LangGraph).

    research -> reason -> score -> draft -> verify -> persist
                   \\__ if no facts: fallback draft (segment template) __/

Why a graph and not a loose agent: explicit nodes give you retries,
node-level logging, and a checkpointable state dict — the observability
surface where the "what broke at scale" story lives.

Each node reads/writes LeadState and nothing else. All persistence happens
in `persist`, so a crash mid-lead leaves the DB consistent (status=failed).
"""
import json
import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy import select

from ..config import settings
from ..db import Fact, Lead, SessionLocal
from ..llm import complete, parse_json
from ..research import research_lead

log = logging.getLogger("leadloom.pipeline")


class LeadState(TypedDict, total=False):
    lead_id: int
    company_name: str
    domain: str
    contact_name: str
    contact_role: str
    icp_description: str          # what a good-fit lead looks like (from the run)
    facts: list[dict]             # {"claim", "source_url", "snippet"}
    cache_hit: bool
    angle: str
    pain_hypothesis: str
    icp_score: int
    confidence_score: int
    draft: str
    fallback_used: bool
    verification: list[dict]      # {"claim", "supported", "supporting_fact"}
    unsupported_claims: int
    error: str


async def _set_status(lead_id: int, status: str) -> None:
    async with SessionLocal() as session:
        lead = await session.get(Lead, lead_id)
        if lead:
            lead.status = status
            await session.commit()


# ------------------------------------------------------------------- nodes
async def node_research(state: LeadState) -> LeadState:
    await _set_status(state["lead_id"], "researching")
    facts, cache_hit = await research_lead(
        lead_id=state["lead_id"], company_name=state["company_name"],
        domain=state["domain"], contact_name=state["contact_name"],
        contact_role=state["contact_role"])
    log.info("lead %d: %d facts (%s)", state["lead_id"], len(facts),
             "cache" if cache_hit else "fresh")
    return {"facts": facts, "cache_hit": cache_hit}


REASON_SYSTEM = """You are a sales-research analyst. You receive ONLY a list of
sourced facts about a company. Using nothing but those facts, pick the single
best personalization hook (angle) and a hypothesis about the company's likely
pain relevant to the product being sold.
Return ONLY JSON: {"angle": "...", "pain_hypothesis": "..."}.
If the facts are too thin to support a specific angle, return {"angle": "", "pain_hypothesis": ""}."""


async def node_reason(state: LeadState) -> LeadState:
    if not state.get("facts"):
        return {"angle": "", "pain_hypothesis": ""}
    await _set_status(state["lead_id"], "reasoning")
    facts_txt = "\n".join(f"[{i}] {f['claim']} (source: {f['source_url']})"
                          for i, f in enumerate(state["facts"]))
    raw, _ = await complete(model=settings.extract_model, purpose="reason",
                            lead_id=state["lead_id"], system=REASON_SYSTEM,
                            user=f"Product being sold: AI-grounded lead-research assistant.\n"
                                 f"Contact: {state['contact_name']} ({state['contact_role']})\n\n"
                                 f"Facts:\n{facts_txt}", max_tokens=400)
    try:
        data = parse_json(raw)
        return {"angle": data.get("angle", ""), "pain_hypothesis": data.get("pain_hypothesis", "")}
    except Exception:
        return {"angle": "", "pain_hypothesis": ""}


SCORE_SYSTEM = """Score this lead. Return ONLY JSON:
{"icp_score": 0-100, "confidence_score": 0-100, "reasoning": "..."}
icp_score: fit against the ideal customer profile provided.
confidence_score: how much real, specific research material was found
(many specific recent facts = high; generic marketing copy = low)."""


async def node_score(state: LeadState) -> LeadState:
    await _set_status(state["lead_id"], "scoring")
    facts_txt = "\n".join(f"- {f['claim']}" for f in state.get("facts", [])) or "(no facts found)"
    icp = state.get("icp_description") or (
        "B2B software companies, 10-500 employees, with outbound sales or "
        "recruiting motions, where the contact influences sales/growth tooling.")
    raw, _ = await complete(model=settings.extract_model, purpose="score",
                            lead_id=state["lead_id"], system=SCORE_SYSTEM,
                            user=f"ICP: {icp}\nCompany: {state['company_name']}\n"
                                 f"Contact: {state['contact_name']} ({state['contact_role']})\n"
                                 f"Facts found ({len(state.get('facts', []))}):\n{facts_txt}",
                            max_tokens=300)
    try:
        data = parse_json(raw)
        return {"icp_score": int(data.get("icp_score", 0)),
                "confidence_score": int(data.get("confidence_score", 0))}
    except Exception:
        return {"icp_score": 0, "confidence_score": 0}


DRAFT_SYSTEM = """You write short, honest cold outreach (under 120 words).
HARD RULES:
- Every factual statement about the company MUST come from the numbered fact
  list. Do not add, embellish, or infer facts.
- Reference the angle naturally in the opening line.
- No hype words, no fake familiarity, one clear ask.
Return only the email body (no subject, no JSON)."""

FALLBACK_TEMPLATE = """Hi {name},

I work with {segment} teams on cutting the manual research time behind outbound.
If prospect research is eating hours on your side, I built a small tool that
drafts source-cited outreach automatically — happy to show you in 15 minutes.

— Aditya"""


async def node_draft(state: LeadState) -> LeadState:
    await _set_status(state["lead_id"], "drafting")
    # Fallback rule: no real hook -> segment template, never an invented one.
    if not state.get("facts") or not state.get("angle"):
        name = state["contact_name"].split(" ")[0] if state["contact_name"] else "there"
        return {"draft": FALLBACK_TEMPLATE.format(name=name, segment="B2B sales"),
                "fallback_used": True, "verification": [], "unsupported_claims": 0}

    facts_txt = "\n".join(f"[{i}] {f['claim']}" for i, f in enumerate(state["facts"]))
    draft, _ = await complete(model=settings.draft_model, purpose="draft",
                              lead_id=state["lead_id"], system=DRAFT_SYSTEM,
                              user=f"Contact: {state['contact_name']} ({state['contact_role']}) "
                                   f"at {state['company_name']}\n"
                                   f"Angle: {state['angle']}\n"
                                   f"Pain hypothesis: {state['pain_hypothesis']}\n\n"
                                   f"Fact list (the ONLY permitted facts):\n{facts_txt}",
                              max_tokens=400)
    return {"draft": draft.strip(), "fallback_used": False}


VERIFY_SYSTEM = """You are a fact-checker. Given an email draft and a numbered
fact list, extract every factual claim about the company from the draft and
check whether it is supported by a fact in the list.
Return ONLY a JSON array:
[{"claim": "...", "supported": true/false, "supporting_fact": <index or null>}]
Generic statements about the sender or the product are not claims. Be strict:
a claim is supported only if a listed fact actually states it."""


async def node_verify(state: LeadState) -> LeadState:
    if state.get("fallback_used"):
        return {}
    await _set_status(state["lead_id"], "verifying")
    facts_txt = "\n".join(f"[{i}] {f['claim']}" for i, f in enumerate(state["facts"]))
    raw, _ = await complete(model=settings.extract_model, purpose="verify",
                            lead_id=state["lead_id"], system=VERIFY_SYSTEM,
                            user=f"Fact list:\n{facts_txt}\n\nDraft:\n{state['draft']}",
                            max_tokens=600)
    try:
        verdicts = parse_json(raw)
        unsupported = sum(1 for v in verdicts if not v.get("supported"))
    except Exception:
        verdicts, unsupported = [], 0
    return {"verification": verdicts, "unsupported_claims": unsupported}


async def node_persist(state: LeadState) -> LeadState:
    async with SessionLocal() as session:
        lead = await session.get(Lead, state["lead_id"])
        if not lead:
            return {}
        lead.status = "done"
        lead.angle = state.get("angle", "")
        lead.pain_hypothesis = state.get("pain_hypothesis", "")
        lead.icp_score = state.get("icp_score", 0)
        lead.confidence_score = state.get("confidence_score", 0)
        lead.draft = state.get("draft", "")
        lead.fallback_used = state.get("fallback_used", False)
        lead.verification_json = json.dumps(state.get("verification", []))
        lead.unsupported_claims = state.get("unsupported_claims", 0)
        lead.cache_hit = state.get("cache_hit", False)
        for f in state.get("facts", []):
            session.add(Fact(lead_id=lead.id, claim=f["claim"],
                             source_url=f["source_url"], snippet=f["snippet"]))
        # per-lead cost = sum of its logged LLM calls
        from ..db import LLMCall
        calls = (await session.execute(
            select(LLMCall).where(LLMCall.lead_id == lead.id))).scalars().all()
        lead.cost_usd = round(sum(c.cost_usd for c in calls), 6)
        await session.commit()
    return {}


# -------------------------------------------------------------------- graph
def build_graph():
    g = StateGraph(LeadState)
    g.add_node("research", node_research)
    g.add_node("reason", node_reason)
    g.add_node("score", node_score)
    g.add_node("draft", node_draft)
    g.add_node("verify", node_verify)
    g.add_node("persist", node_persist)

    g.set_entry_point("research")
    g.add_edge("research", "reason")
    g.add_edge("reason", "score")
    g.add_edge("score", "draft")
    g.add_edge("draft", "verify")
    g.add_edge("verify", "persist")
    g.add_edge("persist", END)
    return g.compile()


lead_graph = build_graph()


async def process_lead(state: LeadState) -> None:
    """Run one lead through the graph; mark failed on any unhandled error."""
    try:
        await lead_graph.ainvoke(state)
    except Exception as e:
        log.exception("lead %d failed", state["lead_id"])
        async with SessionLocal() as session:
            lead = await session.get(Lead, state["lead_id"])
            if lead:
                lead.status = "failed"
                lead.error = str(e)[:1000]
                await session.commit()
        raise
