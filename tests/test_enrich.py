import json
from types import SimpleNamespace

import pytest

import enrich

OPTS = [SimpleNamespace(label=l, value=v) for l, v in [
    ("Supermarkets", "SUPERMARKETS"), ("Retail", "RETAIL"),
    ("Food & Beverages", "FOOD_BEVERAGES"),
    ("Information Technology and Services", "INFORMATION_TECHNOLOGY_AND_SERVICES"),
    ("Security and Investigations", "SECURITY_AND_INVESTIGATIONS")]]


def test_industry_mapping():
    idx = enrich.industry_index(OPTS)
    assert enrich.map_industry("supermarkets", idx) == "SUPERMARKETS"
    assert enrich.map_industry("Food & Beverages", idx) == "FOOD_BEVERAGES"
    assert enrich.map_industry("food and beverages", idx) == "FOOD_BEVERAGES"
    assert enrich.map_industry("information technology & services", idx) == "INFORMATION_TECHNOLOGY_AND_SERVICES"
    assert enrich.map_industry("retail", idx) == "RETAIL"
    assert enrich.map_industry("vertical farming", idx) is None
    assert enrich.map_industry("", idx) is None


class FakeClient:
    def __init__(self, records):
        self.records = records  # id -> props
        self.updates = []
        outer = self

        class Basic:
            def get_by_id(self, company_id=None, properties=None):
                return SimpleNamespace(properties=dict(outer.records[company_id]))

            def update(self, company_id=None, simple_public_object_input=None):
                props = simple_public_object_input.properties
                outer.updates.append((company_id, props))
                outer.records[company_id].update(props)

        class Core:
            def get_by_name(self, object_type=None, property_name=None):
                return SimpleNamespace(options=OPTS)

        self.crm = SimpleNamespace(companies=SimpleNamespace(basic_api=Basic()),
                                   properties=SimpleNamespace(core_api=Core()))


def setup(tmp_path, rows):
    q = tmp_path / "state" / "queue"
    q.mkdir(parents=True)
    (q / "2030-01-17.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


ROWS = [{"domain": "a.example", "status": "done", "hubspot_action": "created", "hubspot_id": "1"},
        {"domain": "b.example", "status": "done", "hubspot_action": "created", "hubspot_id": "2"},
        {"domain": "c.example", "status": "done", "hubspot_action": "exists", "hubspot_id": "3"},
        {"domain": "d.example", "status": "done"},                       # not qualified: never enriched
        {"domain": "e.example", "status": "done", "hubspot_action": "dry_run", "hubspot_id": None}]


def test_todo_skips_filled_and_unpushed(tmp_path):
    setup(tmp_path, ROWS)
    c = FakeClient({"1": {}, "2": {"numberofemployees": "50"},
                    "3": {"numberofemployees": "2100", "industry": "SUPERMARKETS"}})
    out = enrich.todo(tmp_path, c)
    assert out["domains"] == ["a.example", "b.example"]
    assert enrich.load_done(tmp_path)["c.example"]["status"] == "not_needed"
    assert enrich.todo(tmp_path, c)["domains"] == ["a.example", "b.example"]  # stable until applied


def test_todo_respects_the_credit_ceiling(tmp_path):
    rows = [{"domain": f"c{i}.example", "status": "done", "hubspot_action": "created", "hubspot_id": str(i)}
            for i in range(1, 6)]
    setup(tmp_path, rows)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text('{"apollo": {"max_enrich_per_call": 2}}', encoding="utf-8")
    c = FakeClient({str(i): {} for i in range(1, 6)})
    out = enrich.todo(tmp_path, c)
    assert out["domains"] == ["c1.example", "c2.example"] and out["count"] == 2
    assert out["credit_ceiling"] == 2 and out["waiting"] == 3
    (tmp_path / "config" / "settings.json").write_text('{"apollo": {"max_enrich_per_call": 0}}', encoding="utf-8")
    assert enrich.todo(tmp_path, c)["domains"] == []                       # a ceiling of 0 spends nothing


@pytest.mark.parametrize("content", [None, "not json", "{}", '{"apollo": {"max_enrich_per_call": "many"}}'])
def test_credit_ceiling_default(tmp_path, content):
    if content is not None:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "settings.json").write_text(content, encoding="utf-8")
    assert enrich.max_per_call(tmp_path) == enrich.DEFAULT_MAX_PER_CALL == 10


def test_apply_fills_only_empty_and_is_once(tmp_path):
    setup(tmp_path, ROWS)
    c = FakeClient({"1": {}, "2": {"numberofemployees": "50"}, "3": {}})
    rf = tmp_path / "state" / "enrich"
    rf.mkdir(parents=True)
    (rf / "results.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"domain": "a.example", "found": True, "estimated_num_employees": 1200, "industry": "retail"},
        {"domain": "b.example", "found": True, "estimated_num_employees": 900, "industry": "vertical farming"},
        {"domain": "c.example", "found": False},
        {"domain": "zzz.example", "found": True, "estimated_num_employees": 5},  # not ours: ignored
    ]), encoding="utf-8")
    s = enrich.apply(tmp_path, c)
    assert c.updates == [("1", {"numberofemployees": "1200", "industry": "RETAIL"})]
    assert s["enriched"] == 1 and s["not_found"] == 1 and s["no_new_data"] == 1
    assert s["unmapped_industry"] == ["b.example: vertical farming"]
    assert c.records["2"]["numberofemployees"] == "50"  # never overwritten
    assert enrich.todo(tmp_path, c)["domains"] == []   # nothing is enriched twice
    assert not (rf / "results.jsonl").exists()          # consumed; foreign domains dropped


def test_enrich_step_disabled_and_nothing_to_do(monkeypatch):
    import run_nightly as rn
    called = []
    monkeypatch.setattr(rn, "run_claude", lambda *a, **k: called.append(a) or {})
    rep = rn.new_report("2030-01-17", 5, "opus", False)
    holder = {"client": object()}
    assert rn.enrich_step(None, {"apollo": {"enrich_pushed": False}}, "opus", holder, lambda: 999, rep) is None
    monkeypatch.setattr(rn.enrich, "todo", lambda root, client: {"count": 0, "domains": []})
    assert rn.enrich_step(None, {"apollo": {"enrich_pushed": True}}, "opus", holder, lambda: 999, rep) is None
    assert not called


def test_enrich_step_runs_and_counts(monkeypatch):
    import run_nightly as rn
    monkeypatch.setattr(rn.enrich, "todo", lambda root, client: {"count": 1, "domains": ["a.example"]})
    out = '{"status":"ok","enriched":1,"not_found":0,"no_new_data":0,"unmapped_industry":["a.example: vertical farming"],"errors":[]}'
    monkeypatch.setattr(rn, "run_claude", lambda args, timeout, root: {"rc": 0, "json": {"result": "done\n" + out}})
    rep = rn.new_report("2030-01-17", 5, "opus", False)
    kind = rn.enrich_step(None, {"apollo": {"enrich_pushed": True}, "timeout_register_s": 900},
                          "opus", {"client": object()}, lambda: 999, rep)
    assert kind == "ok"
    assert rep["counts"]["enriched"] == 1
    assert any("a.example: vertical farming" in n for n in rep["notes"])
    assert "/enrich" in rn.build_report(rep) or "employee count" in rn.build_report(rep)


def test_record_held_appends_csv(tmp_path):
    import csv as _csv
    import run_nightly as rn
    v = {"name": "Lakeshore Grocers", "account_tier": "TIER_B", "need_strength": "MODERATE",
         "qualification_reason": "qualified, no trigger"}
    rn.record_held(tmp_path, {"domain": "lakeshore-grocers.example", "p1_date": "2030-01-16", "apollo_account_id": "x"}, v, "2030-01-16e")
    rn.record_held(tmp_path, {"domain": "b.example", "p1_date": "2030-01-16"}, {**v, "name": "B"}, "2030-01-16e")
    rows = list(_csv.DictReader(open(tmp_path / rn.HELD_CSV, encoding="utf-8-sig")))
    assert [r["domain"] for r in rows] == ["lakeshore-grocers.example", "b.example"]
    assert rows[0]["account_tier"] == "TIER_B" and rows[0]["batch"] == "2030-01-16e"
