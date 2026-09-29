"""Translate an Apollo web-app Companies URL into Apollo MCP search params.

The owner builds and sizes a search in the Apollo UI, copies the URL
(`https://app.apollo.io/#/companies?...`) and this script turns its query
string into the parameter names of the MCP tool `apollo_mixed_companies_search`.
Anything it cannot translate is listed under `unmapped` with the fallback that
applies, so nothing is silently dropped.

Usage (from the repo root, with the repo venv):
  python scripts/apollo_url.py parse "<apollo url>"            # print JSON
  python scripts/apollo_url.py parse "<apollo url>" --write    # merge into config/search.json
  python scripts/apollo_url.py ack "qPersonTitles[]"            # owner accepts running without it
  python scripts/apollo_url.py resolve --date 2030-01-15        # validate + MCP params (exit 2 if unsafe)

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SEARCH_JSON = ROOT / "config" / "search.json"

# URL base key (brackets stripped) -> (MCP param, kind)
# kind: list | range_int | range_date | range_str | str | int
PARAM_MAP: dict[str, tuple[str, str]] = {
    "organizationNumEmployeesRanges": ("organization_num_employees_ranges", "list"),
    "organizationLocations": ("organization_locations", "list"),
    "organizationNotLocations": ("organization_not_locations", "list"),
    "currentlyUsingAnyOfTechnologyUids": ("currently_using_any_of_technology_uids", "list"),
    "qOrganizationJobTitles": ("q_organization_job_titles", "list"),
    "organizationJobLocations": ("organization_job_locations", "list"),
    "qOrganizationKeywordTags": ("q_organization_keyword_tags", "list"),
    "organizationNaicsCodes": ("organization_naics_codes", "list"),
    "notOrganizationNaicsCodes": ("not_organization_naics_codes", "list"),
    "organizationSicCodes": ("organization_sic_codes", "list"),
    "notOrganizationSicCodes": ("not_organization_sic_codes", "list"),
    "marketSegments": ("market_segments", "list"),
    "qOrganizationDomainsList": ("q_organization_domains_list", "list"),
    "organizationIds": ("organization_ids", "list"),
    "accountLabelIds": ("account_label_ids", "list"),
    "totalFundingRange": ("total_funding_range", "range_int"),
    "latestFundingAmountRange": ("latest_funding_amount_range", "range_int"),
    "revenueRange": ("revenue_range", "range_int"),
    "organizationFoundedYearRange": ("organization_founded_year_range", "range_int"),
    "organizationNumJobsRange": ("organization_num_jobs_range", "range_int"),
    "organizationHeadcountGrowthRange": ("organization_headcount_growth_range", "range_int"),
    "latestFundingDateRange": ("latest_funding_date_range", "range_date"),
    "organizationJobPostedAtRange": ("organization_job_posted_at_range", "range_date"),
    "latestFundingDetectedAtRange": ("latest_funding_detected_at_range", "range_str"),
    "qOrganizationName": ("q_organization_name", "str"),
    "organizationHeadcountGrowthPastNMonths": ("organization_headcount_growth_past_n_months", "int"),
}

# Known Apollo UI params with no MCP equivalent: reason + fallback.
KNOWN_UNMAPPED: dict[str, tuple[str, str]] = {
    "organizationIndustryTagIds": (
        "Apollo industry tag ids are not an MCP filter",
        "use organization_naics_codes (e.g. 4451) and not_organization_sic_codes instead",
    ),
    "organizationNotIndustryTagIds": (
        "Apollo industry tag exclusion is not an MCP filter",
        "use not_organization_naics_codes / not_organization_sic_codes, plus pre_filter.exclude_industry_regex",
    ),
    "qNotOrganizationKeywordTags": (
        "keyword exclusion is not an MCP filter",
        "add the terms to pre_filter.exclude_name_regex / exclude_industry_regex",
    ),
    "qKeywords": (
        "free-text keyword search is not an MCP filter",
        "use q_organization_keyword_tags (broad: it also matches companies that sell into the industry) or drop it",
    ),
    "personTitles": (
        "person-level signal (someone with this title works there) is not exposed by the company search",
        "list mode: build the search in the Apollo UI, save the companies to an accounts list, set mode=list",
    ),
    "qPersonTitles": (
        "person-level signal is not exposed by the company search",
        "list mode: save the companies to an accounts list in the UI, set mode=list",
    ),
    "personSeniorities": (
        "person-level signal is not exposed by the company search",
        "list mode (see personTitles)",
    ),
    "organizationTradingStatus": ("trading status is not an MCP filter", "none; P1 will see it"),
    "intentStrengths": ("intent data is not an MCP filter", "none"),
    "qOrganizationSearchListId": (
        "saved-search / list reference is not an MCP filter",
        "list mode with account_label_ids (resolve the list id via apollo_labels_index)",
    ),
}

# UI state only, never a filter.
IGNORED = {
    "page", "sortByField", "sortAscending", "finderViewId", "uniqueUrlId",
    "includedOrganizationKeywordFields", "includedAnyOfOrganizationKeywordFields",
    "excludedOrganizationKeywordFields",  # which fields a keyword exclusion applies to
    "displayMode", "tour", "recommendationConfigId", "finderTableLayoutId",
    "viewMode", "contactLabelIds", "per_page", "perPage",
}

# Flat UI params that fill one side of an MCP range.
FLAT_RANGE: dict[str, tuple[str, str]] = {
    "totalFundingMin": ("total_funding_range", "min"),
    "totalFundingMax": ("total_funding_range", "max"),
    "latestFundingAmountMin": ("latest_funding_amount_range", "min"),
    "latestFundingAmountMax": ("latest_funding_amount_range", "max"),
}

RELATIVE_RE = re.compile(r"^(\d+)_(day|days|week|weeks|month|months|year|years)_ago$")


def _split_key(raw: str) -> tuple[str, str | None]:
    """'totalFundingRange[min]' -> ('totalFundingRange', 'min'); 'x[]' -> ('x', '')."""
    m = re.match(r"^([^\[]+)(?:\[([^\]]*)\])?$", raw)
    if not m:
        return raw, None
    return m.group(1), m.group(2)


def _query_of(url: str) -> str:
    url = url.strip().strip('"').strip("'")
    parsed = urlparse(url)
    frag = parsed.fragment or ""
    if "?" in frag:
        return frag.split("?", 1)[1]
    if parsed.query:
        return parsed.query
    if "?" in url:
        return url.split("?", 1)[1]
    return ""


def _decode(v: str) -> str:
    """The Apollo UI double-encodes values (United%2520States); decode until stable."""
    for _ in range(3):
        d = unquote(v)
        if d == v:
            break
        v = d
    return v


def _to_int(v: str) -> int | str:
    s = v.replace(",", "").replace("$", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return v


def parse_apollo_url(url: str) -> dict:
    """Pure: Apollo UI URL -> config/search.json shape (without pre_filter)."""
    route = urlparse(url.strip()).fragment.split("?", 1)[0] if "#" in url else ""
    filters: dict = {}
    unmapped: list[dict] = []
    handled: list[dict] = []
    ignored: list[str] = []
    mode = "net_new_search"

    for raw_key, value in parse_qsl(_query_of(url), keep_blank_values=True):
        raw_key = _decode(raw_key)
        value = _decode(value)
        base, sub = _split_key(raw_key)

        if base in FLAT_RANGE:
            name, side = FLAT_RANGE[base]
            if value != "":
                filters.setdefault(name, {})[side] = _to_int(value)
            continue

        if base in IGNORED:
            if base not in ignored:
                ignored.append(base)
            continue

        if base == "prospectedByCurrentTeam":
            if value.lower() == "no":
                handled.append({"param": raw_key, "value": value,
                                "note": "Net New: /source keeps only the `organizations` (net-new) bucket"})
            else:
                unmapped.append({"param": raw_key, "value": value,
                                 "reason": "saved-accounts-only search is not Net New",
                                 "fallback": "list mode (mode=list with account_label_ids)"})
            continue

        if base in PARAM_MAP:
            name, kind = PARAM_MAP[base]
            if kind == "list":
                filters.setdefault(name, [])
                if value and value not in filters[name]:
                    filters[name].append(value)
            elif kind.startswith("range"):
                if sub not in ("min", "max"):
                    unmapped.append({"param": raw_key, "value": value,
                                     "reason": "range param without [min]/[max]", "fallback": "set it by hand"})
                    continue
                if value == "":
                    continue
                filters.setdefault(name, {})[sub] = _to_int(value) if kind == "range_int" else value
            elif kind == "int":
                filters[name] = _to_int(value)
            else:
                filters[name] = value
            if base == "accountLabelIds":
                mode = "list"
            continue

        if base in KNOWN_UNMAPPED:
            reason, fallback = KNOWN_UNMAPPED[base]
        else:
            reason, fallback = "unknown Apollo UI parameter", "check it in the UI; add to PARAM_MAP if it has an MCP equivalent"
        unmapped.append({"param": raw_key, "value": value, "reason": reason, "fallback": fallback})

    if route and route not in ("/companies", "/accounts", "/organizations"):
        unmapped.append({"param": "#" + route, "value": "", "reason": "not a Companies-tab URL",
                         "fallback": "open the Companies tab in Apollo and copy that URL"})

    notes = []
    if "organization_naics_codes" in filters or "organization_sic_codes" in filters:
        notes.append("Excluding a kind of company by keyword is not an Apollo search filter: pre_filter drops "
                     "companies by name/industry regex, and exclude_keyword_tags marks tagged companies as "
                     "prefiltered. Optional: not_organization_naics_codes / not_organization_sic_codes.")

    return {
        "source_url": url.strip(),
        "mode": mode,
        "filters": filters,
        "unmapped": unmapped,
        "handled": handled,
        "ignored": ignored,
        "notes": notes,
    }


def _resolve_date(v, today: dt.date):
    if not isinstance(v, str):
        return v
    s = v.strip().lower()
    if s == "today":
        return today.isoformat()
    m = RELATIVE_RE.match(s)
    if not m:
        return v
    n, unit = int(m.group(1)), m.group(2).rstrip("s")
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
    return (today - dt.timedelta(days=days)).isoformat()


def resolve_filters(filters: dict, date: str) -> dict:
    """Pure: resolve relative date tokens ('30_days_ago', 'today') in date ranges to ISO dates."""
    today = dt.date.fromisoformat(date[:10])  # batch ids like 2030-01-16b
    out = json.loads(json.dumps(filters))
    for key in ("latest_funding_date_range", "organization_job_posted_at_range"):
        if isinstance(out.get(key), dict):
            out[key] = {k: _resolve_date(v, today) for k, v in out[key].items()}
    return out


DEFAULT_REQUIRED = ["organization_naics_codes|organization_sic_codes",
                    "organization_num_employees_ranges", "organization_locations"]


def validate_search(search: dict, required: list[str] | None = None) -> list[str]:
    """Pure: reasons the unattended search must NOT run. Empty list = OK.

    - the shipped example config (source_url starting with EXAMPLE) never runs;
    - every `unmapped` entry must be acknowledged by the owner (param listed in
      `acknowledged_unmapped`), otherwise the search silently runs broader than
      the URL that was built;
    - net_new_search needs the base filters (`required`, 'a|b' = either);
      list mode needs account_label_ids.
    """
    errors = []
    if str(search.get("source_url") or "").strip().upper().startswith("EXAMPLE"):
        errors.append("config/search.json is the shipped EXAMPLE: run /search-config with the real Apollo URL first")
    filters = search.get("filters") or {}
    acked = set(search.get("acknowledged_unmapped") or [])
    for u in search.get("unmapped") or []:
        if u.get("param") not in acked:
            errors.append(f"unacknowledged unmapped filter {u.get('param')}={u.get('value')!r}: {u.get('reason')}")
    mode = search.get("mode", "net_new_search")
    if mode == "list":
        if not filters.get("account_label_ids"):
            errors.append("mode=list but filters.account_label_ids is empty")
    elif mode == "net_new_search":
        for req in (DEFAULT_REQUIRED if required is None else required):
            if not any(filters.get(k) for k in req.split("|")):
                errors.append(f"required filter missing: {req}")
    else:
        errors.append(f"unknown mode {mode!r}")
    return errors


def load_search(path: Path = SEARCH_JSON) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge_into_search(parsed: dict, existing: dict | None, date: str) -> dict:
    """Pure: new filters/unmapped from the URL, keep the existing pre_filter."""
    existing = existing or {}
    out = dict(existing)
    out.update({k: parsed[k] for k in ("source_url", "mode", "filters", "unmapped", "handled", "notes")})
    out["generated_at"] = date
    out.setdefault("pre_filter", {})
    # A new URL always needs a fresh acknowledgement (set by /search-config via `ack`).
    out["acknowledged_unmapped"] = []
    return out


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _required(settings_path: Path) -> list[str] | None:
    if not settings_path.exists():
        return None
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    return (settings.get("apollo") or {}).get("required_filters")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="translate an Apollo URL")
    p.add_argument("url")
    p.add_argument("--write", action="store_true", help="merge into config/search.json (resets acknowledgements)")
    k = sub.add_parser("ack", help="acknowledge unmapped params so the nightly search may run without them")
    k.add_argument("params", nargs="*", help="param names exactly as listed under unmapped, e.g. 'qPersonTitles[]'")
    k.add_argument("--all", action="store_true")
    r = sub.add_parser("resolve", help="validate config/search.json and print MCP params for a run date")
    r.add_argument("--date", default=dt.date.today().isoformat())
    for sp in (p, k, r):
        sp.add_argument("--search", default=str(SEARCH_JSON))
        sp.add_argument("--settings", default=str(ROOT / "config" / "settings.json"))
    a = ap.parse_args(argv)
    search_path, required = Path(a.search), _required(Path(a.settings))

    if a.cmd == "parse":
        parsed = parse_apollo_url(a.url)
        if a.write:
            existing = load_search(search_path) if search_path.exists() else None
            merged = merge_into_search(parsed, existing, dt.date.today().isoformat())
            _write_json(search_path, merged)
            parsed["validation_errors"] = validate_search(merged, required)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        return 0

    cfg = load_search(search_path)

    if a.cmd == "ack":
        known = [u.get("param") for u in cfg.get("unmapped") or []]
        wanted = known if a.all else a.params
        bad = [x for x in wanted if x not in known]
        if bad:
            print(json.dumps({"ok": False, "errors": [f"not an unmapped param: {x}" for x in bad]}))
            return 2
        acked = list(dict.fromkeys((cfg.get("acknowledged_unmapped") or []) + wanted))
        cfg["acknowledged_unmapped"] = acked
        _write_json(search_path, cfg)
        print(json.dumps({"ok": True, "acknowledged_unmapped": acked,
                          "validation_errors": validate_search(cfg, required)}))
        return 0

    errors = validate_search(cfg, required)
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        return 2
    out = {"ok": True, "mode": cfg.get("mode", "net_new_search"),
           "params": resolve_filters(cfg.get("filters", {}), a.date)}
    ex = exclude_params(out["params"], cfg.get("exclude_keyword_tags") or [])
    if ex:
        out["exclude_params"] = ex
    print(json.dumps(out, ensure_ascii=False))
    return 0


# Parameters apollo_organizations_lookup (free) accepts; the rest are dropped, which
# only makes the exclusion lookup broader (it is intersected with the pool by org id).
LOOKUP_PARAMS = {
    "currently_using_any_of_technology_uids", "latest_funding_amount_range", "latest_funding_date_range",
    "market_segments", "not_organization_naics_codes", "not_organization_sic_codes",
    "organization_department_or_subdepartment_counts", "organization_founded_year_range",
    "organization_headcount_growth_past_n_months", "organization_headcount_growth_range",
    "organization_include_unknown_founded_year", "organization_job_locations", "organization_job_posted_at_range",
    "organization_locations", "organization_naics_codes", "organization_not_locations",
    "organization_num_employees_ranges", "organization_num_jobs_range", "organization_sic_codes",
    "q_organization_job_titles", "revenue_range", "total_funding_range",
}


def exclude_params(params: dict, tags: list[str]) -> dict | None:
    """Pure: free-lookup params that list the pool's companies carrying an excluded keyword tag."""
    if not tags:
        return None
    out = {k: v for k, v in params.items() if k in LOOKUP_PARAMS}
    out["q_organization_keyword_tags"] = list(tags)
    return out


if __name__ == "__main__":
    sys.exit(main())
