"""One thin door for every model call.

Design rules:
  - Every call is rate-limited (token bucket) and retried with backoff.
  - Every call logs input/output tokens x price into `llm_calls`, tied to a
    lead. Per-lead cost is just SUM(cost_usd) — that's the "$0.04/lead" story.
  - `purpose` tags let you see *where* the money goes (extract vs draft vs
    verify), which is how you find the "move extraction to a cheaper model" win.
  - MOCK_MODE returns canned-but-plausible JSON so the pipeline runs keyless.
"""
import hashlib
import json
import logging

import anthropic

from .config import settings
from .db import LLMCall, SessionLocal
from .ratelimit import RateLimitedError, TokenBucket, with_backoff

log = logging.getLogger("leadloom.llm")

llm_bucket = TokenBucket(settings.llm_requests_per_minute, name="llm")

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p = settings.price_per_mtok.get(model, {"in": 3.0, "out": 15.0})
    return tokens_in / 1e6 * p["in"] + tokens_out / 1e6 * p["out"]


async def _log_call(lead_id: int | None, model: str, purpose: str,
                    tokens_in: int, tokens_out: int) -> float:
    cost = _cost(model, tokens_in, tokens_out)
    async with SessionLocal() as session:
        session.add(LLMCall(lead_id=lead_id, model=model, purpose=purpose,
                            input_tokens=tokens_in, output_tokens=tokens_out,
                            cost_usd=cost))
        await session.commit()
    return cost


async def complete(*, model: str, system: str, user: str, purpose: str,
                   lead_id: int | None = None, max_tokens: int = 1200) -> tuple[str, float]:
    """Returns (text, cost_usd). All pipeline nodes go through here."""
    if settings.mock_mode:
        text = _mock_response(purpose, user)
        cost = await _log_call(lead_id, model, purpose, len(user) // 4, len(text) // 4)
        return text, cost

    await llm_bucket.acquire()

    async def _call():
        try:
            resp = await client().messages.create(
                model=model, max_tokens=max_tokens,
                system=system, messages=[{"role": "user", "content": user}],
            )
            return resp
        except anthropic.RateLimitError as e:
            raise RateLimitedError(str(e))
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise RateLimitedError(str(e))
            raise

    resp = await with_backoff(_call, max_retries=settings.max_retries, what=f"llm:{purpose}")
    text = "".join(b.text for b in resp.content if b.type == "text")
    cost = await _log_call(lead_id, model, purpose,
                           resp.usage.input_tokens, resp.usage.output_tokens)
    return text, cost


def parse_json(text: str) -> dict | list:
    """Models sometimes wrap JSON in fences; strip and parse defensively."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start = min([i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1], default=0)
    return json.loads(cleaned[start:])


# ---------------------------------------------------------------- mock mode
def _mock_response(purpose: str, user: str) -> str:
    seed = int(hashlib.md5(user.encode()).hexdigest()[:8], 16)
    if purpose == "extract":
        return json.dumps([
            {"claim": "Raised a $6M Series A led by Example Ventures"},
            {"claim": "Launched a self-serve tier last month"},
            {"claim": "Serves 120+ customers"},
        ][: 1 + seed % 3])
    if purpose == "reason":
        return json.dumps({
            "angle": "They announced a new self-serve tier — onboarding volume is about to spike.",
            "pain_hypothesis": "Manual lead research is eating SDR hours as inbound grows.",
        })
    if purpose == "score":
        return json.dumps({"icp_score": 55 + seed % 40, "confidence_score": 40 + seed % 55,
                           "reasoning": "Mock scoring based on fact count and role match."})
    if purpose == "draft":
        return ("Hi {name},\n\nSaw the launch of your self-serve tier last month — congrats. "
                "Teams usually hit a wall right after that: inbound triples but lead research "
                "stays manual.\n\nWe built LeadLoom to draft grounded, source-cited outreach "
                "automatically. Worth a 15-min look?\n\n— Aditya")
    if purpose == "verify":
        return json.dumps([
            {"claim": "They launched a self-serve tier last month", "supported": seed % 4 != 0,
             "supporting_fact": 0 if seed % 4 != 0 else None},
        ])
    return "{}"
