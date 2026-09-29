"""Builds docs/sample-morning-report.md: the report the driver writes, for a made-up night.

  python tests/sample_report.py            print the report
  python tests/sample_report.py --write    write docs/sample-morning-report.md

Nothing here is hand-written report text. The real driver (scripts/run_nightly.py) runs in a
temporary folder against the test stubs: a fake `claude` that answers from a scenario, a fake
crawler, and a fake HubSpot client. The companies are fictional. The clock is fixed to a made-up
date so that the output is the same on every run. tests/test_sample_report.py fails when the
committed file or the copy in the README differs from what this produces.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(TESTS))

import hubspot_writer as hw  # noqa: E402
import run_nightly as rn  # noqa: E402
from contract_spec import good_verdict  # noqa: E402
from fakes import fake_client  # noqa: E402

DATE = "2030-01-15"
OUT = REPO / "docs" / "sample-morning-report.md"
SETTINGS = {"per_run_cap": 10, "model": "opus", "hubspot_tiers": ["TIER_A"], "register_results": False,
            "hubspot": {"portal_id": "YOUR_PORTAL_ID", "owner_id": ""}, "apollo": {"enrich_pushed": False}}


def row(domain, name, status="pending", **kw):
    r = {"domain": domain, "name": name, "apollo_org_id": "org-" + domain, "apollo_account_id": "acc-" + domain,
         "industry": "supermarkets", "headcount": 2400, "tech": [], "funding": None, "job_titles": [],
         "status": status}
    r.update(kw)
    return r


def verdict(domain, name, **over):
    return {"verdict": good_verdict(domain=domain, name=name, **over)}


COMPANIES = [
    (row("harrow-and-finch.example", "Harrow & Finch Grocers"),
     verdict("harrow-and-finch.example", "Harrow & Finch Grocers", account_tier="TIER_A", timing="ACTIVE_TRIGGER",
             qualification_reason="62 stores with a large fresh offer and a published food waste target. "
                                  "A second distribution centre opens this year.")),
    (row("lakeshore-grocers.example", "Lakeshore Grocers"),
     verdict("lakeshore-grocers.example", "Lakeshore Grocers", account_tier="TIER_B", need_strength="MODERATE",
             qualification_reason="31 stores, fresh categories are a clear part of the offer. No recent change found.")),
    (row("tricounty.example", "Tri-County Supply"),
     verdict("tricounty.example", "Tri-County Supply", buyer_or_vendor="VENDOR", account_tier="TIER_B",
             buyer_vendor_reason="Grocers pay it for wholesale deliveries.",
             qualification_reason="Supplies 200 independent stores and runs none of its own.")),
    (row("northfield.example", "Northfield Grocers", headcount=14500, scope_warnings=["headcount=14500"]),
     verdict("northfield.example", "Northfield Grocers", qualification="NOT_QUALIFIED", account_tier="TIER_C",
             qualification_reason="A national chain with more than 300 stores, outside the size we sell to.")),
    (row("pinecrest.example", "Pinecrest Markets"),
     verdict("pinecrest.example", "Pinecrest Markets", account_tier="TIER_A", timing="ACTIVE_TRIGGER",
             qualification_reason="44 stores, new head of supply chain since the spring.")),
    (row("quillbrook.example", "Quillbrook Markets"),
     verdict("quillbrook.example", "Quillbrook Markets", qualification="NOT_QUALIFIED", account_tier="TIER_C",
             need_strength="NOT_FOUND", current_approach="UNKNOWN", timing="UNKNOWN", evidence_confidence="LOW",
             qualification_reason="The site refused the crawler and web search found too little to judge the offer.")),
    (row("marlow-fresh.example", "Marlow Fresh"), {"garbage": "I could not decide."}),
    (row("delta-cash-carry.example", "Delta Cash and Carry", status="skipped_prefilter", skip_reason="name_match"),
     None),
]
EXISTING = {"pinecrest.example": ("301", {"sourcing_qualification_status": "Not Qualified"})}
BLOCKED = "quillbrook.example"


@contextlib.contextmanager
def patched(root: Path, home: Path, scenario: Path):
    ticks = iter(range(10_000))
    env = {"USERPROFILE": str(home), "HOME": str(home), "SOURCING_ROOT": str(root),
           "SOURCING_CLAUDE_BIN": str(TESTS / "stubs" / "fake_claude.py"),
           "SOURCING_SOURCE_PACK_CMD": "{python} " + str(TESTS / "stubs" / "fake_source_pack.py").replace("\\", "/")
                                       + " {domain}",
           "SOURCING_STUB_SCENARIO": str(scenario), "SOURCING_STUB_SP_BLOCKED": BLOCKED}
    drop = ["SOURCING_STUB_LOG", "SOURCING_STUB_SP_FAIL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HUBSPOT_TOKEN"]
    old_env = {k: os.environ.get(k) for k in list(env) + drop}
    old = (hw.get_client, hw._LIVE_OPTIONS, hw.HUBSPOT_PORTAL, hw.HUBSPOT_OWNER_ID, rn.now_iso, rn.today_iso)
    client, _ = fake_client(EXISTING)
    try:
        os.environ.update(env)
        for k in drop:
            os.environ.pop(k, None)
        hw.get_client = lambda: client
        hw._LIVE_OPTIONS = None
        hw.HUBSPOT_PORTAL, hw.HUBSPOT_OWNER_ID = SETTINGS["hubspot"]["portal_id"], SETTINGS["hubspot"]["owner_id"]
        rn.now_iso = lambda: f"{DATE}T01:{next(ticks) // 2:02d}:00+00:00"       # a made-up clock
        rn.today_iso = lambda: DATE
        yield
    finally:
        hw.get_client, hw._LIVE_OPTIONS, hw.HUBSPOT_PORTAL, hw.HUBSPOT_OWNER_ID, rn.now_iso, rn.today_iso = old
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def generate() -> str:
    """Run the driver once on the fictional night and return the text of runs/<date>.md."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root, home = tmp / "root", tmp / "home"
        (root / "config").mkdir(parents=True)
        home.mkdir()
        (root / "config" / "settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
        scenario = tmp / "scenario.json"
        scenario.write_text(json.dumps({
            "source": {"date": DATE, "rows": [r for r, _ in COMPANIES]},
            "qualify": {r["domain"]: v for r, v in COMPANIES if v is not None}}, ensure_ascii=False), encoding="utf-8")
        with patched(root, home, scenario), contextlib.redirect_stdout(io.StringIO()):
            code = rn.main(["--date", DATE, "--root", str(root)])
        if code != 0:
            raise RuntimeError(f"driver exit code {code}")
        return (root / "runs" / f"{DATE}.md").read_text(encoding="utf-8")


README_START, README_END = "<!-- sample-report:start -->", "<!-- sample-report:end -->"


def for_readme(text: str) -> str:
    """The report as the README shows it: the same text with every heading two levels lower."""
    return "\n".join("##" + line if line.startswith("#") else line for line in text.strip().splitlines())


def readme_section(readme: str) -> str:
    return readme.split(README_START, 1)[1].split(README_END, 1)[0].strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="write docs/sample-morning-report.md")
    a = ap.parse_args(argv)
    text = generate()
    if a.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_bytes(text.encode("utf-8"))
        print(f"wrote {OUT.relative_to(REPO).as_posix()}")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
