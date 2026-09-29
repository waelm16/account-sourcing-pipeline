"""Queue the companies of an Apollo CSV export as a new batch (no Apollo search, no credits).

  python scripts/import_csv.py "apollo-accounts-export.csv" [--batch 2030-01-16e] [--dry-run]

Works with Apollo *accounts* exports (Companies tab -> Export): every row carries an
"Apollo Account Id", so nothing has to be created in Apollo and the queue lines get
their account id directly. The same pre-filters as /source apply (config/search.json):
keyword tags (exclude_keyword_tags, matched on the Keywords column), name and
industry regexes, subsidiaries (Parent company column), plus rows without a website.
Companies already in an earlier queue are skipped (resurfaced), so a CSV can be
re-imported safely. The batch is then worked like any other queue:

  python scripts/run_until_done.py --date <batch>

Writes state/source/<batch>/ (page-1.jsonl, exclude.jsonl, source.json, candidates.json,
registered.json) and state/queue/<batch>.jsonl. The final line is JSON.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_queue as sq  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Apollo export column -> /source page-row key
COLUMNS = {
    "Apollo Account Id": "account_id",
    "Company Name": "name",
    "Website": "domain",
    "Industry": "industry",
    "# Employees": "headcount",
    "Company Country": "country",
    "Total Funding": "funding_total",
    "Last Raised At": "latest_funding_date",
    "Latest Funding": "latest_funding_stage",
    "Parent company (Apollo data)": "parent_company",
}


def row_from_csv(r: dict) -> dict:
    out = {}
    for col, key in COLUMNS.items():
        v = (r.get(col) or "").strip()
        if v:
            out[key] = v
    tech = [t.strip() for t in (r.get("Technologies") or "").split(",") if t.strip()]
    if tech:
        out["technologies"] = tech
    return out


def keyword_excluded(r: dict, tags: list[str]) -> bool:
    text = f"{r.get('Keywords') or ''}, {r.get('Industry') or ''}".lower()
    return any(t.lower() in text for t in tags)


def import_csv(root: Path, csv_path: Path, batch: str | None = None, dry: bool = False) -> dict:
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        raw = list(csv.DictReader(f))
    if not raw or "Apollo Account Id" not in raw[0]:
        return {"status": "error", "error": "not an Apollo accounts export (no 'Apollo Account Id' column)"}
    batch = batch or sq.next_batch(root, dt.date.today().isoformat())
    if not sq.is_batch(batch):
        return {"status": "error", "error": f"bad batch id {batch!r}"}
    sdir = root / "state" / "source" / batch
    if (root / "state" / "queue" / f"{batch}.jsonl").exists() or sdir.exists():
        return {"status": "error", "error": f"batch {batch} already exists"}
    search = json.loads((root / "config" / "search.json").read_text(encoding="utf-8"))
    tags = search.get("exclude_keyword_tags") or []
    rows = [row_from_csv(r) for r in raw]
    excluded = [{"domain": sq.normalize_domain(row.get("domain")), "name": row.get("name")}
                for r, row in zip(raw, rows) if tags and keyword_excluded(r, tags)]
    if dry:
        res = sq.select(rows, search.get("pre_filter") or {}, len(rows), sq.seen_domains(root),
                        search.get("filters") or {},
                        {"org_ids": set(), "domains": {e["domain"] for e in excluded if e["domain"]}})
        return {"status": "dry_run", "batch": batch, **_counts(len(raw), res)}
    sdir.mkdir(parents=True)
    sq._write_atomic(sdir / "page-1.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    sq._write_atomic(sdir / "exclude.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in excluded))
    sq._write_atomic(sdir / "source.json", json.dumps(
        {"mode": "csv", "file": csv_path.name, "rows": len(raw), "imported_at": dt.datetime.now().isoformat(
            timespec="seconds")}, indent=1) + "\n")
    # The companies already are Apollo accounts: no registration, the list name just says where they came from.
    sq._write_atomic(sdir / "registered.json", json.dumps(
        {"list_name": f"csv:{csv_path.name}", "list_url": None, "accounts": []}, indent=1) + "\n")
    sq.cmd_select(root, batch, len(rows))
    cand = json.loads((sdir / "candidates.json").read_text(encoding="utf-8"))
    build = sq.cmd_build(root, batch)
    return {"status": build["status"], "batch": batch, "queue_file": build.get("queue_file"),
            **_counts(len(raw), cand), "warnings": build.get("warnings", []), "error": build.get("error")}


def _counts(n_rows: int, res: dict) -> dict:
    reasons: dict[str, int] = {}
    for c in res["candidates"]:
        if c["status"] == "skipped_prefilter":
            reasons[c["skip_reason"]] = reasons.get(c["skip_reason"], 0) + 1
    return {"rows": n_rows, "to_qualify": res["pending"], "skipped": reasons,
            "already_queued_before": len(res.get("resurfaced") or []), "invalid": len(res.get("invalid") or [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--batch", help="batch id (default: next free id for today, e.g. 2030-01-16e)")
    ap.add_argument("--dry-run", action="store_true", help="only report what would be queued")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    out = import_csv(Path(a.root), Path(a.csv), a.batch, a.dry_run)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["status"] in ("ok", "dry_run") else 2


if __name__ == "__main__":
    sys.exit(main())
