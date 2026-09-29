"""The sample morning report in docs/ and in the README is what the report code produces, not an illustration."""
import os

import hubspot_writer as hw
import run_nightly as rn
import sample_report as sr


def test_committed_sample_report_is_generated_output(repo):
    generated = sr.generate()
    assert (repo / "docs" / "sample-morning-report.md").read_text(encoding="utf-8") == generated, \
        "run: python tests/sample_report.py --write"
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert sr.readme_section(readme) == sr.for_readme(generated), "the README copy of the sample report is out of date"


def test_sample_night_covers_the_cases_the_readme_names():
    text = sr.generate()
    rows = [l for l in text.splitlines() if l.startswith("| ") and l[2].isdigit() and "example" in l]
    assert len(rows) == 7                                      # 8 companies, one removed by the pre-filter
    assert "| 7 | 1 | 0 | 7 | 3 | 3 | 1 | 1 | 0 | 0 | 1 | 0 |" in text
    for needle in ("[created](", "| held |", "downgraded QUALIFIED->NOT_QUALIFIED (buyer_or_vendor=VENDOR)",
                   "human_verdict_exists", "[crawl: blocked (http 403)]", "unparseable verdict",
                   "## Needs review", "[scope: headcount=14500]"):
        assert needle in text, needle
    assert "maybe" not in text.lower()


def test_generating_the_report_leaves_nothing_behind(repo, monkeypatch):
    monkeypatch.setenv("SOURCING_ROOT", "kept")
    monkeypatch.delenv("SOURCING_STUB_SP_BLOCKED", raising=False)
    before = (hw.get_client, hw.HUBSPOT_PORTAL, rn.now_iso, rn.today_iso)
    sr.generate()
    assert (hw.get_client, hw.HUBSPOT_PORTAL, rn.now_iso, rn.today_iso) == before
    assert os.environ["SOURCING_ROOT"] == "kept" and "SOURCING_STUB_SP_BLOCKED" not in os.environ
    assert not (repo / "state").exists() and not (repo / "runs").exists()
