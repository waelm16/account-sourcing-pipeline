import apollo_url as au
import source_queue as sq

ROWS = [{"org_id": "a" * 24, "name": "Northfield Grocers", "domain": "northfield.example"},
        {"org_id": "b" * 24, "name": "Tri-County Supply", "domain": "tricounty.example"},
        {"org_id": "c" * 24, "name": "Delta Cash and Carry", "domain": "delta.example"},
        {"org_id": "d" * 24, "name": "Lakeside Fresh", "domain": "lakeside.example"}]


def test_select_marks_excluded_keyword_tag():
    ex = {"org_ids": {"b" * 24}, "domains": {"delta.example"}}
    res = sq.select(ROWS, {}, cap=10, seen_domains=set(), excluded=ex)
    status = {c["domain"]: (c["status"], c["skip_reason"]) for c in res["candidates"]}
    assert status["tricounty.example"] == ("skipped_prefilter", "excluded_keyword_tag")
    assert status["delta.example"] == ("skipped_prefilter", "excluded_keyword_tag")
    assert status["northfield.example"][0] == status["lakeside.example"][0] == "pending"
    assert res["pending"] == 2


def test_excluded_do_not_count_toward_cap():
    ex = {"org_ids": {"a" * 24, "b" * 24}, "domains": set()}
    res = sq.select(ROWS, {}, cap=1, seen_domains=set(), excluded=ex)
    assert [c["domain"] for c in res["candidates"] if c["status"] == "pending"] == ["delta.example"]


def test_no_exclusions_is_unchanged():
    assert sq.select(ROWS, {}, cap=10, seen_domains=set())["pending"] == 4


def test_exclude_params_keeps_only_lookup_keys():
    params = {"organization_locations": ["United States"], "organization_naics_codes": ["4451"],
              "organization_include_unknown_founded_year": True, "latest_funding_detected_at_range": {"min": "x"}}
    ex = au.exclude_params(params, ["Wholesale & Distribution"])
    assert ex["q_organization_keyword_tags"] == ["Wholesale & Distribution"]
    assert "latest_funding_detected_at_range" not in ex
    assert ex["organization_include_unknown_founded_year"] is True
    assert au.exclude_params(params, []) is None
