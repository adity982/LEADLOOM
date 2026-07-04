# LeadLoom — grounded lead-research & outreach agent

A per-lead LangGraph state machine that researches a company from public sources, extracts **facts that always carry a source URL**, drafts outreach only from those facts, then runs a second-model verification pass that flags any unsupported claim. Every LLM call logs tokens × price, so cost-per-lead is a real number, not a guess.

```
research ──► reason ──► score ──► draft ──► verify ──► persist
  │                                 │
  └─ (domain,date) cache            └─ no grounded hook? → segment template,
     + dedup on domain                 never an invented one
```

## Quickstart (zero keys needed)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # MOCK_MODE=1 by default

# Phase 1 — CLI batch, no UI:
python -m scripts.run_batch --domains stripe.com,linear.app,posthog.com --out output.csv

# API + dashboard:
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, talks to :8000
```

Paste a list of domains (or upload a CSV with `company,domain,contact,role` headers), hit **Start run**, watch leads move through statuses, click a finished lead to see the draft, the claim-by-claim verification, the facts with their sources, and the cost breakdown.

**Going live:** set `MOCK_MODE=0`, add `ANTHROPIC_API_KEY` and `TAVILY_API_KEY` (free tier at tavily.com) to `.env`. Nothing else changes.

## Phase → file map

| Phase | What | Where |
|---|---|---|
| 1 | Async batch, rate limiting, cost logging, CSV/Sheets out | `scripts/run_batch.py`, `app/ratelimit.py`, `app/llm.py`, `app/sheets.py` |
| 2 | LangGraph state machine, dedup+cache, scoring, verification | `app/pipeline/graph.py`, `app/research.py`, `app/runner.py` |
| 3 | Review dashboard: upload → progress → review/edit → export | `app/main.py` (API), `frontend/src/App.jsx` |
| 4 | Eval harness: hand-labeled before/after hallucination metric | `scripts/eval_harness.py` |

## The eval workflow (Phase 4 — the headline number)

```bash
# 1. run a batch, then export completed drafts for labeling
python -m scripts.eval_harness export --run 3 --out eval_set.csv

# 2. open eval_set.csv, fill the `label` column per row:
#    clean         = every factual claim in the draft is in the fact list
#    hallucinated  = at least one claim isn't

# 3. get the metric + how well the automatic verifier agrees with you
python -m scripts.eval_harness report eval_set.csv
```

To get the **before/after pair**, run a baseline first: set `DRAFT_SYSTEM`'s fact-list rule aside (or feed the draft node raw scraped text instead of facts) on a copy, label ~30–50 drafts from each variant, and quote the two numbers. That "34% → 4%" sentence is the credibility core of the whole project.

## The scale instrumentation

- `GET /api/stats` reports `rate_limit_hits`, `retries`, `avg_cost_per_lead`, `cost_by_purpose` (this is how you find that extraction on the cheap model is where the savings are), and `drafts_with_unsupported_claims_pct`.
- `TokenBucket` caps requests/min per upstream; the semaphore in `runner.py` caps lead parallelism; `with_backoff` does exponential backoff + jitter on 429/5xx. Crank `MAX_CONCURRENT_LEADS` to 50 against real APIs and watch the counters — that's the "what broke" story, with numbers.
- Research is cached on `(domain, date)` and leads dedup on normalized domain, so re-runs are near-free (cache hits show as ⟳ in the dashboard).

## Deploy

**Backend → Railway:** new service from this repo, root `backend/`, start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Add a Postgres plugin, set `DATABASE_URL=postgresql+asyncpg://…` (uncomment `asyncpg` in requirements), plus `MOCK_MODE=0` and the two API keys.

**Frontend → Vercel:** import repo, root `frontend/`, framework Vite. Set env `VITE_API_URL=https://<your-railway-app>.up.railway.app`. Same flow as your Task Tracker deploy — and CORS is already open on the backend (`allow_origins=["*"]`; tighten to your Vercel URL before sharing widely).

**Google Sheets (optional):** `pip install gspread`, create a service account, set `GOOGLE_SERVICE_ACCOUNT_JSON` to the key file path, share the sheet with the service-account email, then `POST /api/runs/{id}/export/sheets`.

