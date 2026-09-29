"""Cross-boundary contracts: config files, skills, prompt footer, driver, writer, Apollo MCP schema."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import apollo_url as au
import hubspot_writer as hw
import run_nightly as rn
from contract_spec import ACCEPTED_NOT_ASKED, VERDICT_ENUMS, VERDICT_KEYS

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / ".claude" / "skills"

# Properties of mcp__claude_ai_Apollo_io__apollo_mixed_companies_search (as read when this test was written; additionalProperties: false)
APOLLO_SEARCH_PARAMS = {
    "account_label_ids", "currently_using_any_of_technology_uids", "latest_funding_amount_range",
    "latest_funding_date_range", "latest_funding_detected_at_range", "market_segments", "not_organization_naics_codes",
    "not_organization_sic_codes", "organization_department_or_subdepartment_counts", "organization_founded_year_range",
    "organization_headcount_growth_past_n_months", "organization_headcount_growth_range", "organization_ids",
    "organization_include_unknown_founded_year", "organization_job_locations", "organization_job_posted_at_range",
    "organization_locations", "organization_naics_codes", "organization_not_locations",
    "organization_num_employees_ranges", "organization_num_jobs_range", "organization_sic_codes", "page", "per_page",
    "q_organization_domains_list", "q_organization_job_titles", "q_organization_keyword_tags", "q_organization_name",
    "revenue_range", "show_new_companies_only", "total_funding_range", "web_page_view_counts",
    "website_visitors_domain_exact_pages", "website_visitors_domain_pages", "website_visitors_from_domains",
    "website_visitors_from_past", "website_visitors_intent",
}


# Tool names of the claude.ai Apollo connector (so field names like apollo_account_id are not mistaken for tools)
APOLLO_TOOLS = {
    "apollo_accounts_bulk_create", "apollo_accounts_create", "apollo_accounts_update", "apollo_contacts_bulk_create",
    "apollo_contacts_create", "apollo_contacts_search", "apollo_contacts_update", "apollo_fields_create",
    "apollo_fields_index", "apollo_fields_update", "apollo_labels_add_entity_ids_to_label_names", "apollo_labels_create",
    "apollo_labels_index", "apollo_labels_remove_entity_ids_from_label_names", "apollo_labels_update",
    "apollo_mixed_companies_search", "apollo_mixed_people_api_search", "apollo_organizations_bulk_enrich",
    "apollo_organizations_enrich", "apollo_organizations_lookup", "apollo_organizations_job_postings",
    "apollo_people_bulk_match", "apollo_people_match", "apollo_usage_stats_credit_usage_stats",
}


def skill(name):
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def frontmatter_args(text):
    m = re.search(r"^arguments:\s*\[(.*?)\]", text, re.M)
    return [a.strip() for a in m.group(1).split(",")] if m else []


# ---------------------------------------------------------------- encoding
@pytest.mark.parametrize("rel", ["config/settings.json", "config/search.json", "config/signals.json",
                                 ".claude/settings.json", ".mcp.json", "prompts/p1_verdict.schema.json",
                                 "tests/fixtures/claude_result_sample.json", "tests/fixtures/job_feed.json"])
def test_json_files_plain_utf8_no_bom(rel):
    raw = (REPO / rel).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} has a UTF-8 BOM (json.load(encoding='utf-8') fails)"
    json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- settings.json
def test_every_settings_key_is_read_somewhere():
    s = json.loads((REPO / "config" / "settings.json").read_text(encoding="utf-8"))
    corpus = "".join(p.read_text(encoding="utf-8") for p in (REPO / "scripts").glob("*.py"))
    corpus += "".join(p.read_text(encoding="utf-8") for p in SKILLS.glob("*/SKILL.md"))
    unused = []
    for k, v in s.items():
        if k.startswith("_"):
            continue
        if k not in corpus:
            unused.append(k)
        if isinstance(v, dict):
            unused += [f"{k}.{kk}" for kk in v if not kk.startswith("_") and kk not in corpus]
    assert not unused, f"settings keys nobody reads: {unused}"


def test_driver_settings_have_values():
    s = rn.load_settings(REPO)
    for k in ("per_run_cap", "model", "max_attempts", "run_deadline_min", "timeout_qualify_s", "timeout_source_s",
              "timeout_source_pack_s", "timeout_register_s", "max_consecutive_errors", "max_consecutive_transient"):
        assert s.get(k) not in (None, ""), k


# ---------------------------------------------------------------- Apollo
def test_param_map_targets_exist_in_mcp_schema():
    missing = {mcp for mcp, _ in au.PARAM_MAP.values()} - APOLLO_SEARCH_PARAMS
    assert not missing, missing


def test_search_json_filters_exist_in_mcp_schema():
    s = json.loads((REPO / "config" / "search.json").read_text(encoding="utf-8"))
    assert set(s.get("filters", {})) <= APOLLO_SEARCH_PARAMS


def test_example_search_json_refused(tmp_path):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "apollo_url.py"), "resolve", "--date", "2030-01-15"],
                       capture_output=True, text=True, encoding="utf-8", cwd=REPO, timeout=60)
    s = json.loads((REPO / "config" / "search.json").read_text(encoding="utf-8"))
    assert str(s["source_url"]).startswith("EXAMPLE"), "the shipped search must be the example, never a real one"
    out = json.loads(r.stdout)
    assert r.returncode == 2 and out["ok"] is False and "EXAMPLE" in out["errors"][0]


def test_no_apollo_wildcard_or_spend_tools_allowed():
    cfg = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow = cfg["permissions"]["allow"]
    for a in allow:
        assert not re.match(r"mcp__(claude_ai_Apollo_io|apollo)(__\*)?$", a), a
        assert "*" not in a.split("__")[-1] if a.startswith("mcp__") else True, a
        low = a.lower()
        for bad in ("enrich", "emailer", "sequence", "people_", "contacts_", "fields_create", "phone_calls"):
            assert bad not in low, a
    assert not any(a.startswith("mcp__claude_ai_HubSpot") or a == "mcp__hubspot" for a in allow)


def test_enrich_tools_granted_only_to_enrich_step():
    a = rn.enrich_args("opus", {})
    allowed = a[a.index("--allowedTools") + 1].split(",")
    assert all(t.endswith("apollo_organizations_bulk_enrich") or "scripts/enrich.py" in t for t in allowed)
    for other in (rn.source_args("2030-01-17", 5, "opus", {}), rn.register_args("2030-01-17", "opus", {})):
        assert not any("enrich" in x for x in other)


def test_source_side_disallows_outbound_tools():
    dis = set(rn.DEFAULT_SOURCE_DISALLOWED)
    for t in ("mcp__claude_ai_HubSpot", "mcp__claude_ai_Gmail", "mcp__claude_ai_Smartlead_mcp", "WebFetch"):
        assert t in dis


FORBIDDEN_WORDS = ("over" + "ride", "manda" + "tory")   # spelled in two parts so that this file passes its own check


def test_no_skill_claims_to_overrule_a_vendor_confirmation():
    """Credit spend is authorised in advance, in writing, with a ceiling read from the settings."""
    for name in ("source", "enrich"):
        text = skill(name)
        low = text.lower()
        assert not any(w in low for w in FORBIDDEN_WORDS), name
        assert "given in advance by the account owner" in low and "config/settings.json" in text, name
    assert "`apollo.max_search_pages`" in skill("source")
    assert "`apollo.max_enrich_per_call`" in skill("enrich")
    s = rn.load_settings(REPO)["apollo"]
    assert isinstance(s["max_search_pages"], int) and s["max_search_pages"] > 0
    assert isinstance(s["max_enrich_per_call"], int) and s["max_enrich_per_call"] > 0
    assert FORBIDDEN_WORDS[0] not in (REPO / "tests" / "LIVE_CHECKLIST.md").read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("name", ["source", "register-results", "search-config"])
def test_skill_python_commands_exist_and_are_allowed(name):
    text = skill(name)
    allow = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
    bash_prefixes = [a[5:-1].rstrip(":*") for a in allow if a.startswith("Bash(")]
    for m in re.finditer(r"\.venv/Scripts/python\.exe scripts/([a-z_]+)\.py", text):
        assert (REPO / "scripts" / f"{m.group(1)}.py").exists(), m.group(0)
        assert any(m.group(0).startswith(p) for p in bash_prefixes), f"{name}: '{m.group(0)}' not in Bash allow list"


def test_skill_apollo_tools_are_allowed():
    allow = set(json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"])
    for name in ("source", "register-results"):
        text = skill(name)
        for tool in set(re.findall(r"`(apollo_[a-z_]+)`", text)) & APOLLO_TOOLS:
            if tool in ("apollo_fields_create", "apollo_fields_update"):
                continue  # mentioned as forbidden
            assert f"mcp__claude_ai_Apollo_io__{tool}" in allow, f"{name} uses {tool} but it is not allowed"


# ---------------------------------------------------------------- driver <-> skills invocation
def test_source_invocation_matches_skill_args():
    args = frontmatter_args(skill("source"))
    a = rn.source_args("2030-01-15", 7, "opus", rn.load_settings(REPO))
    prompt = a[a.index("-p") + 1].split()
    assert prompt[0] == "/source" and len(prompt) - 1 == len(args) == 2


def test_register_invocation_matches_skill_args():
    args = frontmatter_args(skill("register-results"))
    a = rn.register_args("2030-01-15", "opus", rn.load_settings(REPO))
    prompt = a[a.index("-p") + 1].split()
    assert prompt[0] == "/register-results" and len(prompt) - 1 == len(args) == 1


def test_qualify_invocation_matches_skill_args(tmp_path):
    text = skill("qualify")
    args = frontmatter_args(text)
    a = rn.qualify_args("acme.example", "Acme Corp", "opus", rn.load_settings(REPO), root=tmp_path)
    p = a[a.index("-p") + 1]
    assert p.startswith("/qualify acme.example ")
    assert len(args) == 2, args


def test_source_skill_never_writes_queue_directly():
    text = re.sub(r"\s+", " ", skill("source").lower())
    assert "never write state/queue" in text


# ---------------------------------------------------------------- verdict footer
def _p1_corpus():
    parts = []
    for rel in ("prompts/prompt1.md", ".claude/skills/qualify/SKILL.md"):   # the schema has its own tests
        p = REPO / rel
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_writer_required_keys_equal_contract():
    assert set(hw.REQUIRED_KEYS) == VERDICT_KEYS


def test_writer_enum_maps_equal_contract():
    for k, (_, mapping) in hw.ENUM_FIELDS.items():
        assert set(mapping) == VERDICT_ENUMS[k], k
    assert set(hw.QUALIFICATION) == VERDICT_ENUMS["qualification"]
    assert hw.BUYER_OR_VENDOR == VERDICT_ENUMS["buyer_or_vendor"]
    assert set(hw.ENUM_FIELDS) | {"status", "qualification", "buyer_or_vendor"} == set(VERDICT_ENUMS)


def test_prompt_footer_names_every_key_and_token():
    text = _p1_corpus()
    missing_keys = [k for k in VERDICT_KEYS if k not in text]
    missing_tokens = [f"{k}={t}" for k, ts in VERDICT_ENUMS.items() if k != "status" for t in ts if t not in text]
    assert not missing_keys, missing_keys
    assert not missing_tokens, missing_tokens


def test_json_schema_matches_contract():
    sch = json.loads((REPO / "prompts" / "p1_verdict.schema.json").read_text(encoding="utf-8"))

    def find_props(s):
        if isinstance(s, dict):
            if "properties" in s and "qualification" in s["properties"]:
                return s["properties"]
            for v in s.values():
                r = find_props(v)
                if r:
                    return r
        if isinstance(s, list):
            for v in s:
                r = find_props(v)
                if r:
                    return r
        return None
    props = find_props(sch)
    assert props, "schema has no object with a qualification property"
    assert VERDICT_KEYS <= set(props), VERDICT_KEYS - set(props)
    for k, allowed in VERDICT_ENUMS.items():
        enum = props[k].get("enum")
        if k == "status" and enum is None:
            continue
        assert enum is not None, k
        assert set(enum) <= allowed | {"error"}, (k, enum)
        asked = allowed - {"error"} - ACCEPTED_NOT_ASKED.get(k, set())
        assert asked <= set(enum) or k == "status", (k, enum)
        assert not (ACCEPTED_NOT_ASKED.get(k, set()) & set(enum)), (k, enum)   # the schema offers a binary verdict


def test_shipped_schema_is_the_generated_one():
    """prompts/p1_verdict.schema.json must be regenerated (scripts/verdict_schema.py --write) when the contract changes."""
    import verdict_schema as vs
    shipped = (REPO / "prompts" / "p1_verdict.schema.json").read_text(encoding="utf-8")
    assert shipped == vs.render()
    sch = json.loads(shipped)
    assert set(sch["required"]) == VERDICT_KEYS == set(sch["properties"])
    assert sch["additionalProperties"] is False
    assert sch["properties"]["qualification"]["enum"] == ["QUALIFIED", "NOT_QUALIFIED"]


def test_prompt_placeholders_filled_by_skill():
    p = (REPO / "prompts" / "prompt1.md").read_text(encoding="utf-8")
    s = skill("qualify")
    for ph in ("{{COMPANY}}", "{{DOMAIN}}", "{{APOLLO_RECORD}}"):
        assert ph in p, ph
        assert ph in s, ph
    leftovers = set(re.findall(r"\{\{[A-Z_]+\}\}", p)) - {"{{COMPANY}}", "{{DOMAIN}}", "{{APOLLO_RECORD}}"}
    assert not leftovers, leftovers


def test_qualify_skill_tools_subset_of_driver_tools():
    s = skill("qualify")
    tools = set(rn.DEFAULT_QUALIFY_TOOLS.split(","))
    for t in ("Read", "Glob", "WebSearch"):
        if re.search(rf"\b{t}\b", s):
            assert t in tools, t
    assert "jobs.txt" in s and "Never Read `jobs.json`" in s


def test_qualify_skill_reads_files_driver_writes():
    s = skill("qualify")
    assert "state/work/$domain/apollo.json" in s          # driver writes apollo.json before /qualify
    assert "sources/index.txt" in s                         # source_pack --light default output


# ---------------------------------------------------------------- what each session type can do
# The README and docs/system-design.md claim:
#   /qualify reads files and searches the web. It has no shell, no file writes, no Apollo, no CRM, and
#   no page-fetch tool unless crawler.session_page_fetch is on (no stealth tool unless that is on too).
#   /source, /register-results and /enrich use named Apollo tools and the repo's own scripts. They
#   have no web search, no page fetching of any kind, no crawler, and no other connector.
# These tests derive each session's tool list from the project lists and the driver's flags and compare.
# They test configuration. What the real CLI does with it is checked by hand (LIVE_CHECKLIST.md, section E).
PROJECT = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
APOLLO_ALLOWED = ["apollo_accounts_bulk_create", "apollo_accounts_update", "apollo_fields_index",
                  "apollo_labels_add_entity_ids_to_label_names", "apollo_labels_index",
                  "apollo_mixed_companies_search", "apollo_organizations_lookup"]
APOLLO_SESSION = sorted([f"mcp__claude_ai_Apollo_io__{t}" for t in APOLLO_ALLOWED]
                        + [f"mcp__apollo__{t}" for t in APOLLO_ALLOWED]
                        + ["Read", "Edit(./state/**)", "Write(./state/**)",
                           "Bash(.venv/Scripts/python.exe scripts/apollo_url.py:*)",
                           "Bash(.venv/Scripts/python.exe scripts/source_queue.py:*)"])
ENRICH_EXTRA = ["mcp__claude_ai_Apollo_io__apollo_organizations_bulk_enrich",
                "mcp__apollo__apollo_organizations_bulk_enrich",
                "Bash(.venv/Scripts/python.exe scripts/enrich.py:*)"]
PAGE_FETCH = ["mcp__scrapling__bulk_fetch", "mcp__scrapling__bulk_get", "mcp__scrapling__fetch",
              "mcp__scrapling__make_request"]
WEB_AND_OUTBOUND = ["WebSearch", "WebFetch", "PowerShell", "mcp__scrapling__fetch", "mcp__scrapling__make_request",
                    "mcp__scrapling__stealthy_fetch", "Bash(.venv/Scripts/python.exe scripts/source_pack.py:*)",
                    "mcp__claude_ai_HubSpot__manage_crm_objects", "mcp__hubspot__anything",
                    "mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Notion__anything",
                    "mcp__claude_ai_Apollo_io__apollo_sequences_create",
                    "mcp__claude_ai_Apollo_io__apollo_emailer_messages_send_now"]


def _covers(rule, tool):
    """A rule names a tool, or a whole MCP server (mcp__server covers mcp__server__tool)."""
    return tool == rule or tool.startswith(rule + "__")


def _flag(args, name):
    return args[args.index(name) + 1].split(",") if name in args else []


def session_tools(kind, settings=None, extra_project_allow=(), tmp=None, strict=True):
    settings = dict(rn.load_settings(REPO), **(settings or {}))
    allow = list(PROJECT["permissions"]["allow"]) + list(extra_project_allow)
    deny = list(PROJECT["permissions"]["deny"])
    if kind == "qualify":
        a = rn.qualify_args("acme.example", "Acme", "opus", settings, root=tmp)
        assert "--strict-mcp-config" in a and a[a.index("--permission-mode") + 1] == "dontAsk"
        builtin = set(_flag(a, "--tools"))                     # every other built-in tool is removed
        mcp = json.loads(Path(a[a.index("--mcp-config") + 1]).read_text(encoding="utf-8"))["mcpServers"]
        servers = {f"mcp__{s}" for s in mcp}                    # strict config: no other MCP server is loaded
        if strict:
            allow = [t for t in allow + _flag(a, "--allowedTools")
                     if t.split("(")[0] in builtin or any(_covers(s, t) for s in servers)]
        else:                                                   # as if the strict configuration did nothing
            allow = [t for t in allow + _flag(a, "--allowedTools") if t.split("(")[0] in builtin or t.startswith("mcp__")]
        deny += _flag(a, "--disallowedTools")
    else:
        a = {"source": rn.source_args("2030-01-15", 5, "opus", settings),
             "register-results": rn.register_args("2030-01-15", "opus", settings),
             "enrich": rn.enrich_args("opus", settings)}[kind]
        assert a[a.index("--permission-mode") + 1] == "dontAsk"   # a tool that is not allowed is refused, not asked about
        allow += _flag(a, "--allowedTools")
        deny += _flag(a, "--disallowedTools")
    return sorted(t for t in set(allow) if not any(_covers(d, t) for d in deny))


def test_qualify_session_tools(tmp_path):
    assert session_tools("qualify", tmp=tmp_path) == ["Glob", "Grep", "Read", "WebSearch"]


def test_qualify_session_tools_with_page_fetch(tmp_path):
    on = {"crawler": {"session_page_fetch": True}}
    assert session_tools("qualify", on, tmp=tmp_path) == sorted(["Glob", "Grep", "Read", "WebSearch"] + PAGE_FETCH)
    both = {"crawler": {"session_page_fetch": True, "stealth_fallback": True}}
    assert session_tools("qualify", both, tmp=tmp_path) == sorted(
        ["Glob", "Grep", "Read", "WebSearch", "mcp__scrapling__stealthy_fetch"] + PAGE_FETCH)
    # the stealth switch alone gives the session nothing
    assert session_tools("qualify", {"crawler": {"stealth_fallback": True}}, tmp=tmp_path) == \
        ["Glob", "Grep", "Read", "WebSearch"]


def test_qualify_session_cannot_be_widened_by_the_project_list(tmp_path):
    extra = WEB_AND_OUTBOUND + ["Bash", "Write", "Edit", "mcp__claude_ai_Apollo_io__apollo_mixed_companies_search"]
    assert session_tools("qualify", extra_project_allow=extra, tmp=tmp_path) == ["Glob", "Grep", "Read", "WebSearch"]


def test_qualify_is_kept_from_apollo_and_connectors_by_two_layers(tmp_path):
    """First layer: the strict MCP configuration loads no connector. Second layer: the driver names
    Apollo and every other connector in --disallowedTools. Either one alone must be enough."""
    a = rn.qualify_args("acme.example", "Acme", "opus", rn.load_settings(REPO), root=tmp_path)
    dis = _flag(a, "--disallowedTools")
    assert "mcp__claude_ai_Apollo_io" in dis and "mcp__apollo" in dis
    for t in rn.DEFAULT_SOURCE_DISALLOWED:
        if t.startswith("mcp__"):
            assert any(_covers(d, t) for d in dis), t
    assert "mcp__scrapling" in dis                              # page fetch is off in the shipped settings
    # the project list grants Apollo tools; without the second layer they would reach the session
    granted = [t for t in PROJECT["permissions"]["allow"] if t.startswith("mcp__") and "apollo" in t.lower()]
    assert len(granted) == 14
    extra = WEB_AND_OUTBOUND + ["mcp__claude_ai_Smartlead_mcp__anything", "mcp__claude_ai_Scrubby__anything"]
    for strict in (True, False):
        assert session_tools("qualify", extra_project_allow=extra, tmp=tmp_path, strict=strict) == \
            ["Glob", "Grep", "Read", "WebSearch"], strict


def test_qualify_deny_list_follows_the_page_fetch_switches(tmp_path):
    def dis(settings):
        a = rn.qualify_args("acme.example", "Acme", "opus", settings, root=tmp_path)
        return _flag(a, "--disallowedTools")
    assert "mcp__scrapling" in dis({})
    on = dis({"crawler": {"session_page_fetch": True}})
    assert "mcp__scrapling" not in on and "mcp__scrapling__stealthy_fetch" in on
    both = dis({"crawler": {"session_page_fetch": True, "stealth_fallback": True}})
    assert not any(d.startswith("mcp__scrapling") for d in both)
    # a settings list can add to the refusals but cannot take Apollo out
    custom = dis({"qualify_disallowed_tools": ["mcp__claude_ai_Gmail"]})
    assert {"mcp__claude_ai_Gmail", "mcp__claude_ai_Apollo_io", "mcp__apollo", "mcp__scrapling"} <= set(custom)
    for strict in (True, False):
        assert session_tools("qualify", {"crawler": {"session_page_fetch": True}}, tmp=tmp_path, strict=strict,
                             extra_project_allow=["mcp__scrapling__stealthy_fetch"]) == \
            sorted(["Glob", "Grep", "Read", "WebSearch"] + PAGE_FETCH), strict


@pytest.mark.parametrize("kind", ["source", "register-results"])
def test_apollo_session_tools(kind):
    assert session_tools(kind) == APOLLO_SESSION


def test_enrich_session_tools():
    assert session_tools("enrich") == sorted(APOLLO_SESSION + ENRICH_EXTRA)


@pytest.mark.parametrize("kind", ["source", "register-results", "enrich"])
def test_apollo_sessions_have_no_web_even_if_the_project_list_grants_it(kind):
    """The driver's own flags refuse the web and the other connectors, whatever the project list says."""
    expected = APOLLO_SESSION + (ENRICH_EXTRA if kind == "enrich" else [])
    assert session_tools(kind, extra_project_allow=WEB_AND_OUTBOUND + ["mcp__scrapling"]) == sorted(expected)


def test_project_list_grants_no_web_and_no_crawler():
    allow = PROJECT["permissions"]["allow"]
    assert not any(a.startswith("mcp__scrapling") or "source_pack" in a or a in ("WebSearch", "WebFetch", "Bash")
                   for a in allow), allow
    assert PROJECT.get("enabledMcpjsonServers") == []          # no MCP server is loaded into every session
    assert "WebFetch" in PROJECT["permissions"]["deny"]


def test_driver_refuses_the_crawler_and_the_page_fetcher_for_apollo_sessions():
    dis = rn.DEFAULT_SOURCE_DISALLOWED
    for t in ("WebSearch", "WebFetch", "PowerShell", "mcp__scrapling",
              "Bash(.venv/Scripts/python.exe scripts/source_pack.py:*)"):
        assert t in dis, t
    for args in (rn.source_args("2030-01-15", 5, "opus", {}), rn.register_args("2030-01-15", "opus", {}),
                 rn.enrich_args("opus", {})):
        assert _flag(args, "--disallowedTools") == dis
