"""Phase 1: batch of N companies -> processed run -> CSV on disk.

Usage:
    python -m scripts.run_batch leads.csv --label "job-hunt batch 1"
    python -m scripts.run_batch --domains stripe.com,linear.app,posthog.com

CSV headers understood: company/company_name, domain/website/url,
contact/contact_name/name, role/title. This is the dogfood entry point —
point it at the companies you're job-hunting into today.
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_db  # noqa: E402
from app.main import _parse_csv, export_csv  # noqa: E402
from app.runner import create_run, execute_run  # noqa: E402


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("csv_file", nargs="?", help="CSV of leads")
    p.add_argument("--domains", help="comma-separated domains instead of a CSV")
    p.add_argument("--label", default="cli batch")
    p.add_argument("--icp", default="", help="ideal customer profile description")
    p.add_argument("--out", default="output.csv")
    args = p.parse_args()

    if args.csv_file:
        leads = _parse_csv(Path(args.csv_file).read_text())
    elif args.domains:
        leads = [{"domain": d.strip()} for d in args.domains.split(",")]
    else:
        p.error("provide a CSV file or --domains")

    await init_db()
    run_id = await create_run(leads, label=args.label)
    print(f"run {run_id}: processing {len(leads)} leads...")
    await execute_run(run_id, args.icp)

    resp = await export_csv(run_id)
    body = "".join([chunk async for chunk in resp.body_iterator])
    Path(args.out).write_text(body)
    print(f"done -> {args.out}")

    # quick per-lead summary
    reader = csv.DictReader(body.splitlines())
    for row in reader:
        print(f"  {row['company']:<25} icp={row['icp_score']:>3} "
              f"conf={row['confidence']:>3} unsupported={row['unsupported_claims']} "
              f"${row['cost_usd']}")


if __name__ == "__main__":
    asyncio.run(main())
