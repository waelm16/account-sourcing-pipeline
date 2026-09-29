import csv
import json

import import_csv as ic

HEAD = ["Company Name", "Website", "Industry", "# Employees", "Company Country", "Keywords", "Technologies",
        "Apollo Account Id", "Parent company (Apollo data)"]
ROWS = [
    ["Northfield Grocers", "http://www.northfield.example", "supermarkets", "900", "United States", "grocery",
     "ExamplePOS, ExampleLoyalty", "a" * 24, ""],
    ["Tri-County Supply", "http://www.tricounty.example", "retail", "600", "Canada",
     "cash and carry, wholesale & distribution", "", "b" * 24, ""],
    ["Example Properties", "http://www.properties.example", "real estate", "4000", "United States", "", "", "c" * 24, ""],
    ["Lakeside Fresh", "http://www.lakeside.example", "supermarkets", "550", "Canada", "", "",
     "d" * 24, "Example Retail Group"],
    ["No Site Inc", "", "supermarkets", "300", "United States", "", "", "e" * 24, ""],
    ["Northfield dup", "https://northfield.example/", "supermarkets", "900", "United States", "", "", "f" * 24, ""],
    ["Old Co", "http://old.example", "supermarkets", "300", "United States", "", "", "1" * 24, ""],
]


def setup(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "search.json").write_text(json.dumps({
        "exclude_keyword_tags": ["Wholesale & Distribution"],
        "pre_filter": {"exclude_industry_regex": "wholesale|food production|^real estate$",
                       "exclude_subsidiaries": True}}), encoding="utf-8")
    (tmp_path / "config" / "settings.json").write_text("{}", encoding="utf-8")
    q = tmp_path / "state" / "queue"
    q.mkdir(parents=True)
    (q / "2030-01-16.jsonl").write_text(json.dumps({"domain": "old.example", "status": "done"}) + "\n", encoding="utf-8")
    p = tmp_path / "export.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows(ROWS)
    return p


def test_import_queues_only_the_right_companies(tmp_path):
    p = setup(tmp_path)
    out = ic.import_csv(tmp_path, p, batch="2030-01-16e")
    assert out["status"] == "ok", out
    assert out["to_qualify"] == 1
    assert out["skipped"] == {"excluded_keyword_tag": 1, "industry_match": 1, "subsidiary": 1, "no_domain": 1}
    assert out["already_queued_before"] == 1
    lines = [json.loads(l) for l in (tmp_path / "state" / "queue" / "2030-01-16e.jsonl").read_text().splitlines()]
    pending = [l for l in lines if l["status"] == "pending"]
    assert [(l["domain"], l["apollo_account_id"]) for l in pending] == [("northfield.example", "a" * 24)]
    assert all(l["apollo_account_id"] for l in lines)  # nothing needs creating in Apollo
    assert lines[0]["list_name"] == "csv:export.csv"


def test_dry_run_writes_nothing_and_batch_is_never_reused(tmp_path):
    p = setup(tmp_path)
    out = ic.import_csv(tmp_path, p, batch="2030-01-16e", dry=True)
    assert out["status"] == "dry_run" and out["to_qualify"] == 1
    assert not (tmp_path / "state" / "source" / "2030-01-16e").exists()
    assert ic.import_csv(tmp_path, p, batch="2030-01-16e")["status"] == "ok"
    assert ic.import_csv(tmp_path, p, batch="2030-01-16e")["status"] == "error"


def test_rejects_non_accounts_export(tmp_path):
    p = tmp_path / "people.csv"
    p.write_text("First Name,Email\nA,a@b.c\n", encoding="utf-8")
    setup(tmp_path)
    assert ic.import_csv(tmp_path, p, batch="2030-01-16e")["status"] == "error"
