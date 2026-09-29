"""source_queue.py: the deterministic half of /source. No Apollo calls: rows are hand-written fixtures."""
import json
import shutil
import subprocess
import sys

import pytest

import source_queue as sq
from contract_spec import QUEUE_KEYS

DATE = "2030-01-15"
H = lambda n: f"{n:024x}"  # 24-hex Apollo id

PRE = {"exclude_name_regex": r"\b(wholesale|distributors?|foodservice|consulting)\b",
       "exclude_industry_regex": "wholesale|food production|management consulting",
       "allowed_countries": ["United States", "Canada"], "headcount_min": 1001, "headcount_max": 10000}


def raw(i, name=None, domain=None, **kw):
    r = {"org_id": H(i), "name": name or f"Co{i}", "domain": domain or f"co{i}.example", "industry": "supermarkets",
         "headcount": 2400, "country": "United States"}
    r.update(kw)
    return r


@pytest.mark.parametrize("inp,out", [
    ("https://WWW.Acme.example:443/about", "acme.example"), ("acme.example", "acme.example"), ("", None), (None, None),
    ("localhost", None), (5, None), ("http://sub.acme.example/x?y", "sub.acme.example"),
])
def test_normalize_domain(inp, out):
    assert sq.normalize_domain(inp) == out


def test_prefilter_wholesalers_and_consultancies():
    assert sq.prefilter({"name": "Acme Wholesale"}, PRE) == (False, "name_match")
    assert sq.prefilter({"name": "Acme", "industry": "Wholesale & Food Production"}, PRE)[0] is False
    assert sq.prefilter({"name": "Harbor Grocers", "industry": "retail"}, PRE) == (True, None)
    assert sq.prefilter({"name": "Pine Market", "industry": None}, PRE) == (True, None)  # unknown never excludes


def test_select_cap_and_prefilter_do_not_count():
    rows = [raw(1), raw(2, name="Blue Distributors"), raw(3), raw(4), raw(5)]
    res = sq.select(rows, PRE, cap=2, seen_domains=set())
    st = [(c["domain"], c["status"]) for c in res["candidates"]]
    assert st == [("co1.example", "pending"), ("co2.example", "skipped_prefilter"), ("co3.example", "pending")]
    assert res["pending"] == 2 and res["prefiltered"] == 1 and res["need_more"] is False


def test_select_resurfaced_and_dupes():
    rows = [raw(1), raw(2, domain="https://www.co1.example/"), raw(3, domain="seen.example")]
    res = sq.select(rows, PRE, cap=10, seen_domains={"seen.example"})
    assert [c["domain"] for c in res["candidates"]] == ["co1.example"]
    assert res["resurfaced"] == ["seen.example"]


def test_select_invalid_ids():
    res = sq.select([raw(1, org_id="not-hex")], PRE, cap=10, seen_domains=set())
    assert res["candidates"] == [] and len(res["invalid"]) == 1


def test_scope_warning_never_excludes():
    res = sq.select([raw(1, headcount=50000, country="India")], PRE, cap=10, seen_domains=set())
    c = res["candidates"][0]
    assert c["status"] == "pending" and c["scope_warnings"]


def test_queue_line_contract():
    res = sq.select([raw(1), raw(2, name="X Foodservice")], PRE, cap=10, seen_domains=set())
    reg = [{"domain": "co1.example", "name": "Co1", "account_id": H(101), "organization_id": H(1)},
           {"domain": "co2.example", "name": "X Foodservice", "account_id": H(102), "organization_id": H(2)}]
    lines = sq.build_queue_lines(res["candidates"], reg, DATE, "sourced-" + DATE)
    for l in lines:
        assert QUEUE_KEYS <= set(l), QUEUE_KEYS - set(l)
        assert l["status"] in {"pending", "skipped_prefilter"}
        json.dumps(l)  # serialisable
    assert [l["apollo_account_id"] for l in lines] == [H(101), H(102)]


@pytest.mark.parametrize("line,expected", [
    ({"status": "done", "verdict": "QUALIFIED"}, "Qualified"),   # driver writes verdict as the qualification string
    ({"status": "done", "verdict": "MAYBE"}, "Maybe"),
    ({"status": "done", "verdict": "NOT_QUALIFIED"}, "Not Qualified"),
    ({"status": "skipped_prefilter"}, "Prefiltered"),
    ({"status": "error"}, "Error"),
    ({"status": "pending"}, None),
    ({"status": "hubspot_error", "verdict": "QUALIFIED"}, "Qualified"),   # P1 final; only HubSpot pending
    ({"status": "needs_review", "verdict": "QUALIFIED"}, "Qualified"),
])
def test_result_value_reads_driver_rows(line, expected):
    assert sq.result_value(line) == expected


def _root(tmp_path, repo):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    for f in ("settings.json", "search.json"):
        shutil.copy(repo / "config" / f, root / "config" / f)
    return root


def cli(repo, root, *args):
    r = subprocess.run([sys.executable, str(repo / "scripts" / "source_queue.py"), *args, "--date", DATE,
                        "--root", str(root)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_cli_select_build_then_exists(tmp_path, repo):
    root = _root(tmp_path, repo)
    sdir = root / "state" / "source" / DATE
    sdir.mkdir(parents=True)
    rows = [raw(1, name="Épicerie Côté Frères", domain="ecf.example"), raw(2, name="Alpha Wholesale"), raw(3)]
    (sdir / "page-1.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    s = cli(repo, root, "select", "--cap", "5")
    assert s["pending"] == 2 and s["prefiltered"] == 1
    (sdir / "registered.json").write_text(json.dumps({"list_name": "sourced-" + DATE, "list_url": "u", "accounts": [
        {"domain": "ecf.example", "name": "Épicerie Côté Frères", "account_id": H(11), "organization_id": H(1)},
        {"domain": "co2.example", "name": "Alpha Wholesale", "account_id": H(12), "organization_id": H(2)},
        {"domain": "co3.example", "name": "Co3", "account_id": H(13), "organization_id": H(3)}]}), encoding="utf-8")
    b = cli(repo, root, "build")
    assert b["status"] == "ok" and b["registered"] == 3 and b["unregistered"] == 0
    q = root / "state" / "queue" / f"{DATE}.jsonl"
    lines = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["name"] == "Épicerie Côté Frères"
    # second build never overwrites (driver may already have updated statuses)
    before = q.read_bytes()
    assert cli(repo, root, "build")["status"] == "exists"
    assert q.read_bytes() == before


def test_unregistered_after_driver_marks_done(tmp_path, repo):
    root = _root(tmp_path, repo)
    q = root / "state" / "queue" / f"{DATE}.jsonl"
    q.parent.mkdir(parents=True)
    rows = [{"domain": "a.example", "apollo_account_id": H(1), "status": "done", "verdict": "QUALIFIED"},
            {"domain": "b.example", "apollo_account_id": H(2), "status": "pending"},
            {"domain": "c.example", "apollo_account_id": None, "status": "done", "verdict": "MAYBE"}]
    q.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    u = sq.unregistered(root, DATE)
    assert [(x["domain"], x["result"]) for x in u] == [("a.example", "Qualified")]
    m = sq.mark_registered(root, DATE, [H(1)], "list")
    assert m["marked"] == 1 and m["remaining"] == 0


def test_name_only_match_with_different_domain_rejected():
    cand = [{"domain": "acme.example", "name": "Acme Grocers", "apollo_org_id": None, "status": "pending"}]
    reg = [{"domain": "acme-grocers.example", "name": "Acme Grocers", "account_id": H(9), "organization_id": H(8)}]
    lines = sq.build_queue_lines(cand, reg, DATE, "l")
    assert lines[0]["apollo_account_id"] is None
    assert sq.name_collisions(cand, reg) == ["dedupe_name_collision: acme.example vs acme-grocers.example"]


def test_name_only_match_without_domain_still_matches():
    cand = [{"domain": None, "name": "Acme Grocers", "apollo_org_id": None, "status": "skipped_prefilter"}]
    reg = [{"domain": None, "name": "acme grocers", "account_id": H(9)}]
    assert sq.build_queue_lines(cand, reg, DATE, "l")[0]["apollo_account_id"] == H(9)


def test_every_driver_status_is_known_to_result_value(repo):
    """Statuses run_nightly.py writes into queue lines must all be understood by /register-results."""
    import re as _re
    src = (repo / "scripts" / "run_nightly.py").read_text(encoding="utf-8")
    statuses = set(_re.findall(r'\["status"\]\s*=\s*"([a-z_]+)"', src)) | \
        set(_re.findall(r'status="([a-z_]+)"', src))
    statuses -= {"ok"}
    assert {"done", "error", "hubspot_error", "needs_review"} <= statuses, statuses
    for st in statuses:
        v = sq.result_value({"status": st, "verdict": "MAYBE"})
        assert (v is None) == (st == "pending"), (st, v)


def test_batch_ids():
    import source_queue as sq
    assert sq.is_batch("2030-01-16") and sq.is_batch("2030-01-16b") and sq.is_batch("2030-01-16z")
    assert not sq.is_batch("2030-01-16a") and not sq.is_batch("2030-01-16-2") and not sq.is_batch("27/09")
    assert "2030-01-16" < "2030-01-16b" < "2030-01-16c" < "2030-01-17"  # string order = run order
