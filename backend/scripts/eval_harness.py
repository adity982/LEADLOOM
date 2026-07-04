"""Phase 4: the eval harness — the whole pitch.

Workflow:
  1. `export` — dump completed drafts + their fact lists to eval_set.csv.
     Hand-label each row: for every factual claim in the draft, is it true
     AND present in the fact list? Fill `label` with `clean` or `hallucinated`.
  2. `report` — read the labeled CSV and print:
       - human-labeled % of drafts containing an unsupported claim
       - the automatic verifier's number for the same drafts
       - verifier agreement with your labels (precision/recall of the checker)
  Run this once on a baseline run (verification/grounding off or an early
  version) and once on the current pipeline; the before/after pair
  ("34% -> 4%") is the headline metric.

Usage:
    python -m scripts.eval_harness export --run 3 --out eval_set.csv
    python -m scripts.eval_harness report eval_set_labeled.csv
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import Fact, Lead, SessionLocal, init_db  # noqa: E402


async def export(run_id: int | None, out: str, limit: int) -> None:
    await init_db()
    async with SessionLocal() as session:
        q = select(Lead).where(Lead.status == "done", Lead.fallback_used == False)  # noqa: E712
        if run_id:
            q = q.where(Lead.run_id == run_id)
        leads = (await session.execute(q.limit(limit))).scalars().all()
        facts = (await session.execute(
            select(Fact).where(Fact.lead_id.in_([l.id for l in leads])))).scalars().all()
    by_lead: dict[int, list[str]] = {}
    for f in facts:
        by_lead.setdefault(f.lead_id, []).append(f"{f.claim} <{f.source_url}>")

    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lead_id", "company", "draft", "fact_list",
                    "verifier_unsupported_claims", "label"])
        for l in leads:
            w.writerow([l.id, l.company_name, l.draft_edited or l.draft,
                        "\n".join(by_lead.get(l.id, [])), l.unsupported_claims, ""])
    print(f"exported {len(leads)} drafts -> {out}")
    print("Label each row: `clean` (every claim supported) or `hallucinated`.")


def report(labeled_csv: str) -> None:
    rows = list(csv.DictReader(open(labeled_csv)))
    labeled = [r for r in rows if r["label"].strip().lower() in ("clean", "hallucinated")]
    if not labeled:
        print("No labeled rows found. Fill the `label` column first.")
        return
    n = len(labeled)
    human_bad = sum(1 for r in labeled if r["label"].strip().lower() == "hallucinated")
    auto_bad = sum(1 for r in labeled if int(r["verifier_unsupported_claims"] or 0) > 0)

    tp = sum(1 for r in labeled if r["label"].strip().lower() == "hallucinated"
             and int(r["verifier_unsupported_claims"] or 0) > 0)
    fp = auto_bad - tp
    fn = human_bad - tp
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0

    print(f"labeled drafts:                      {n}")
    print(f"HUMAN  % with unsupported claim:     {100*human_bad/n:.1f}%   <-- headline metric")
    print(f"AUTO   % flagged by verifier:        {100*auto_bad/n:.1f}%")
    print(f"verifier precision / recall:         {prec:.2f} / {rec:.2f}")
    print("\nRun this on a baseline run vs the grounded+verified pipeline and quote")
    print("the pair, e.g. 'cut drafts with hallucinated claims from 34% to 4%'.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--run", type=int, default=None)
    e.add_argument("--out", default="eval_set.csv")
    e.add_argument("--limit", type=int, default=50)
    r = sub.add_parser("report")
    r.add_argument("labeled_csv")
    args = p.parse_args()
    if args.cmd == "export":
        asyncio.run(export(args.run, args.out, args.limit))
    else:
        report(args.labeled_csv)
