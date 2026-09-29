"""Upsert a Prompt 1 verdict into HubSpot (companies), matched by domain.

Only QUALIFIED verdicts are ever written (HubSpot holds only Qualified accounts).
The P1 verdict uses UPPER_SNAKE enum tokens (contract with the /qualify skill);
this module maps them to the exact HubSpot option strings.

Safety rules:
- search by domain first; create only when nothing matches.
- more than one match -> no write (action "conflict"), reported for the owner.
- an existing record that is already Qualified is never touched at all
  (action "exists"): no downgrade, and later research written to it is never overwritten.
- an existing record with a Needs Review / Not Qualified verdict is not reversed
  unattended (action "human_verdict_exists", flagged in the morning report).
- the Apollo queue row's domain is authoritative; a verdict for another domain
  is rejected. Matching covers domain, www.domain and the website host.
- on other existing records (status empty / Not Reviewed) sourcing_account_stage never moves backwards
  (only empty/"Sourced" -> "Qualified"); name/domain are never renamed.
- enum values are checked against the portal's live property options
  (read-only, once per process); an unknown option fails closed for that record.
- dry_run never calls create/update (a read-only search is still done when a
  client is available).

Token: HUBSPOT_TOKEN env, else keyring("account-sourcing", "HUBSPOT_TOKEN").
Portal id and owner id: HUBSPOT_PORTAL_ID / HUBSPOT_OWNER_ID env, else the
"hubspot" block of config/settings.json (portal_id, owner_id). The shipped
values are placeholders.

CLI (manual use / debugging):
  python scripts/hubspot_writer.py --verdict state/work/<d>/p1.json \
      --apollo state/work/<d>/apollo.json --date 2030-01-15 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- mappings
QUALIFICATION = {
    "QUALIFIED": "Qualified",
    "MAYBE": "Needs Review",
    "NOT_QUALIFIED": "Not Qualified",
}
ACCOUNT_TIER = {"TIER_A": "Tier A", "TIER_B": "Tier B", "TIER_C": "Tier C"}
NEED_STRENGTH = {"STRONG": "Strong", "MODERATE": "Moderate", "WEAK": "Weak", "NOT_FOUND": "Not Found"}
CURRENT_APPROACH = {
    "MANUAL": "Manual",
    "BASIC_TOOLS": "Basic Tools",
    "ADVANCED_SYSTEM": "Advanced System",
    "UNKNOWN": "Unknown",
}
TIMING = {"ACTIVE_TRIGGER": "Active Trigger", "NO_TRIGGER": "No Trigger", "UNKNOWN": "Unknown"}
CONFIDENCE = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}
# Validated but not written to the CRM: only a BUYER can be QUALIFIED.
BUYER_OR_VENDOR = {"BUYER", "VENDOR", "UNCLEAR"}

# verdict key -> (hubspot property, mapping)
ENUM_FIELDS = {
    "account_tier": ("sourcing_account_tier", ACCOUNT_TIER),
    "need_strength": ("sourcing_need_strength", NEED_STRENGTH),
    "current_approach": ("sourcing_current_approach", CURRENT_APPROACH),
    "timing": ("sourcing_timing", TIMING),
    "evidence_confidence": ("sourcing_evidence_confidence", CONFIDENCE),
}
TEXT_FIELDS = {
    "qualification_reason": "sourcing_qualification_reason",
    "strongest_signal": "sourcing_strongest_signal",
    "main_uncertainty": "sourcing_main_uncertainty",
    "roles_to_contact": "sourcing_roles_to_contact",
}
# Keys every ok verdict must carry (contract with /qualify).
REQUIRED_KEYS = (
    ["domain", "name", "status", "qualification", "buyer_or_vendor",
     "buyer_vendor_reason", "evidence_links"]
    + list(ENUM_FIELDS) + list(TEXT_FIELDS)
)

# Existing records are only written when no verdict is on them yet. Needs Review /
# Not Qualified were set by a human (or an earlier run) and are never reversed
# unattended; they are reported as "human_verdict_exists" for the owner.
WRITABLE_STATUSES = {"", "Not Reviewed"}

# Stages at or beyond "Qualified" are never overwritten.
STAGES_BEFORE_QUALIFIED = {"", "Sourced"}

READ_PROPS = ["name", "domain", "sourcing_qualification_status", "sourcing_account_stage",
              "sourcing_p1_date", "sourcing_sourced_from", "sourcing_evidence_links"] \
    + [p for p, _ in ENUM_FIELDS.values()] + list(TEXT_FIELDS.values())

SETTINGS_JSON = Path(__file__).resolve().parent.parent / "config" / "settings.json"


def load_hubspot_ids(env=None, settings_path=None) -> tuple[str, str]:
    """(portal id, owner id): HUBSPOT_PORTAL_ID / HUBSPOT_OWNER_ID from the environment,
    else "hubspot": {"portal_id", "owner_id"} in config/settings.json, else ""."""
    env = os.environ if env is None else env
    cfg = {}
    try:
        with open(settings_path or SETTINGS_JSON, encoding="utf-8") as f:
            cfg = json.load(f).get("hubspot") or {}
    except (OSError, ValueError, AttributeError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    portal = str(env.get("HUBSPOT_PORTAL_ID") or cfg.get("portal_id") or "").strip()
    owner = str(env.get("HUBSPOT_OWNER_ID") or cfg.get("owner_id") or "").strip()
    return portal, owner


# HUBSPOT_PORTAL builds the record links in the report. HUBSPOT_OWNER_ID is the owner
# assigned to new records: without an owner, new records are hidden from
# owner-filtered views such as "My companies". An empty owner id sets no owner.
HUBSPOT_PORTAL, HUBSPOT_OWNER_ID = load_hubspot_ids()


class MappingError(ValueError):
    pass


def normalize_domain(d: str) -> str:
    d = (d or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def validate_verdict(v: dict) -> None:
    """Raise MappingError if an ok verdict breaks the contract."""
    if not isinstance(v, dict):
        raise MappingError("verdict is not an object")
    if v.get("status") != "ok":
        raise MappingError(f"verdict status is {v.get('status')!r}: {v.get('error', '')}")
    missing = [k for k in REQUIRED_KEYS if k not in v]
    if missing:
        raise MappingError(f"missing keys: {missing}")
    # type checks first: a list/dict enum value would otherwise raise TypeError
    str_keys = ["domain", "name", "qualification", "buyer_or_vendor", "buyer_vendor_reason"] \
        + list(ENUM_FIELDS) + list(TEXT_FIELDS)
    wrong = [k for k in str_keys if not isinstance(v[k], str)]
    if wrong:
        raise MappingError(f"non-string values for: {wrong}")
    if not v["domain"].strip():
        raise MappingError("empty domain")
    if v["qualification"] not in QUALIFICATION:
        raise MappingError(f"bad qualification {v['qualification']!r}")
    if v["buyer_or_vendor"] not in BUYER_OR_VENDOR:
        raise MappingError(f"bad buyer_or_vendor {v['buyer_or_vendor']!r}")
    for key, (_, mapping) in ENUM_FIELDS.items():
        if v[key] not in mapping:
            raise MappingError(f"bad {key} {v[key]!r}")
    if not isinstance(v["evidence_links"], list):
        raise MappingError("evidence_links must be a list")


def _to_int(x):
    try:
        return int(float(str(x).replace(",", "")))
    except (TypeError, ValueError):
        return None


def build_properties(verdict: dict, apollo_row: dict | None, date: str) -> dict:
    """Pure mapping: verdict + Apollo queue row -> HubSpot company properties.

    Raises MappingError for anything that is not a well-formed QUALIFIED verdict.
    """
    validate_verdict(verdict)
    if verdict["qualification"] != "QUALIFIED":
        raise MappingError("only QUALIFIED verdicts are written to HubSpot")
    if verdict["buyer_or_vendor"] != "BUYER":
        raise MappingError("QUALIFIED requires BUYER")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise MappingError(f"bad date {date!r}")
    apollo_row = apollo_row or {}
    # The queue row (Apollo) is authoritative; the LLM's domain must agree.
    domain = normalize_domain(apollo_row.get("domain") or verdict.get("domain", ""))
    if not domain:
        raise MappingError("no domain")
    if normalize_domain(verdict.get("domain", "")) != domain:
        raise MappingError(f"domain mismatch: verdict {verdict.get('domain')!r} vs queue {domain!r}")

    props = {
        "name": (verdict.get("name") or apollo_row.get("name") or domain).strip(),
        "domain": domain,
        "website": f"https://{domain}",
        "sourcing_qualification_status": QUALIFICATION["QUALIFIED"],
        "sourcing_account_stage": "Qualified",
        "sourcing_sourced_from": "Apollo",
        "sourcing_p1_date": date,
    }
    if HUBSPOT_OWNER_ID:
        props["hubspot_owner_id"] = HUBSPOT_OWNER_ID
    for key, (prop, mapping) in ENUM_FIELDS.items():
        props[prop] = mapping[verdict[key]]
    for key, prop in TEXT_FIELDS.items():
        val = verdict[key].strip()
        if val:
            props[prop] = val
    links = [str(u).strip() for u in verdict["evidence_links"] if str(u).strip()]
    if links:
        props["sourcing_evidence_links"] = " | ".join(links)
    emp = _to_int(apollo_row.get("headcount"))
    if emp:
        props["numberofemployees"] = str(emp)
    # NOTE: HubSpot "industry" is a fixed enumeration (e.g. SUPERMARKETS);
    # Apollo's free-text industry would be rejected, so it is not written.
    return props


# ---------------------------------------------------------------- client
def get_client():
    """Real HubSpot client; returns None when no token is configured."""
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        try:
            import keyring
            token = keyring.get_password("account-sourcing", "HUBSPOT_TOKEN")
        except Exception:
            token = None
    if not token:
        return None
    from hubspot import HubSpot
    return HubSpot(access_token=token)


def _host(url: str) -> str:
    return normalize_domain(url or "")


def _search_by_domain(client, domain: str) -> list:
    """Records whose domain or website host is `domain` (also matches www. and website-only records)."""
    from hubspot.crm.companies import PublicObjectSearchRequest
    groups = [
        {"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]},
        {"filters": [{"propertyName": "domain", "operator": "EQ", "value": "www." + domain}]},
        {"filters": [{"propertyName": "website", "operator": "CONTAINS_TOKEN", "value": domain}]},
    ]
    req = PublicObjectSearchRequest(filter_groups=groups, properties=READ_PROPS + ["website"], limit=10)
    res = client.crm.companies.search_api.do_search(public_object_search_request=req)
    out, seen = [], set()
    for r in res.results or []:
        p = r.properties or {}
        if r.id in seen:
            continue
        if _host(p.get("domain")) == domain or _host(p.get("website")) == domain:
            seen.add(r.id)
            out.append(r)
    return out


def plan_update(existing: dict, props: dict) -> dict:
    """Given the existing record's properties, return the subset of props to write.

    Never downgrades Qualified, never moves stage backwards, never renames.
    """
    existing = {k: (v or "") for k, v in (existing or {}).items()}
    out = {}
    if existing.get("sourcing_qualification_status", "") not in WRITABLE_STATUSES:
        return {}  # a verdict already exists (human or earlier run): never touched
    for k, v in props.items():
        if k in ("name", "domain", "website"):
            continue  # keep the record's identity as it is in the CRM
        if k == "sourcing_account_stage":
            if existing.get("sourcing_account_stage", "") in STAGES_BEFORE_QUALIFIED:
                out[k] = v
            continue
        if k in ("sourcing_sourced_from", "numberofemployees", "hubspot_owner_id"):
            if not existing.get(k):
                out[k] = v
            continue
        if existing.get(k) != v:
            out[k] = v
    return out


_LIVE_OPTIONS: dict | None = None


def live_enum_options(client) -> dict:
    """{property: set(option values)} for the enum properties we write (read-only, cached)."""
    global _LIVE_OPTIONS
    if _LIVE_OPTIONS is None:
        props = [p for p, _ in ENUM_FIELDS.values()] + ["sourcing_qualification_status",
                                                        "sourcing_account_stage", "sourcing_sourced_from"]
        out = {}
        for name in props:
            prop = client.crm.properties.core_api.get_by_name(object_type="companies", property_name=name)
            out[name] = {o.value for o in (prop.options or [])}
        _LIVE_OPTIONS = out
    return _LIVE_OPTIONS


def check_enums(props: dict, options: dict) -> list:
    """Return a list of 'prop=value' strings that are not valid live options. A property whose
    live option list is empty or could not be read has no valid value: fail closed."""
    bad = []
    for name, allowed in options.items():
        if name in props and props[name] not in (allowed or ()):
            bad.append(f"{name}={props[name]!r}")
    return bad


def portal_url(company_id) -> str:
    # Record link for a portal hosted in na2; change the host if your portal is in another region.
    if not company_id or not HUBSPOT_PORTAL:
        return ""
    return f"https://app-na2.hubspot.com/contacts/{HUBSPOT_PORTAL}/record/0-2/{company_id}"


def upsert(client, domain: str, props: dict, dry_run: bool = False) -> dict:
    """Create or update the company for `domain`.

    Returns {"action": created|updated|exists|human_verdict_exists|unchanged|conflict|invalid|dry_run,
             "id", "url", "props", "note"}.
    With dry_run the search still runs when a client is given (read-only), but
    nothing is created or updated. client=None with dry_run -> no network at all.
    """
    domain = normalize_domain(domain)
    if client is None:
        if dry_run:
            return {"action": "dry_run", "id": None, "url": "", "props": props,
                    "note": "no HubSpot client: lookup skipped"}
        raise RuntimeError("HubSpot token not found (HUBSPOT_TOKEN env or keyring account-sourcing/HUBSPOT_TOKEN)")

    bad = check_enums(props, live_enum_options(client))
    if bad:
        return {"action": "invalid", "id": None, "url": "", "props": props,
                "note": "values not in live HubSpot options: " + ", ".join(bad)}
    matches = _search_by_domain(client, domain)
    if len(matches) > 1:
        ids = [m.id for m in matches]
        return {"action": "conflict", "id": None, "url": "", "props": props,
                "note": f"{len(matches)} companies share domain {domain}: {ids}; not written"}
    if not matches:
        if dry_run:
            return {"action": "dry_run", "id": None, "url": "", "props": props, "note": "would create"}
        from hubspot.crm.companies import SimplePublicObjectInputForCreate
        created = client.crm.companies.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props))
        return {"action": "created", "id": created.id, "url": portal_url(created.id), "props": props, "note": ""}

    rec = matches[0]
    status = (rec.properties or {}).get("sourcing_qualification_status") or ""
    if status == "Qualified":
        return {"action": "exists", "id": rec.id, "url": portal_url(rec.id), "props": {},
                "note": "already Qualified in HubSpot; not touched"}
    if status not in WRITABLE_STATUSES:
        return {"action": "human_verdict_exists", "id": rec.id, "url": portal_url(rec.id), "props": {},
                "note": f"HubSpot already says {status!r}; P1 now says Qualified. Owner to decide."}
    to_write = plan_update(rec.properties or {}, props)
    if not to_write:
        return {"action": "unchanged", "id": rec.id, "url": portal_url(rec.id), "props": {}, "note": "nothing to change"}
    if dry_run:
        return {"action": "dry_run", "id": rec.id, "url": portal_url(rec.id), "props": to_write,
                "note": f"would update existing {rec.id}"}
    from hubspot.crm.companies import SimplePublicObjectInput
    client.crm.companies.basic_api.update(
        company_id=rec.id, simple_public_object_input=SimplePublicObjectInput(properties=to_write))
    return {"action": "updated", "id": rec.id, "url": portal_url(rec.id), "props": to_write, "note": ""}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verdict", required=True, help="p1.json (verdict object)")
    ap.add_argument("--apollo", help="apollo.json (queue row)")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD for sourcing_p1_date")
    ap.add_argument("--dry-run", action="store_true", help="print payload, never create/update")
    a = ap.parse_args(argv)
    with open(a.verdict, encoding="utf-8") as f:
        verdict = json.load(f)
    row = {}
    if a.apollo:
        with open(a.apollo, encoding="utf-8") as f:
            row = json.load(f)
    try:
        props = build_properties(verdict, row, a.date)
    except MappingError as e:
        print(json.dumps({"action": "skipped", "note": str(e)}))
        return 2
    res = upsert(get_client(), props["domain"], props, dry_run=a.dry_run)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
