import pytest

import hubspot_writer as hw
from contract_spec import HS_ENUMS, VERDICT_ENUMS, good_verdict
from fakes import fake_client

ROW = {"domain": "acme.example", "name": "Acme", "apollo_org_id": "o1", "apollo_account_id": "a1",
       "industry": "supermarkets", "headcount": 450, "tech": [], "funding": None, "job_titles": [], "status": "pending"}
DATE = "2030-01-15"
PORTAL, OWNER = "9000001", "9000002"   # test ids, independent of the environment and of config/settings.json


def props(**over):
    return hw.build_properties(good_verdict(**over), ROW, DATE)


def test_fixed_fields():
    p = props()
    assert p["sourcing_qualification_status"] == "Qualified"
    assert p["sourcing_account_stage"] == "Qualified"
    assert p["sourcing_sourced_from"] == "Apollo"
    assert p["sourcing_p1_date"] == DATE
    assert p["domain"] == "acme.example"
    assert p["name"] == "Acme"
    assert p["hubspot_owner_id"] == OWNER


def test_owner_never_reassigned():
    fresh = {"sourcing_qualification_status": "", "hubspot_owner_id": ""}
    assert hw.plan_update(fresh, props())["hubspot_owner_id"] == OWNER
    owned = {"sourcing_qualification_status": "", "hubspot_owner_id": "111"}
    assert "hubspot_owner_id" not in hw.plan_update(owned, props())


def test_no_owner_configured_sets_no_owner(monkeypatch):
    monkeypatch.setattr(hw, "HUBSPOT_OWNER_ID", "")
    assert "hubspot_owner_id" not in props()


def test_ids_from_settings_file(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text('{"hubspot": {"portal_id": "111", "owner_id": 222}}', encoding="utf-8")
    assert hw.load_hubspot_ids(env={}, settings_path=s) == ("111", "222")


def test_ids_env_takes_precedence_over_settings(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text('{"hubspot": {"portal_id": "111", "owner_id": "222"}}', encoding="utf-8")
    env = {"HUBSPOT_PORTAL_ID": "333", "HUBSPOT_OWNER_ID": " 444 "}
    assert hw.load_hubspot_ids(env=env, settings_path=s) == ("333", "444")
    assert hw.load_hubspot_ids(env={"HUBSPOT_OWNER_ID": "444"}, settings_path=s) == ("111", "444")


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"hubspot": "x"}', '{"hubspot": {}}', "{}"])
def test_ids_missing_or_broken_settings_are_empty(tmp_path, content):
    s = tmp_path / "settings.json"
    if content is not None:
        s.write_text(content, encoding="utf-8")
    assert hw.load_hubspot_ids(env={}, settings_path=s) == ("", "")


def test_shipped_settings_hold_placeholder_ids(repo):
    """The committed config must never carry a real portal or owner id."""
    assert hw.load_hubspot_ids(env={}, settings_path=repo / "config" / "settings.json") == ("00000000", "00000000")


def test_portal_url(monkeypatch):
    assert hw.portal_url("42") == f"https://app-na2.hubspot.com/contacts/{PORTAL}/record/0-2/42"
    assert hw.portal_url(None) == ""
    monkeypatch.setattr(hw, "HUBSPOT_PORTAL", "")
    assert hw.portal_url("42") == ""


@pytest.mark.parametrize("token,expected", [
    ("TIER_A", "Tier A"),
    ("TIER_B", "Tier B"),
    ("TIER_C", "Tier C"),
])
def test_tier_labels(token, expected):
    assert props(account_tier=token)["sourcing_account_tier"] == expected


@pytest.fixture(autouse=True)
def _reset_option_cache(monkeypatch):
    monkeypatch.setattr(hw, "_LIVE_OPTIONS", None, raising=False)
    monkeypatch.setattr(hw, "HUBSPOT_PORTAL", PORTAL)
    monkeypatch.setattr(hw, "HUBSPOT_OWNER_ID", OWNER)


def test_maybe_label():
    # MAYBE is never written (HubSpot holds only Qualified) but the label mapping must be exact
    assert hw.QUALIFICATION["MAYBE"] == "Needs Review"
    assert hw.QUALIFICATION["NOT_QUALIFIED"] == "Not Qualified"


@pytest.mark.parametrize("over", [{"qualification": "MAYBE"}, {"qualification": "NOT_QUALIFIED"},
                                  {"buyer_or_vendor": "VENDOR"}, {"buyer_or_vendor": "UNCLEAR"},
                                  {"status": "error"}])
def test_non_qualified_never_mapped(over):
    with pytest.raises(hw.MappingError):
        props(**over)


def test_bad_date_rejected():
    with pytest.raises(hw.MappingError):
        hw.build_properties(good_verdict(), ROW, "15/01/2030")


def test_domain_normalised():
    p = hw.build_properties(good_verdict(domain="https://www.Acme.example/about"), ROW, DATE)
    assert p["domain"] == "acme.example"


@pytest.mark.parametrize("key,prop,token,expected", [
    ("need_strength", "sourcing_need_strength", "STRONG", "Strong"),
    ("need_strength", "sourcing_need_strength", "MODERATE", "Moderate"),
    ("need_strength", "sourcing_need_strength", "WEAK", "Weak"),
    ("need_strength", "sourcing_need_strength", "NOT_FOUND", "Not Found"),
    ("evidence_confidence", "sourcing_evidence_confidence", "HIGH", "High"),
    ("evidence_confidence", "sourcing_evidence_confidence", "LOW", "Low"),
    ("timing", "sourcing_timing", "ACTIVE_TRIGGER", "Active Trigger"),
    ("timing", "sourcing_timing", "NO_TRIGGER", "No Trigger"),
    ("timing", "sourcing_timing", "UNKNOWN", "Unknown"),
    ("current_approach", "sourcing_current_approach", "MANUAL", "Manual"),
    ("current_approach", "sourcing_current_approach", "BASIC_TOOLS", "Basic Tools"),
    ("current_approach", "sourcing_current_approach", "ADVANCED_SYSTEM", "Advanced System"),
    ("current_approach", "sourcing_current_approach", "UNKNOWN", "Unknown"),
])
def test_enum_mapping(key, prop, token, expected):
    assert props(**{key: token})[prop] == expected


def test_all_enum_outputs_are_portal_values():
    fields = {vk: hk for vk, (hk, _) in hw.ENUM_FIELDS.items()}
    assert set(fields.values()) == set(HS_ENUMS) - {"sourcing_qualification_status"}
    for vk, hk in fields.items():
        for tok in VERDICT_ENUMS[vk]:
            out = props(**{vk: tok})[hk]
            assert out in HS_ENUMS[hk], (vk, tok, out)


def test_evidence_links_joined():
    p = props(evidence_links=["https://a.example/x", "https://b.example/y", "https://c.example/z"])
    assert p["sourcing_evidence_links"] == "https://a.example/x | https://b.example/y | https://c.example/z"


def test_evidence_links_empty():
    assert props(evidence_links=[]).get("sourcing_evidence_links", "") == ""


def test_free_text_mapped():
    p = props()
    assert p["sourcing_qualification_reason"] == "Large fresh offer, orders placed per store"
    assert p["sourcing_strongest_signal"] == "Job post: Replenishment Analyst"
    assert p["sourcing_main_uncertainty"] == "Whether a forecasting system is already in use"
    assert p["sourcing_roles_to_contact"] == "Supply chain; Merchandising"


def test_empty_text_not_written():
    p = props(main_uncertainty="  ", roles_to_contact="")
    assert "sourcing_main_uncertainty" not in p and "sourcing_roles_to_contact" not in p


def test_no_unknown_sourcing_props():
    """Every sourcing_* key must be a property that exists in the portal."""
    known = set(HS_ENUMS) | {"sourcing_account_stage", "sourcing_sourced_from", "sourcing_p1_date",
                             "sourcing_qualification_reason", "sourcing_strongest_signal",
                             "sourcing_main_uncertainty", "sourcing_roles_to_contact", "sourcing_evidence_links"}
    extra = {k for k in props() if k.startswith("sourcing_")} - known
    assert not extra, extra


def test_no_none_values():
    p = hw.build_properties(good_verdict(), {**ROW, "industry": None, "headcount": None}, DATE)
    assert all(v is not None for v in p.values()), p


def test_unicode_name_kept():
    p = hw.build_properties(good_verdict(name="Épicerie Côté Frères"), {**ROW, "name": "Épicerie Côté Frères"}, DATE)
    assert p["name"] == "Épicerie Côté Frères"


# ---- upsert ----

def test_www_domain_record_matched():
    client, comp = fake_client({"www.acme.example": ("42", {"sourcing_qualification_status": "Qualified"})})
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "exists"
    assert comp.writes() == []


def test_upsert_creates_when_missing():
    client, comp = fake_client()
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "created"
    assert [c[0] for c in comp.writes()] == ["create"]


def test_upsert_updates_when_found():
    client, comp = fake_client({"acme.example": ("42", {"sourcing_qualification_status": "Not Reviewed",
                                                    "sourcing_account_stage": "Sourced"})})
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "updated"
    w = comp.writes()
    assert len(w) == 1 and w[0][0] == "update" and str(w[0][1]) == "42"
    assert w[0][2]["sourcing_qualification_status"] == "Qualified"
    assert w[0][2]["sourcing_account_stage"] == "Qualified"


def test_dry_run_no_writes():
    client, comp = fake_client()
    res = hw.upsert(client, "acme.example", props(), dry_run=True)
    assert res["action"] == "dry_run"
    assert comp.writes() == []


def test_existing_qualified_untouched():
    client, comp = fake_client({"acme.example": ("42", {"sourcing_qualification_status": "Qualified",
                                                    "sourcing_account_stage": "Qualified"})})
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "exists"
    assert comp.writes() == []


@pytest.mark.parametrize("status", ["Needs Review", "Not Qualified"])
def test_human_verdict_not_overridden(status):
    client, comp = fake_client({"acme.example": ("42", {"sourcing_qualification_status": status,
                                                    "sourcing_account_stage": "Sourced"})})
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "human_verdict_exists"
    assert comp.writes() == []


def test_not_reviewed_existing_upgraded_not_renamed():
    client, comp = fake_client({"acme.example": ("42", {"name": "ACME Holdings", "sourcing_qualification_status":
                                                    "Not Reviewed", "sourcing_account_stage": "Sourced"})})
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "updated"
    sent = comp.writes()[0][-1]
    assert sent["sourcing_qualification_status"] == "Qualified"
    assert "name" not in sent and "domain" not in sent


def test_domain_conflict_no_write():
    client, comp = fake_client({"acme.example": ("42", {})})
    orig = comp.search_api.do_search

    def two(*a, **kw):
        from types import SimpleNamespace
        r = orig(*a, **kw)
        r.results = r.results + [SimpleNamespace(id="43", properties=dict(r.results[0].properties))]
        return r
    comp.search_api.do_search = two
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "conflict"
    assert comp.writes() == []


def test_live_option_mismatch_fails_closed():
    from fakes import default_options
    opts = default_options()
    opts["sourcing_account_tier"] = {"Tier A"}  # the portal no longer has the other options
    client, comp = fake_client(options=opts)
    res = hw.upsert(client, "acme.example", props(account_tier="TIER_B"), dry_run=False)
    assert res["action"] == "invalid"
    assert comp.writes() == []


@pytest.mark.parametrize("options", [set(), None])
def test_property_without_live_options_fails_closed(options):
    """A property whose option list is empty or unreadable has no valid value."""
    from fakes import default_options
    opts = default_options()
    opts["sourcing_timing"] = options
    assert hw.check_enums(props(), opts) == ["sourcing_timing='No Trigger'"]
    if options is not None:
        client, comp = fake_client(options=opts)
        res = hw.upsert(client, "acme.example", props(), dry_run=False)
        assert res["action"] == "invalid" and "sourcing_timing" in res["note"]
        assert comp.writes() == []


def test_property_missing_from_the_portal_fails_closed():
    from fakes import default_options
    opts = default_options()
    del opts["sourcing_need_strength"]                     # the fake portal answers with no options for it
    client, comp = fake_client(options=opts)
    res = hw.upsert(client, "acme.example", props(), dry_run=False)
    assert res["action"] == "invalid" and "sourcing_need_strength" in res["note"]
    assert comp.writes() == []


def test_dry_run_without_client_no_network():
    res = hw.upsert(None, "acme.example", props(), dry_run=True)
    assert res["action"] == "dry_run"


def test_dry_run_existing_record_no_writes():
    client, comp = fake_client({"acme.example": ("42", {"sourcing_account_stage": "Sourced"})})
    res = hw.upsert(client, "acme.example", props(), dry_run=True)
    assert res["action"] == "dry_run"
    assert comp.writes() == []


@pytest.mark.parametrize("stage", ["Researched", "Contacts Selected", "In Outreach"])
def test_stage_never_moves_back(stage):
    client, comp = fake_client({"acme.example": ("42", {"sourcing_qualification_status": "Qualified",
                                                    "sourcing_account_stage": stage})})
    hw.upsert(client, "acme.example", props(), dry_run=False)
    for w in comp.writes():
        assert w[-1].get("sourcing_account_stage", stage) == stage


def test_existing_text_not_overwritten():
    client, comp = fake_client({"acme.example": ("42", {"sourcing_qualification_status": "Qualified",
                                                    "sourcing_account_stage": "Researched",
                                                    "sourcing_qualification_reason": "hand-written reason",
                                                    "sourcing_main_uncertainty": ""})})
    hw.upsert(client, "acme.example", props(), dry_run=False)
    for w in comp.writes():
        assert w[-1].get("sourcing_qualification_reason", "hand-written reason") == "hand-written reason"


@pytest.mark.parametrize("stage", ["Researched", "Contacts Selected", "In Outreach"])
def test_stage_never_moves_back_when_status_not_qualified(stage):
    client, comp = fake_client({"acme.example": ("42", {"sourcing_qualification_status": "Not Reviewed",
                                                    "sourcing_account_stage": stage})})
    hw.upsert(client, "acme.example", props(), dry_run=False)
    for w in comp.writes():
        assert w[-1].get("sourcing_account_stage", stage) == stage
