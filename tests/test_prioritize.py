import csv
import json

import prioritize as pr


def test_moves_only_pending_matches_to_front(tmp_path):
    q = tmp_path / "state" / "queue"
    q.mkdir(parents=True)
    lines = [{"domain": "a.example", "status": "done", "apollo_account_id": "1"},
             {"domain": "b.example", "status": "pending", "apollo_account_id": "2"},
             {"domain": "c.example", "status": "pending", "apollo_account_id": "3"},
             {"domain": "d.example", "status": "skipped_prefilter", "apollo_account_id": "4"},
             {"domain": "e.example", "status": "pending", "apollo_account_id": "5"}]
    (q / "2030-01-16e.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    p = tmp_path / "export3.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Apollo Account Id", "Website"])
        w.writerows([["5", ""], ["", "http://www.c.example"], ["1", ""], ["4", ""], ["9", "zzz.example"]])
    out = pr.prioritize(tmp_path, "2030-01-16e", p)
    assert out["moved_to_front"] == 2 and out["matched"] == 4
    got = [json.loads(l) for l in (q / "2030-01-16e.jsonl").read_text().splitlines()]
    assert [l["domain"] for l in got] == ["c.example", "e.example", "a.example", "b.example", "d.example"]
    assert got[0]["priority"] == "export3.csv" and "priority" not in got[2]
    assert len(got) == 5
