"""Async SQLAlchemy models.

Schema maps 1:1 to the plan:
  runs            one batch execution (CSV upload / pasted domains)
  leads           one company/person inside a run, with scores + draft + status
  facts           grounded facts: claim + source_url + snippet (the non-negotiable rule)
  llm_calls       every model call: tokens in/out, USD cost, purpose, lead linkage
  research_cache  (domain, date) -> facts JSON, so re-runs don't re-pay
"""
import datetime as dt
import json

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    label: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|done|failed
    total_leads: Mapped[int] = mapped_column(Integer, default=0)
    completed_leads: Mapped[int] = mapped_column(Integer, default=0)
    failed_leads: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    leads: Mapped[list["Lead"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # normalized identity (dedup happens on `domain`)
    company_name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(200), index=True)
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    contact_role: Mapped[str] = mapped_column(String(200), default="")

    # pipeline outputs
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # queued|researching|reasoning|drafting|verifying|done|failed
    angle: Mapped[str] = mapped_column(Text, default="")
    pain_hypothesis: Mapped[str] = mapped_column(Text, default="")
    icp_score: Mapped[int] = mapped_column(Integer, default=0)          # 0-100
    confidence_score: Mapped[int] = mapped_column(Integer, default=0)   # 0-100, "how much real material"
    draft: Mapped[str] = mapped_column(Text, default="")
    draft_edited: Mapped[str] = mapped_column(Text, default="")         # human edits from dashboard
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False) # segment template instead of invented hook
    verification_json: Mapped[str] = mapped_column(Text, default="[]")  # per-claim verdicts
    unsupported_claims: Mapped[int] = mapped_column(Integer, default=0)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)

    run: Mapped["Run"] = relationship(back_populates="leads")
    facts: Mapped[list["Fact"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    llm_calls: Mapped[list["LLMCall"]] = relationship(back_populates="lead", cascade="all, delete-orphan")


class Fact(Base):
    __tablename__ = "facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    claim: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)   # no source, not a usable fact
    snippet: Mapped[str] = mapped_column(Text)      # the raw text the claim came from

    lead: Mapped["Lead"] = relationship(back_populates="facts")


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model: Mapped[str] = mapped_column(String(100))
    purpose: Mapped[str] = mapped_column(String(50))  # extract|reason|score|draft|verify
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    lead: Mapped["Lead"] = relationship(back_populates="llm_calls")


class ResearchCache(Base):
    __tablename__ = "research_cache"
    __table_args__ = (UniqueConstraint("domain", "day", name="uq_domain_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(200), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    facts_json: Mapped[str] = mapped_column(Text)

    def facts(self) -> list[dict]:
        return json.loads(self.facts_json)


engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
