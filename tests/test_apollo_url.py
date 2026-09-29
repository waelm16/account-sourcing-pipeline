"""apollo_url.py: Apollo Companies-tab URL -> search parameters. All URLs and values here are made up."""
import json
import subprocess
import sys

import pytest

import apollo_url as au

BASE = "https://app.apollo.io/#/companies?"
SAMPLE = (BASE + "sortAscending=true&sortByField=organization_estimated_number_employees"
          "&organizationLocations[]=Canada&organizationLocations[]=United%20States"
          "&organizationNotLocations[]=Quebec%2C%20Canada"
          "&organizationNumEmployeesRanges[]=1001%2C2000&organizationNumEmployeesRanges[]=2001%2C5000"
          "&organizationNumEmployeesRanges[]=5001%2C10000"
          "&organizationNaicsCodes[]=4451&notOrganizationNaicsCodes[]=4244"
          "&revenueRange[min]=50000000&revenueRange[max]=900000000"
          "&organizationFoundedYearRange[max]=2015"
          "&organizationJobPostedAtRange[min]=60_days_ago"
          "&qOrganizationJobTitles[]=Replenishment%20Analyst"
          "&organizationIndustryTagIds[]=EXAMPLE_INDUSTRY_TAG_ID"
          "&prospectedByCurrentTeam[]=no&page=3")


def _s(x):
    """Compare loosely on type (int vs str) but strictly on value."""
    if isinstance(x, list):
        return [_s(i) for i in x]
    if isinstance(x, dict):
        return {k: _s(v) for k, v in x.items()}
    return str(x)


@pytest.fixture(scope="module")
def cfg():
    return au.parse_apollo_url(SAMPLE)


def unmapped_params(cfg):
    return {u["param"] if isinstance(u, dict) else u for u in cfg["unmapped"]}


def test_top_level_keys(cfg):
    assert {"filters", "unmapped", "mode", "handled", "notes", "source_url"} <= set(cfg)
    assert cfg["mode"] == "net_new_search"
    assert cfg["source_url"] == SAMPLE


def test_list_params(cfg):
    f = cfg["filters"]
    assert _s(f["organization_num_employees_ranges"]) == ["1001,2000", "2001,5000", "5001,10000"]
    assert f["organization_locations"] == ["Canada", "United States"]        # order of the URL is kept
    assert f["organization_not_locations"] == ["Quebec, Canada"]
    assert _s(f["organization_naics_codes"]) == ["4451"]
    assert _s(f["not_organization_naics_codes"]) == ["4244"]
    assert f["q_organization_job_titles"] == ["Replenishment Analyst"]


def test_range_params(cfg):
    f = cfg["filters"]
    assert f["revenue_range"] == {"min": 50000000, "max": 900000000}          # range_int: numbers
    assert f["organization_founded_year_range"] == {"max": 2015}              # one side only
    assert f["organization_job_posted_at_range"] == {"min": "60_days_ago"}    # range_date: kept as text


def test_exactly_the_expected_filters(cfg):
    assert set(cfg["filters"]) == {
        "organization_locations", "organization_not_locations", "organization_num_employees_ranges",
        "organization_naics_codes", "not_organization_naics_codes", "revenue_range",
        "organization_founded_year_range", "organization_job_posted_at_range", "q_organization_job_titles"}


def test_industry_tags_unmapped(cfg):
    hits = [u for u in cfg["unmapped"] if u["param"] == "organizationIndustryTagIds[]"]
    assert len(hits) == 1 and hits[0]["value"] == "EXAMPLE_INDUSTRY_TAG_ID"
    assert hits[0]["reason"] and hits[0]["fallback"]
    assert unmapped_params(cfg) == {"organizationIndustryTagIds[]"}


def test_ui_params_not_filters(cfg):
    f = cfg["filters"]
    for k in ("page", "sortByField", "sortAscending", "sort_by_field", "per_page"):
        assert k not in f, k
    # UI-only params are not reported as untranslatable filters either
    assert not ({"page", "sortByField", "sortAscending"} & unmapped_params(cfg))
    assert {"page", "sortByField", "sortAscending"} <= set(cfg["ignored"])


def test_net_new_flag_is_handled(cfg):
    """prospectedByCurrentTeam=no is the Net New flag: recorded as handled, never dropped silently."""
    assert [h["param"] for h in cfg["handled"]] == ["prospectedByCurrentTeam[]"]


def test_industry_code_adds_a_note(cfg):
    assert cfg["notes"]
    assert au.parse_apollo_url(BASE + "organizationLocations[]=Canada")["notes"] == []


def test_no_camelcase_leaks(cfg):
    for k in cfg["filters"]:
        assert k == k.lower(), k


@pytest.mark.parametrize("q,key,expected", [
    ("qOrganizationKeywordTags[]=grocery&qOrganizationKeywordTags[]=supermarket",
     "q_organization_keyword_tags", ["grocery", "supermarket"]),
    ("currentlyUsingAnyOfTechnologyUids[]=example_pos", "currently_using_any_of_technology_uids", ["example_pos"]),
    ("notOrganizationSicCodes[]=5141&organizationSicCodes[]=5411", "not_organization_sic_codes", ["5141"]),
    ("totalFundingRange[min]=1&totalFundingRange[max]=5", "total_funding_range", {"min": "1", "max": "5"}),
    ("latestFundingDateRange[max]=2019-06-30", "latest_funding_date_range", {"max": "2019-06-30"}),
    ("latestFundingDetectedAtRange[min]=abc", "latest_funding_detected_at_range", {"min": "abc"}),   # range_str
    ("qOrganizationName=Example%20Grocers", "q_organization_name", "Example Grocers"),               # str
    ("organizationHeadcountGrowthPastNMonths=12", "organization_headcount_growth_past_n_months", 12),  # int
    ("revenueRange[min]=%241%2C500%2C000", "revenue_range", {"min": 1500000}),                        # "$1,500,000"
])
def test_more_params(q, key, expected):
    cfg = au.parse_apollo_url(BASE + q)
    assert _s(cfg["filters"][key]) == _s(expected)
    assert cfg["unmapped"] == []


def test_empty_range_value_is_skipped():
    cfg = au.parse_apollo_url(BASE + "revenueRange[min]=&revenueRange[max]=10&totalFundingMin=")
    assert cfg["filters"] == {"revenue_range": {"max": 10}}


def test_range_without_side_is_reported():
    cfg = au.parse_apollo_url(BASE + "revenueRange=10")
    assert cfg["filters"] == {}
    assert cfg["unmapped"][0]["param"] == "revenueRange" and cfg["unmapped"][0]["fallback"]


def test_single_value_bracket_param_is_list():
    cfg = au.parse_apollo_url(BASE + "organizationLocations[]=Canada")
    assert cfg["filters"]["organization_locations"] == ["Canada"]


def test_encoded_brackets():
    url = BASE + "organizationLocations%5B%5D=Canada"
    assert au.parse_apollo_url(url)["filters"]["organization_locations"] == ["Canada"]


def test_unknown_param_reported():
    cfg = au.parse_apollo_url(BASE + "fooBar[]=x&organizationLocations[]=Canada")
    hit = [u for u in cfg["unmapped"] if u["param"] == "fooBar[]"]
    assert hit and hit[0]["reason"] == "unknown Apollo UI parameter"


def test_list_mode_from_account_labels():
    cfg = au.parse_apollo_url(BASE + "accountLabelIds[]=EXAMPLE_LIST_ID")
    assert cfg["mode"] == "list"
    assert cfg["filters"]["account_label_ids"] == ["EXAMPLE_LIST_ID"]


def test_other_tab_is_reported():
    cfg = au.parse_apollo_url("https://app.apollo.io/#/people?personTitles[]=Demand%20Planner")
    assert cfg["filters"] == {}
    assert {"#/people", "personTitles[]"} <= unmapped_params(cfg)


@pytest.mark.parametrize("bad", ["", "not a url", "https://app.apollo.io/#/companies"])
def test_bad_urls_fail_cleanly(bad):
    try:
        cfg = au.parse_apollo_url(bad)
    except (ValueError, KeyError) as e:
        assert str(e)
        return
    # tolerated: must return an empty filter set, never crash
    assert cfg["filters"] == {}


def test_cli_parse_prints_json(repo):
    r = subprocess.run([sys.executable, str(repo / "scripts" / "apollo_url.py"), "parse", SAMPLE],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["filters"]["organization_locations"] == ["Canada", "United States"]


def test_resolve_relative_dates():
    out = au.resolve_filters({"organization_job_posted_at_range": {"min": "30_days_ago", "max": "today"},
                              "latest_funding_date_range": {"min": "2_years_ago"},
                              "revenue_range": {"min": 5}}, "2030-01-15b")           # batch ids resolve too
    assert out["organization_job_posted_at_range"] == {"min": "2029-12-16", "max": "2030-01-15"}
    assert out["latest_funding_date_range"] == {"min": "2028-01-16"}
    assert out["revenue_range"] == {"min": 5}


@pytest.mark.parametrize("q,param", [
    ("prospectedByCurrentTeam[]=yes", "prospectedByCurrentTeam[]"),   # saved-only is not Net New
    ("personTitles[]=Demand%20Planner", "personTitles[]"),            # person-level signal not exposed
    ("qKeywords=fresh%20produce", "qKeywords"),
])
def test_untranslatable_reported_with_fallback(q, param):
    cfg = au.parse_apollo_url(BASE + q)
    hits = [u for u in cfg["unmapped"] if u["param"] == param]
    assert hits and hits[0].get("fallback")
    assert cfg["filters"] == {}


def test_duplicate_values_deduped():
    cfg = au.parse_apollo_url(BASE + "organizationNumEmployeesRanges[]=1001%2C2000"
                                     "&organizationNumEmployeesRanges[]=1001%2C2000")
    assert cfg["filters"]["organization_num_employees_ranges"] == ["1001,2000"]


def test_double_encoded_values_and_flat_ranges():
    url = (BASE + "organizationLocations[]=United%2520States"
           "&organizationNumEmployeesRanges[]=5001%252C10000&latestFundingAmountMax=750000"
           "&qNotOrganizationKeywordTags[]=Wholesale%2520%2526%2520Distribution"
           "&excludedOrganizationKeywordFields[]=name")
    cfg = au.parse_apollo_url(url)
    f = cfg["filters"]
    assert f["organization_locations"] == ["United States"]
    assert f["organization_num_employees_ranges"] == ["5001,10000"]
    assert f["latest_funding_amount_range"] == {"max": 750000}
    params = [u["param"] for u in cfg["unmapped"]]
    assert params == ["qNotOrganizationKeywordTags[]"]
    assert cfg["unmapped"][0]["value"] == "Wholesale & Distribution"


# ---------------------------------------------------------------- readiness check
GOOD = {"source_url": BASE + "x=1", "mode": "net_new_search", "unmapped": [], "acknowledged_unmapped": [],
        "filters": {"organization_naics_codes": ["4451"], "organization_num_employees_ranges": ["1001,2000"],
                    "organization_locations": ["Canada"]}}


def test_validate_accepts_a_complete_search():
    assert au.validate_search(GOOD) == []
    sic = dict(GOOD, filters={**GOOD["filters"], "organization_sic_codes": ["5411"]})
    del sic["filters"]["organization_naics_codes"]
    assert au.validate_search(sic) == []          # 'a|b' = either industry code


def test_validate_refuses_the_shipped_example():
    errors = au.validate_search(dict(GOOD, source_url="EXAMPLE: replace me"))
    assert len(errors) == 1 and "EXAMPLE" in errors[0]


def test_validate_needs_every_unmapped_filter_acknowledged():
    s = dict(GOOD, unmapped=[{"param": "qKeywords", "value": "x", "reason": "r"},
                             {"param": "personTitles[]", "value": "y", "reason": "r"}],
             acknowledged_unmapped=["qKeywords"])
    errors = au.validate_search(s)
    assert len(errors) == 1 and "personTitles[]" in errors[0]
    assert au.validate_search(dict(s, acknowledged_unmapped=["qKeywords", "personTitles[]"])) == []


@pytest.mark.parametrize("missing", ["organization_naics_codes", "organization_num_employees_ranges",
                                     "organization_locations"])
def test_validate_needs_the_base_filters(missing):
    f = {k: v for k, v in GOOD["filters"].items() if k != missing}
    errors = au.validate_search(dict(GOOD, filters=f))
    assert len(errors) == 1 and "required filter missing" in errors[0]
    assert au.validate_search(dict(GOOD, filters=f), required=[]) == []      # settings can relax it


def test_validate_list_mode_and_unknown_mode():
    assert au.validate_search({"source_url": "u", "mode": "list", "filters": {}})
    assert au.validate_search({"source_url": "u", "mode": "list", "filters": {"account_label_ids": ["L1"]}}) == []
    assert au.validate_search(dict(GOOD, mode="everything"))


def test_new_url_resets_acknowledgements_and_keeps_pre_filter():
    existing = {"pre_filter": {"headcount_min": 1001}, "acknowledged_unmapped": ["qKeywords"],
                "exclude_keyword_tags": ["Wholesale"], "filters": {"organization_locations": ["Canada"]}}
    merged = au.merge_into_search(au.parse_apollo_url(SAMPLE), existing, "2030-01-15")
    assert merged["acknowledged_unmapped"] == []
    assert merged["pre_filter"] == {"headcount_min": 1001}
    assert merged["exclude_keyword_tags"] == ["Wholesale"]
    assert merged["filters"]["organization_locations"] == ["Canada", "United States"]
    assert merged["generated_at"] == "2030-01-15"


def test_cli_write_ack_resolve_roundtrip(tmp_path, repo):
    """parse --write, then ack, then resolve, all against files in a temp folder."""
    search, settings = tmp_path / "search.json", tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    def cli(*args):
        r = subprocess.run([sys.executable, str(repo / "scripts" / "apollo_url.py"), *args, "--search", str(search),
                            "--settings", str(settings)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        return r.returncode, json.loads(r.stdout)

    code, out = cli("parse", SAMPLE, "--write")
    assert code == 0 and out["validation_errors"]                       # the industry tag is not acknowledged yet
    code, out = cli("resolve", "--date", "2030-01-15")
    assert code == 2 and out["ok"] is False
    code, out = cli("ack", "not-a-param")
    assert code == 2 and out["ok"] is False
    code, out = cli("ack", "organizationIndustryTagIds[]")
    assert code == 0 and out["validation_errors"] == []
    code, out = cli("resolve", "--date", "2030-01-15")
    assert code == 0 and out["ok"] is True and out["mode"] == "net_new_search"
    assert out["params"]["organization_job_posted_at_range"] == {"min": "2029-11-16"}   # 60 days before the run date
    assert "exclude_params" not in out
