"""Firmographics for companies the pipeline pushed to HubSpot (employee count, industry).

Only companies with a HubSpot record from this pipeline (queue rows with a
hubspot_id) are considered, and only those whose HubSpot record is missing
numberofemployees or industry cost an Apollo credit. The /enrich skill does the
Apollo calls (apollo_organizations_bulk_enrich, 1 credit per matched company);
this script decides what to enrich and writes the results to HubSpot.

  python scripts/enrich.py todo    -> {"count": N, "domains": [...], "results_file": "..."}
  python scripts/enrich.py apply   -> {"status": "ok", "enriched": n, ...}  (final line)

Rules:
- a domain is enriched at most once (state/enrich/done.json); not-found is final too,
  so a credit is never spent twice on the same company.
- one call spends at most apollo.max_enrich_per_call credits (config/settings.json); the rest
  waits for the next call.
- HubSpot values that are already set are never overwritten.
- industry is written only when Apollo's text matches one of HubSpot's industry
  options exactly (by label, ignoring case, punctuation and "&" vs "and");
  otherwise it is left empty and listed under unmapped_industry.
State lives in state/enrich/ (never in the queue files, which a running
nightly process rewrites).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hubspot_writer as hw  # noqa: E402

ROOT = Path(os.environ.get("SOURCING_ROOT") or Path(__file__).resolve().parents[1])
PUSHED_ACTIONS = {"created", "updated", "exists", "unchanged"}


def enrich_dir(root: Path) -> Path:
    d = root / "state" / "enrich"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_done(root: Path) -> dict:
    p = enrich_dir(root) / "done.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_done(root: Path, done: dict) -> None:
    p = enrich_dir(root) / "done.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def pushed_companies(root: Path) -> dict:
    """{domain: hubspot_id} for every queue row that has a HubSpot record."""
    out = {}
    for qf in sorted(glob.glob(str(root / "state" / "queue" / "*.jsonl"))):
        for line in Path(qf).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("hubspot_id") and r.get("hubspot_action") in PUSHED_ACTIONS:
                out[hw.normalize_domain(r.get("domain", ""))] = str(r["hubspot_id"])
    return out


def _norm(s: str, drop_and: bool = False) -> str:
    s = (s or "").lower().replace("_", " ").replace("&", " and ")
    if drop_and:
        s = re.sub(r"\band\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def industry_index(options) -> dict:
    """{normalised label or value: option value}. The word 'and' is optional, so
    'Food & Beverages' matches both the label and FOOD_BEVERAGES."""
    idx = {}
    for o in options:
        for s in (getattr(o, "label", ""), getattr(o, "value", "")):
            for n in (_norm(s), _norm(s, drop_and=True)):
                if n:
                    idx.setdefault(n, o.value)
    return idx


def map_industry(apollo_industry: str, idx: dict) -> str | None:
    n = _norm(apollo_industry)
    if not n:
        return None
    return idx.get(n) or idx.get(_norm(apollo_industry, drop_and=True))


def _employees(x) -> int | None:
    try:
        v = int(float(str(x).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


DEFAULT_MAX_PER_CALL = 10


def max_per_call(root: Path) -> int:
    """apollo.max_enrich_per_call in config/settings.json: the credit ceiling of one /enrich call."""
    try:
        s = json.loads((root / "config" / "settings.json").read_text(encoding="utf-8"))
        n = int((s.get("apollo") or {}).get("max_enrich_per_call", DEFAULT_MAX_PER_CALL))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_MAX_PER_CALL
    return max(0, n)


def todo(root: Path, client) -> dict:
    """Domains still worth an Apollo credit, at most apollo.max_enrich_per_call of them. Records that
    already have both fields are marked not_needed without spending anything."""
    done = load_done(root)
    need = []
    for domain, cid in pushed_companies(root).items():
        if domain in done:
            continue
        p = client.crm.companies.basic_api.get_by_id(
            company_id=cid, properties=["numberofemployees", "industry"]).properties or {}
        if p.get("numberofemployees") and p.get("industry"):
            done[domain] = {"status": "not_needed", "at": dt.datetime.now().isoformat(timespec="seconds")}
            continue
        need.append(domain)
    save_done(root, done)
    ceiling = max_per_call(root)
    need, waiting = need[:ceiling], max(0, len(need) - ceiling)
    return {"count": len(need), "domains": need, "credit_ceiling": ceiling, "waiting": waiting,
            "results_file": str((enrich_dir(root) / "results.jsonl").relative_to(root)).replace("\\", "/")}


def apply(root: Path, client) -> dict:
    """Write results.jsonl (written by /enrich) into HubSpot; fill only empty fields."""
    rf = enrich_dir(root) / "results.jsonl"
    results = {}
    if rf.exists():
        for line in rf.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                results[hw.normalize_domain(r.get("domain", ""))] = r
    done = load_done(root)
    pushed = pushed_companies(root)
    idx = None
    summary = {"status": "ok", "enriched": 0, "not_found": 0, "no_new_data": 0,
               "unmapped_industry": [], "errors": []}
    now = dt.datetime.now().isoformat(timespec="seconds")
    for domain, r in results.items():
        if domain in done or domain not in pushed:
            continue
        if not r.get("found"):
            done[domain] = {"status": "not_found", "at": now}
            summary["not_found"] += 1
            continue
        try:
            cid = pushed[domain]
            cur = client.crm.companies.basic_api.get_by_id(
                company_id=cid, properties=["numberofemployees", "industry"]).properties or {}
            upd = {}
            emp = _employees(r.get("estimated_num_employees"))
            if emp and not cur.get("numberofemployees"):
                upd["numberofemployees"] = str(emp)
            ind_text = r.get("industry") or ""
            ind = None
            if ind_text and not cur.get("industry"):
                if idx is None:
                    prop = client.crm.properties.core_api.get_by_name(object_type="companies",
                                                                      property_name="industry")
                    idx = industry_index(prop.options or [])
                ind = map_industry(ind_text, idx)
                if ind:
                    upd["industry"] = ind
                else:
                    summary["unmapped_industry"].append(f"{domain}: {ind_text}")
            if upd:
                from hubspot.crm.companies import SimplePublicObjectInput
                client.crm.companies.basic_api.update(
                    company_id=cid, simple_public_object_input=SimplePublicObjectInput(properties=upd))
                summary["enriched"] += 1
            else:
                summary["no_new_data"] += 1
            done[domain] = {"status": "ok" if upd else "no_new_data", "at": now,
                            "employees": emp, "industry_apollo": ind_text, "industry_hubspot": ind,
                            "written": sorted(upd)}
        except Exception as e:  # one record must not stop the rest; retried next run
            summary["errors"].append(f"{domain}: {type(e).__name__}: {e}"[:200])
    save_done(root, done)
    left = {d: r for d, r in results.items() if d not in done and d in pushed}  # errors: retry next time
    if left:
        rf.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in left.values()), encoding="utf-8")
    elif rf.exists():
        rf.unlink()
    if summary["errors"]:
        summary["status"] = "partial"
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["todo", "apply"])
    a = ap.parse_args(argv)
    client = hw.get_client()
    if client is None:
        print(json.dumps({"status": "error", "error": "no HubSpot token (keyring account-sourcing/HUBSPOT_TOKEN)"}))
        return 2
    out = todo(ROOT, client) if a.cmd == "todo" else apply(ROOT, client)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
