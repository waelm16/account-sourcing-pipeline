"""Deterministic half of the /source and /register-results skills.

The LLM only talks to Apollo and transcribes compact rows; everything that must
be exact (domain normalisation, dedupe, pre-filter, cap, queue file) lives here.

Files (all under state/, gitignored):
  state/source/<date>/page-<n>.jsonl        rows transcribed by /source, one per org
  state/source/<date>/candidates.json       written by `select`, BEFORE any Apollo write
  state/source/<date>/registered.json       written by /source after bulk_create + label
  state/queue/<date>.jsonl                  written by `build` (atomic); the driver's input
  state/source/<date>/results_registered.jsonl  appended by /register-results

CLI (repo root, repo venv):
  python scripts/source_queue.py select --date D [--cap N]
  python scripts/source_queue.py build --date D [--check]
  python scripts/source_queue.py unregistered --date D
  python scripts/source_queue.py mark --date D --method field|list --ids ID1,ID2
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

QUEUE_KEYS = ["domain", "name", "apollo_org_id", "apollo_account_id", "industry", "headcount",
              "tech", "funding", "job_titles", "status"]
TERMINAL = {"done", "error", "skipped_prefilter", "hubspot_error", "needs_review"}
RESULT_VALUES = {"QUALIFIED": "Qualified", "MAYBE": "Maybe", "NOT_QUALIFIED": "Not Qualified"}
RESULT_SLUG = {"Qualified": "qualified", "Maybe": "maybe", "Not Qualified": "rejected",
               "Prefiltered": "prefiltered", "Error": "error"}


# ---------------------------------------------------------------- pure helpers

def normalize_domain(value) -> str | None:
    """'https://WWW.Acme.example:443/about' -> 'acme.example'. None/blank -> None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split("@")[-1].split(":", 1)[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    return s if "." in s else None


def _to_int(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).replace(",", "").strip()))
    except ValueError:
        return None


def _to_list(v) -> list | None:
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")] or None
    return [v]


COUNTRY_ALIASES = {
    "us": "united states", "usa": "united states", "u.s.": "united states", "u.s.a.": "united states",
    "united states of america": "united states", "america": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "great britain": "united kingdom", "britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
    "northern ireland": "united kingdom", "ca": "canada",
}


def _country(v) -> str:
    c = (v or "").strip().lower() if isinstance(v, str) else ""
    return COUNTRY_ALIASES.get(c, c)


def prefilter(row: dict, cfg: dict) -> tuple[bool, str | None]:
    """Exclusion of obvious non-targets by name/industry regex (config/search.json pre_filter).

    Only these exclude, because an excluded company is registered in Apollo and is
    gone for good. Unknown data never excludes."""
    cfg = cfg or {}
    name = (row.get("name") or "").strip()
    industry = (row.get("industry") or "").strip()
    if cfg.get("exclude_name_regex") and name and re.search(cfg["exclude_name_regex"], name, re.I):
        return False, "name_match"
    if cfg.get("exclude_industry_regex") and industry and re.search(cfg["exclude_industry_regex"], industry, re.I):
        return False, "industry_match"
    # Prompt 1 never qualifies subsidiaries; only CSV imports carry Apollo's parent-company column.
    if cfg.get("exclude_subsidiaries") and (row.get("parent_company") or "").strip():
        return False, "subsidiary"
    return True, None


def scope_warnings(row: dict, cfg: dict) -> list[str]:
    """Country/headcount outside the configured scope. Apollo already filtered on
    these, so a mismatch is usually alias/transcription noise: warn, never exclude.
    Prompt 1 and the morning report see the warning."""
    cfg = cfg or {}
    out = []
    allowed = {_country(c) for c in cfg.get("allowed_countries") or []}
    country = _country(row.get("country"))
    if allowed and country and country not in allowed:
        out.append(f"country={row.get('country')}")
    hc = _to_int(row.get("headcount"))
    if hc is not None:
        lo, hi = _to_int(cfg.get("headcount_min")), _to_int(cfg.get("headcount_max"))
        if (lo is not None and hc < lo) or (hi is not None and hc > hi):
            out.append(f"headcount={hc}")
    return out


def search_signals(filters: dict) -> dict:
    """What every row of this search matched on (context for Prompt 1)."""
    f = filters or {}
    out = {
        "technologies_any": f.get("currently_using_any_of_technology_uids"),
        "job_titles_any": f.get("q_organization_job_titles"),
        "job_posted_since": (f.get("organization_job_posted_at_range") or {}).get("min"),
        "total_funding_min": (f.get("total_funding_range") or {}).get("min"),
        "latest_funding_since": (f.get("latest_funding_date_range") or {}).get("min"),
        "naics": f.get("organization_naics_codes"),
        "keyword_tags": f.get("q_organization_keyword_tags"),
    }
    return {k: v for k, v in out.items() if v}


def _clean_row(row: dict) -> dict:
    funding = row.get("funding")
    if not isinstance(funding, dict):
        funding = {"total": _to_int(row.get("funding_total")),
                   "latest_date": row.get("latest_funding_date"),
                   "latest_stage": row.get("latest_funding_stage")}
    funding = {k: v for k, v in funding.items() if v not in (None, "")} or None
    return {
        "domain": normalize_domain(row.get("domain") or row.get("primary_domain") or row.get("website_url")),
        "name": (row.get("name") or "").strip() or None,
        "apollo_org_id": row.get("org_id") or row.get("organization_id"),
        "apollo_account_id": row.get("account_id"),
        "industry": row.get("industry") or None,
        "headcount": _to_int(row.get("headcount")),
        "tech": _to_list(row.get("technologies") or row.get("tech")),
        "funding": funding,
        "job_titles": _to_list(row.get("job_titles")),
        "country": row.get("country") or None,
    }


HEX24 = re.compile(r"^[a-f0-9]{24}$")


def row_problem(raw: dict) -> str | None:
    """Transcription sanity check. A row is invalid if an Apollo id is not 24-hex,
    or a domain was given but does not normalise, or it has neither name nor domain."""
    for k in ("org_id", "organization_id", "account_id"):
        v = raw.get(k)
        if v not in (None, "") and not (isinstance(v, str) and HEX24.match(v)):
            return f"bad {k}"
    d = raw.get("domain") or raw.get("primary_domain")
    if d and not normalize_domain(d):
        return "bad domain"
    if not (raw.get("name") or d):
        return "no name or domain"
    return None


def select(rows: list[dict], pre_filter_cfg: dict, cap: int, seen_domains: set[str],
           filters: dict | None = None, excluded: dict | None = None) -> dict:
    """Walk rows in search order until `cap` pending. Prefiltered rows met before
    the cap are kept as skipped_prefilter (they get registered too, so they never
    come back). Rows past the cap are left untouched for a later night.
    `excluded` = {"org_ids": set, "domains": set} from the keyword-tag lookup."""
    ex_ids = (excluded or {}).get("org_ids") or set()
    ex_domains = (excluded or {}).get("domains") or set()
    candidates: list[dict] = []
    resurfaced: list[str] = []
    invalid: list[dict] = []
    run_seen: set[str] = set()
    pending = 0
    signals = search_signals(filters or {})
    for raw in rows:
        if pending >= cap:
            break
        problem = row_problem(raw)
        if problem:
            invalid.append({"row": raw, "problem": problem})
            continue
        row = _clean_row(raw)
        key = row["domain"] or ("name:" + (row["name"] or "").lower())
        if key in run_seen or key == "name:":
            continue
        run_seen.add(key)
        if row["domain"] and row["domain"] in seen_domains:
            resurfaced.append(row["domain"])
            continue
        if not row["domain"]:
            ok, reason = False, "no_domain"
        elif (row["apollo_org_id"] and row["apollo_org_id"] in ex_ids) or row["domain"] in ex_domains:
            ok, reason = False, "excluded_keyword_tag"
        else:
            ok, reason = prefilter({**raw, **row}, pre_filter_cfg)
        row["status"] = "pending" if ok else "skipped_prefilter"
        row["skip_reason"] = reason
        row["scope_warnings"] = scope_warnings({**raw, **row}, pre_filter_cfg)
        row["source_rank"] = len(candidates) + 1
        row["search_signals"] = signals
        candidates.append(row)
        if ok:
            pending += 1
    return {"candidates": candidates, "resurfaced": resurfaced, "invalid": invalid, "pending": pending,
            "prefiltered": sum(1 for c in candidates if c["status"] == "skipped_prefilter"),
            "need_more": pending < cap}


def _match_registered(c: dict, registered: list[dict]) -> str | None:
    if c.get("apollo_account_id"):
        return c["apollo_account_id"]
    for r in registered:
        if c.get("apollo_org_id") and r.get("organization_id") == c["apollo_org_id"]:
            return r.get("account_id")
    for r in registered:
        if c.get("domain") and normalize_domain(r.get("domain")) == c["domain"]:
            return r.get("account_id")
    # Name match only when one side has no domain: bulk_create dedupe also matches
    # by NAME, so a different company with the same name can come back under
    # existing_accounts. Never adopt its id (see name_collisions).
    for r in registered:
        if c.get("name") and (r.get("name") or "").strip().lower() == c["name"].lower():
            if c.get("domain") and normalize_domain(r.get("domain")):
                continue
            return r.get("account_id")
    return None


def name_collisions(candidates: list[dict], registered: list[dict]) -> list[str]:
    """Candidates left unmatched because a same-name account with another domain came back."""
    out = []
    for c in candidates:
        if not c.get("domain") or _match_registered(c, registered):
            continue
        for r in registered:
            rd = normalize_domain(r.get("domain"))
            if rd and rd != c["domain"] and (r.get("name") or "").strip().lower() == (c.get("name") or "").lower():
                out.append(f"dedupe_name_collision: {c['domain']} vs {rd}")
                break
    return out


def build_queue_lines(candidates: list[dict], registered: list[dict], date: str, list_name: str) -> list[dict]:
    lines = []
    for c in candidates:
        line = {k: c.get(k) for k in QUEUE_KEYS}
        line["apollo_account_id"] = _match_registered(c, registered or [])
        line.update({"date": date, "list_name": list_name, "source_rank": c.get("source_rank"),
                     "skip_reason": c.get("skip_reason"), "search_signals": c.get("search_signals") or {},
                     "country": c.get("country"), "scope_warnings": c.get("scope_warnings") or []})
        lines.append(line)
    return lines


def result_value(line: dict) -> str | None:
    """Queue line -> 'Sourcing P1 Result' value, or None if not terminal yet."""
    status = line.get("status")
    if status == "skipped_prefilter":
        return "Prefiltered"
    if status == "error":
        return "Error"
    # hubspot_error / needs_review: P1 finished and the verdict is final; only the
    # HubSpot side is pending or needs a human. Apollo gets the P1 verdict.
    if status in ("done", "hubspot_error", "needs_review"):
        v = line.get("verdict")
        if isinstance(v, dict):
            v = v.get("qualification")
        return RESULT_VALUES.get(str(v or "").upper(), "Error")
    return None


# ---------------------------------------------------------------- file helpers

def _read_jsonl(path: Path, bad: list | None = None) -> list[dict]:
    """Read JSONL; malformed lines are skipped and, if `bad` is given, recorded there."""
    if not path.exists():
        return []
    out = []
    for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        ln = ln.strip()
        if ln:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                out.append(obj)
            elif bad is not None:
                bad.append(f"{path.name}:{i}")
    return out


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _page_files(sdir: Path) -> list[Path]:
    def n(p: Path) -> int:
        m = re.search(r"page-(\d+)", p.name)
        return int(m.group(1)) if m else 0
    return sorted(sdir.glob("page-*.jsonl"), key=n)


def seen_domains(root: Path, exclude_date: str | None = None) -> set[str]:
    seen = set()
    for q in (root / "state" / "queue").glob("*.jsonl"):
        if exclude_date and q.stem == exclude_date:
            continue
        for line in _read_jsonl(q):
            d = normalize_domain(line.get("domain"))
            if d:
                seen.add(d)
    return seen


# A batch id is the real date of the run, plus a letter for extra batches the same day:
# 2030-01-16, 2030-01-16b, 2030-01-16c ... (sorts correctly as a string).
BATCH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[b-z]?$")


def is_batch(s: str) -> bool:
    if not BATCH_RE.match(s or ""):
        return False
    dt.date.fromisoformat(s[:10])
    return True


def next_batch(root: Path, today: str) -> str:
    """First unused batch id for `today` (no queue file and no source dir yet)."""
    for suffix in [""] + [chr(c) for c in range(ord("b"), ord("z") + 1)]:
        b = today + suffix
        if not (root / "state" / "queue" / f"{b}.jsonl").exists() and not (root / "state" / "source" / b).exists():
            return b
    raise RuntimeError(f"no free batch id left for {today}")


def list_name_for(settings: dict, date: str) -> str:
    return (settings.get("list_name_pattern") or "sourced-{date}").replace("{date}", date)


def unregistered(root: Path, date: str) -> list[dict]:
    """Terminal queue lines whose P1 result is not yet recorded in Apollo."""
    queue = _read_jsonl(root / "state" / "queue" / f"{date}.jsonl")
    done = {(r.get("apollo_account_id"), r.get("result"))
            for r in _read_jsonl(root / "state" / "source" / date / "results_registered.jsonl") if r.get("ok")}
    out = []
    for line in queue:
        res = result_value(line)
        acc = line.get("apollo_account_id")
        if res is None or not acc or (acc, res) in done:
            continue
        out.append({"apollo_account_id": acc, "domain": line.get("domain"), "name": line.get("name"),
                    "result": res, "result_slug": RESULT_SLUG[res]})
    return out


# ---------------------------------------------------------------- CLI

def _settings(root: Path) -> dict:
    return _load_json(root / "config" / "settings.json", {})


def cmd_select(root: Path, date: str, cap: int | None) -> dict:
    sdir = root / "state" / "source" / date
    settings = _settings(root)
    search = _load_json(root / "config" / "search.json", {})
    cap = cap if cap is not None else int(settings.get("per_run_cap", 50))
    rows, malformed = [], []
    for p in _page_files(sdir):
        rows.extend(_read_jsonl(p, malformed))
    excluded = {"org_ids": set(), "domains": set()}
    for r in _read_jsonl(sdir / "exclude.jsonl", malformed) if (sdir / "exclude.jsonl").exists() else []:
        if r.get("org_id"):
            excluded["org_ids"].add(r["org_id"])
        d = normalize_domain(r.get("domain"))
        if d:
            excluded["domains"].add(d)
    res = select(rows, search.get("pre_filter") or {}, cap, seen_domains(root, exclude_date=date),
                 search.get("filters") or {}, excluded)
    _write_atomic(sdir / "candidates.json", json.dumps(
        {"date": date, "cap": cap, "list_name": list_name_for(settings, date), **res}, indent=1) + "\n")
    return {"rows": len(rows), "pending": res["pending"], "prefiltered": res["prefiltered"],
            "resurfaced": len(res["resurfaced"]), "resurfaced_domains": res["resurfaced"],
            "invalid": len(res["invalid"]) + len(malformed),
            "invalid_detail": [i["problem"] + ": " + json.dumps(i["row"])[:120] for i in res["invalid"]] + malformed,
            "need_more": res["need_more"],
            "excluded_keyword_tag": sum(1 for c in res["candidates"]
                                        if c.get("skip_reason") == "excluded_keyword_tag"),
            "to_register": len(res["candidates"])}


def cmd_build(root: Path, date: str, check: bool = False) -> dict:
    """Write state/queue/<date>.jsonl from candidates.json + registered.json.
    check=True: dry run, reports which candidates still lack an Apollo account id."""
    sdir = root / "state" / "source" / date
    qpath = root / "state" / "queue" / f"{date}.jsonl"
    summary = {"status": "ok", "date": date, "pulled": 0, "pending": 0, "prefiltered": 0, "resurfaced": 0,
               "registered": 0, "unregistered": 0, "credits_used": 0, "list_name": None,
               "queue_file": f"state/queue/{date}.jsonl", "warnings": [], "error": None}
    summary["credits_used"] = sum(1 for p in _page_files(sdir) if p.stat().st_size > 0)
    if qpath.exists():
        lines = _read_jsonl(qpath)
        summary.update(status="exists", pulled=len(lines),
                       pending=sum(1 for l in lines if l.get("status") == "pending"),
                       prefiltered=sum(1 for l in lines if l.get("status") == "skipped_prefilter"),
                       registered=sum(1 for l in lines if l.get("apollo_account_id")),
                       list_name=next((l.get("list_name") for l in lines if l.get("list_name")), None))
        summary["unregistered"] = summary["pulled"] - summary["registered"]
        return summary
    cand = _load_json(sdir / "candidates.json", None)
    if cand is None:
        summary.update(status="error", error="candidates.json missing: run select first")
        return summary
    reg = _load_json(sdir / "registered.json", {"accounts": []})
    list_name = reg.get("list_name") or cand.get("list_name")
    lines = build_queue_lines(cand["candidates"], reg.get("accounts") or [], date, list_name)
    missing = [{"domain": l["domain"], "name": l["name"]} for l in lines if not l["apollo_account_id"]]
    summary.update(
        pulled=len(lines),
        pending=sum(1 for l in lines if l["status"] == "pending"),
        prefiltered=sum(1 for l in lines if l["status"] == "skipped_prefilter"),
        resurfaced=len(cand.get("resurfaced") or []),
        registered=len(lines) - len(missing),
        unregistered=len(missing),
        list_name=list_name,
    )
    if reg.get("list_url"):
        summary["list_url"] = reg["list_url"]
    summary["warnings"].extend(name_collisions(cand["candidates"], reg.get("accounts") or []))
    if missing:
        summary["warnings"].append(f"{len(missing)} companies have no Apollo account id: they stay Net New "
                                   "and may resurface; P1 still runs on them")
    if summary["resurfaced"]:
        summary["warnings"].append(f"{summary['resurfaced']} net-new rows were already in an earlier queue "
                                   "(registration did not remove them from Net New?)")
    if check:
        summary["status"] = "check"
        summary["missing"] = missing
        return summary
    if not lines:
        summary["status"] = "empty"
        return summary
    _write_atomic(qpath, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))
    return summary


def mark_registered(root: Path, date: str, ids: list[str], method: str) -> dict:
    """Append ok lines to results_registered.jsonl for ids currently unregistered."""
    todo = {u["apollo_account_id"]: u for u in unregistered(root, date)}
    path = root / "state" / "source" / date / "results_registered.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    marked, unknown = [], []
    with open(path, "a", encoding="utf-8") as f:
        for i in ids:
            u = todo.get(i)
            if not u:
                unknown.append(i)
                continue
            f.write(json.dumps({"apollo_account_id": i, "domain": u["domain"], "result": u["result"],
                                "method": method, "ok": True,
                                "at": dt.datetime.now().isoformat(timespec="seconds")}) + "\n")
            marked.append(i)
    return {"marked": len(marked), "unknown_ids": unknown, "remaining": len(unregistered(root, date))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["select", "build", "unregistered", "mark"])
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--cap", type=int, default=None)
    ap.add_argument("--check", action="store_true", help="build: dry run, list candidates without account id")
    ap.add_argument("--method", choices=["field", "list"], help="mark: how the result was written")
    ap.add_argument("--ids", default="", help="mark: comma-separated Apollo account ids written successfully")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    if not is_batch(a.date):
        ap.error("--date must be YYYY-MM-DD or a same-day batch id like 2030-01-16b")
    root = Path(a.root)
    if a.cmd == "select":
        out = cmd_select(root, a.date, a.cap)
    elif a.cmd == "build":
        out = cmd_build(root, a.date, a.check)
    elif a.cmd == "mark":
        if not a.method:
            ap.error("mark needs --method")
        out = mark_registered(root, a.date, [i.strip() for i in a.ids.split(",") if i.strip()], a.method)
    else:
        out = unregistered(root, a.date)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
