"""Central configuration. Everything comes from environment variables.

MOCK_MODE=1 replaces the search API and LLM calls with deterministic fakes so
the entire pipeline (and the dashboard) can be run end-to-end with zero keys.
Flip it off and set ANTHROPIC_API_KEY + TAVILY_API_KEY for real runs.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- switches ---
    mock_mode: bool = False

    # --- keys ---
    anthropic_api_key: str = ""
    tavily_api_key: str = ""

    # --- models: cheap one for extraction/verification, strong one for drafts ---
    extract_model: str = "claude-haiku-4-5-20251001"
    draft_model: str = "claude-sonnet-4-6"

    # --- pricing per million tokens (USD), used for per-lead cost accounting ---
    # Update these if Anthropic pricing changes; they only affect reporting.
    price_per_mtok: dict = {
        "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00},
        "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    }

    # --- storage ---
    # SQLite by default so `uvicorn` works out of the box.
    # On Railway set DATABASE_URL=postgresql+asyncpg://... (add `asyncpg` to reqs).
    database_url: str = "sqlite+aiosqlite:///./leadloom.db"

    # --- concurrency / rate limiting ---
    max_concurrent_leads: int = 5          # leads processed in parallel
    llm_requests_per_minute: int = 50      # token bucket for LLM calls
    search_requests_per_minute: int = 60   # token bucket for search calls
    max_retries: int = 4                   # exponential backoff attempts on 429/5xx

    # --- research ---
    research_cache_ttl_days: int = 1       # cache key is (domain, date)
    max_pages_scraped_per_lead: int = 3
    max_facts_per_lead: int = 12

    # --- optional Google Sheets export ---
    google_service_account_json: str = ""  # path to service-account file
    google_sheet_name: str = "LeadLoom Output"

    class Config:
        env_file = ".env"


settings = Settings()
