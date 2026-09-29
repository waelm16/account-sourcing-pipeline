"""Move a batch's pending companies that also appear in another Apollo CSV to the front of the queue.

  python scripts/prioritize.py 2030-01-16e "priority-accounts.csv"

Matching is by Apollo Account Id, then by domain. Only pending lines move (done and
skipped lines keep their place); their relative order is kept, and each gets
"priority": "<csv file name>". The driver works a queue in file order, so these
companies are qualified first. Refuses while a run is active. The final line is JSON.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_queue as sq  # noqa: E402
import run_until_done as rud  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def prioritize(root: Path, batch: str, csv_path: Path) -> dict:
    qpath = root / "state" / "queue" / f"{batch}.jsonl"
    if not qpath.exists():
        return {"status": "error", "error": f"no queue for batch {batch}"}
    if rud.lock_holder_alive(root):
        return {"status": "error", "error": "a run is active; stop it first (the queue is rewritten after every company)"}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    ids = {(r.get("Apollo Account Id") or "").strip() for r in rows} - {""}
    doms = {sq.normalize_domain(r.get("Website")) for r in rows} - {None}
    lines = sq._read_jsonl(qpath)

    def hit(l: dict) -> bool:
        return l.get("apollo_account_id") in ids or (l.get("domain") or None) in doms

    front = [l for l in lines if l.get("status") == "pending" and hit(l)]
    for l in front:
        l["priority"] = csv_path.name
    front_ids = {id(l) for l in front}
    rest = [l for l in lines if id(l) not in front_ids]
    sq._write_atomic(qpath, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in front + rest))
    return {"status": "ok", "batch": batch, "matched": sum(1 for l in lines if hit(l)),
            "moved_to_front": len(front), "pending_total": sum(1 for l in lines if l.get("status") == "pending")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("batch")
    ap.add_argument("csv")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    out = prioritize(Path(a.root), a.batch, Path(a.csv))
    print(json.dumps(out))
    return 0 if out["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
