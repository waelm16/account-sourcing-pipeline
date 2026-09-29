"""Driver tests with a stubbed `claude` (tests/stubs/fake_claude.py), a stubbed source pack and a fake HubSpot
client. Nothing here reaches Apollo, HubSpot or Anthropic."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hubspot_writer as hw
import run_nightly as rn
from contract_spec import good_verdict
from fakes import fake_client

STUBS = Path(__file__).resolve().parent / "stubs"
DATE = "2030-01-15"
YDAY = "2030-01-14"


def row(domain, name=None, status="pending", **kw):
    r = {"domain": domain, "name": name or domain.split(".")[0].title(), "apollo_org_id": "o-" + domain,
         "apollo_account_id": "a-" + domain, "industry": "supermarkets", "headcount": 400, "tech": ["example_pos"],
         "funding": None, "job_titles": [], "status": status}
    r.update(kw)
    return r


def V(d, **over):
    base = {"domain": d, "name": d.split(".")[0].title()}
    base.update(over)
    return {"verdict": good_verdict(**base)}


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.scen_path = tmp_path / "scenario.json"
        self.log_path = tmp_path / "claude_calls.jsonl"
        self.mp = monkeypatch
        self.scenario = {"source": {"date": DATE, "rows": []}, "qualify": {}}
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("USERPROFILE", str(home))       # api_key_guard reads ~/.claude/settings*.json
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SOURCING_CLAUDE_BIN", str(STUBS / "fake_claude.py"))
        monkeypatch.setenv("SOURCING_SOURCE_PACK_CMD",
                           "{python} " + str(STUBS / "fake_source_pack.py").replace("\\", "/") + " {domain}")
        monkeypatch.setenv("SOURCING_STUB_SCENARIO", str(self.scen_path))
        monkeypatch.setenv("SOURCING_STUB_LOG", str(self.log_path))
        monkeypatch.setenv("SOURCING_ROOT", str(self.root))
        # secrets that must never reach a claude child
        monkeypatch.setenv("HUBSPOT_TOKEN", "pat-should-not-leak")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
        self.client, self.hs = fake_client()
        monkeypatch.setattr(hw, "get_client", lambda: self.client)
        monkeypatch.setattr(hw, "_LIVE_OPTIONS", None, raising=False)
        monkeypatch.setattr(hw, "HUBSPOT_PORTAL", "9000001")    # test ids, independent of the environment
        monkeypatch.setattr(hw, "HUBSPOT_OWNER_ID", "9000002")

    def settings(self, **kw):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "settings.json").write_text(json.dumps(kw), encoding="utf-8")

    def queue(self, date, rows):
        q = self.root / "state" / "queue" / f"{date}.jsonl"
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def read_queue(self, date):
        q = self.root / "state" / "queue" / f"{date}.jsonl"
        return {r["domain"]: r for r in (json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip())}

    def run(self, *args):
        self.scen_path.write_text(json.dumps(self.scenario, ensure_ascii=False), encoding="utf-8")
        # ANTHROPIC_API_KEY is only a warning for the driver (it strips it), so keep it set.
        code = rn.main(["--date", DATE, "--root", str(self.root), *args])
        return code

    def report(self, dry=False):
        stem = DATE + ("-dry-run" if dry else "")
        return json.loads((self.root / "runs" / f"{stem}.json").read_text(encoding="utf-8"))

    def calls(self, prefix=None):
        if not self.log_path.exists():
            return []
        out = [json.loads(l) for l in self.log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if prefix:
            out = [c for c in out if any(a.startswith(prefix) for a in c["argv"])]
        return out

    def reset_calls(self):
        if self.log_path.exists():
            self.log_path.unlink()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---------------------------------------------------------------- happy path
def test_fresh_run_end_to_end(env):
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example"), row("c.example"),
                                      row("wholesaler.example", status="skipped_prefilter")]
    env.scenario["qualify"] = {"a.example": V("a.example"), "b.example": V("b.example", qualification="MAYBE"),
                               "c.example": V("c.example", qualification="NOT_QUALIFIED")}
    assert env.run("--cap", "5") == 0
    q = env.read_queue(DATE)
    assert {d: r["status"] for d, r in q.items()} == {"a.example": "done", "b.example": "done", "c.example": "done",
                                                    "wholesaler.example": "skipped_prefilter"}
    rep = env.report()
    c = rep["counts"]
    assert (c["pulled"], c["prefiltered"], c["qualified"], c["not_qualified"], c["errors"]) == (3, 1, 1, 2, 0)
    md = (env.root / "runs" / f"{DATE}.md").read_text(encoding="utf-8")
    header = next(l for l in md.splitlines() if l.startswith("| pulled"))
    assert "maybe" not in c and "maybe" not in header.lower()      # verdicts are binary: no such column
    assert "MAYBE->NOT_QUALIFIED" in md                            # the downgrade of a stray MAYBE is still noted
    assert c["hubspot_created"] == 1
    assert [w[0] for w in env.hs.writes()] == ["create"]
    assert env.hs.writes()[0][1]["domain"] == "a.example"
    assert rep["stopped_reason"] in (None, "completed")
    md = (env.root / "runs" / f"{DATE}.md").read_text(encoding="utf-8")
    assert "a.example" in md and "b.example" in md and "c.example" in md
    assert len(env.calls("/source")) == 1
    assert len(env.calls("/qualify")) == 3
    assert len(env.calls("/register-results")) == 1
    assert (env.root / "state" / "work" / "a.example" / "p1.json").exists()


def test_secrets_not_passed_to_claude(env):
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", qualification="MAYBE")}
    env.run()
    calls = env.calls()
    assert calls
    for c in calls:
        assert not c["has_hubspot_token"], c
        assert not c["has_anthropic_key"], c


def test_qualify_uses_model_and_json_output(env):
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", qualification="MAYBE")}
    env.run("--model", "opus")
    argv = env.calls("/qualify")[0]["argv"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--max-turns" in argv


# ---------------------------------------------------------------- idempotency / resume
def test_same_day_rerun_is_noop(env):
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": V("a.example"), "b.example": V("b.example", qualification="MAYBE")}
    env.run()
    env.reset_calls()
    writes_before = len(env.hs.writes())
    env.run()
    assert env.calls("/source") == []
    assert env.calls("/qualify") == []
    assert env.calls("/register-results") == []
    assert len(env.hs.writes()) == writes_before


def test_rerun_keeps_first_runs_report(env):
    """The morning report must not be wiped by a second (no-op or --resume) run the same day."""
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    env.run()
    env.run()
    runs = env.root / "runs"
    text = "".join(p.read_text(encoding="utf-8") for p in runs.glob(f"{DATE}*.md"))
    assert "a.example" in text, "first run's companies vanished from runs/<date>*.md after a same-day rerun"


def test_existing_hubspot_record_not_duplicated(env):
    env.client, env.hs = fake_client({"a.example": ("99", {"sourcing_qualification_status": "Qualified",
                                                       "sourcing_account_stage": "In Outreach"})})
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    env.run()
    assert env.hs.writes() == []
    assert env.report()["counts"]["hubspot_skipped"] == 1


# ---------------------------------------------------------------- error isolation
def test_per_company_error_isolation(env, monkeypatch):
    monkeypatch.setenv("SOURCING_STUB_SP_FAIL", "d.example")
    env.scenario["source"]["rows"] = [row(d) for d in ("a.example", "b.example", "c.example", "d.example", "e.example", "f.example", "g.example")]
    env.scenario["qualify"] = {
        "a.example": {"crash": True},                                  # claude exits 3 with no JSON
        "b.example": {"garbage": "I could not decide, sorry."},        # no JSON line
        "c.example": V("c.example", domain="other.example"),                   # verdict for the wrong company
        "d.example": V("d.example"),                                       # source pack failed; P1 still OK
        "e.example": {"max_turns": True},                              # error_max_turns
        "f.example": V("f.example", need_strength="VERY_STRONG"),          # bad enum
        "g.example": V("g.example", qualification="MAYBE"),
    }
    assert env.run("--cap", "10") == 0
    q = env.read_queue(DATE)
    st = {d: r["status"] for d, r in q.items()}
    assert st == {"a.example": "error", "b.example": "error", "c.example": "error", "d.example": "done", "e.example": "error",
                  "f.example": "error", "g.example": "done"}
    assert [w[1]["domain"] for w in env.hs.writes()] == ["d.example"]
    rep = env.report()
    assert rep["counts"]["errors"] == 5
    assert all(q[d].get("error") for d in ("a.example", "b.example", "c.example", "e.example", "f.example"))


def test_unhashable_enum_value_does_not_kill_run(env):
    """A verdict with a list where an enum string belongs must be an error line, not a crash of the whole run."""
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", qualification=["QUALIFIED"]),
                               "b.example": V("b.example", qualification="MAYBE")}
    env.run()
    q = env.read_queue(DATE)
    assert q["a.example"]["status"] == "error"
    assert q["b.example"]["status"] == "done"
    assert (env.root / "runs" / f"{DATE}.md").exists()


def test_non_string_domain_does_not_kill_run(env):
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", domain=None), "b.example": V("b.example", qualification="MAYBE")}
    env.run()
    q = env.read_queue(DATE)
    assert q["a.example"]["status"] == "error"
    assert q["b.example"]["status"] == "done"


def test_skill_error_shape(env):
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": {"verdict": {"domain": "a.example", "name": "A", "status": "error",
                                                     "error": "site unreachable"}}}
    env.run()
    q = env.read_queue(DATE)
    assert q["a.example"]["status"] == "error" and "site unreachable" in q["a.example"]["error"]
    assert env.hs.writes() == []


def test_error_retried_next_run_then_given_up(env):
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": {"garbage": "nope"}}
    env.run()
    env.run()
    env.run()
    assert len(env.calls("/qualify")) == 2  # max_attempts = 2
    assert env.read_queue(DATE)["a.example"]["status"] == "error"


def test_timeout_is_error_and_run_continues(env):
    env.settings(timeout_qualify_s=2)
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": {"sleep": 30}, "b.example": V("b.example", qualification="MAYBE")}
    env.run()
    q = env.read_queue(DATE)
    assert q["a.example"]["status"] == "error"
    assert q["b.example"]["status"] == "done"


def test_hubspot_failure_retried_without_claude(env):
    def boom(*a, **kw):
        raise RuntimeError("HubSpot 502")
    env.hs.basic_api.create = boom
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    env.run()
    assert env.read_queue(DATE)["a.example"]["status"] == "hubspot_error"
    env.client, env.hs = fake_client()
    env.reset_calls()
    env.run()
    assert env.calls("/qualify") == []
    assert env.read_queue(DATE)["a.example"]["status"] == "done"
    assert [w[0] for w in env.hs.writes()] == ["create"]


# ---------------------------------------------------------------- vendor backstop
def test_qualified_vendor_not_written(env):
    env.scenario["source"]["rows"] = [row("vendor.example"), row("unclear.example")]
    env.scenario["qualify"] = {"vendor.example": V("vendor.example", buyer_or_vendor="VENDOR"),
                               "unclear.example": V("unclear.example", buyer_or_vendor="UNCLEAR")}
    env.run()
    assert env.hs.writes() == []
    q = env.read_queue(DATE)
    assert q["vendor.example"]["verdict"] == "NOT_QUALIFIED"
    assert env.report()["counts"]["qualified"] == 0


# ---------------------------------------------------------------- usage limit
def test_usage_limit_stops_cleanly_then_resumes(env):
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example"), row("c.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", qualification="MAYBE"), "b.example": {"usage_limit": True},
                               "c.example": V("c.example", qualification="MAYBE")}
    assert env.run() == 0
    q = env.read_queue(DATE)
    assert (q["a.example"]["status"], q["b.example"]["status"], q["c.example"]["status"]) == ("done", "pending", "pending")
    rep = env.report()
    assert rep["stopped_reason"] == "usage_limit"
    assert len(env.calls("/qualify")) == 2          # c.example never attempted
    assert env.calls("/register-results") == []
    md = (env.root / "runs" / f"{DATE}.md").read_text(encoding="utf-8")
    assert "usage_limit" in md
    # next run: only the pending ones
    env.scenario["qualify"]["b.example"] = V("b.example", qualification="MAYBE")
    env.reset_calls()
    env.run("--resume")
    doms = [c["argv"][c["argv"].index("-p") + 1].split()[1] for c in env.calls("/qualify")]
    assert doms == ["b.example", "c.example"]
    assert all(r["status"] == "done" for r in env.read_queue(DATE).values())


def test_usage_limit_during_source(env):
    env.scenario["source"]["mode"] = "usage_limit"
    assert env.run() == 0
    assert env.report()["stopped_reason"] == "usage_limit"
    assert env.calls("/qualify") == []


def test_source_failure_without_backlog(env):
    env.scenario["source"]["mode"] = "fail"
    code = env.run()
    assert code != 0
    assert env.report()["stopped_reason"] == "source_failed"


def test_source_empty_is_not_a_failure(env):
    """/source's build step reports status "empty" and writes no queue file when nothing is net-new.
    That is a normal night, not source_failed / non-zero exit."""
    env.scenario["source"]["mode"] = "empty"
    assert env.run() == 0
    assert env.report()["stopped_reason"] == "no_companies"


def test_source_returns_zero_companies(env):
    env.scenario["source"]["rows"] = []
    assert env.run() == 0
    assert env.report()["stopped_reason"] == "no_companies"
    assert env.calls("/qualify") == []


# ---------------------------------------------------------------- backlog + cap
def test_backlog_from_yesterday_first(env):
    env.queue(YDAY, [row("old.example"), row("olddone.example", status="done")])
    env.scenario["source"]["rows"] = [row("new.example")]
    env.scenario["qualify"] = {"old.example": V("old.example", qualification="MAYBE"),
                               "new.example": V("new.example", qualification="MAYBE")}
    env.run("--cap", "5")
    src = env.calls("/source")
    assert len(src) == 1
    p = src[0]["argv"][src[0]["argv"].index("-p") + 1]
    assert p.split()[-1] == "4"  # cap 5 minus 1 backlog line
    doms = [c["argv"][c["argv"].index("-p") + 1].split()[1] for c in env.calls("/qualify")]
    assert doms == ["old.example", "new.example"]
    assert env.read_queue(YDAY)["old.example"]["status"] == "done"


def test_backlog_fills_cap_skips_source(env):
    env.queue(YDAY, [row("x.example"), row("y.example")])
    env.scenario["qualify"] = {"x.example": V("x.example", qualification="MAYBE"), "y.example": V("y.example", qualification="MAYBE")}
    env.run("--cap", "2")
    assert env.calls("/source") == []


def test_cap_honoured(env):
    env.scenario["source"]["rows"] = [row(f"c{i}.example") for i in range(5)]
    env.scenario["qualify"] = {f"c{i}.example": V(f"c{i}.example", qualification="MAYBE") for i in range(5)}
    env.run("--cap", "2")
    st = [r["status"] for r in env.read_queue(DATE).values()]
    assert st.count("done") == 2 and st.count("pending") == 3
    assert env.report()["stopped_reason"] == "cap_reached"


# ---------------------------------------------------------------- dry run
def test_dry_run_changes_nothing(env):
    env.queue(DATE, [row("a.example"), row("b.example")])
    before = (env.root / "state" / "queue" / f"{DATE}.jsonl").read_bytes()
    env.scenario["qualify"] = {"a.example": V("a.example"), "b.example": V("b.example", qualification="MAYBE")}
    env.run("--dry-run")
    assert (env.root / "state" / "queue" / f"{DATE}.jsonl").read_bytes() == before
    assert env.hs.writes() == []
    assert env.calls("/source") == [] and env.calls("/register-results") == []
    assert (env.root / "runs" / f"{DATE}-dry-run.md").exists()
    rep = env.report(dry=True)
    assert rep["dry_run"] is True and rep["counts"]["qualified"] == 1


def test_dry_run_no_queue_does_not_pull(env):
    env.scenario["source"]["rows"] = [row("a.example")]
    env.run("--dry-run")
    assert env.calls("/source") == []


# ---------------------------------------------------------------- lock
def test_lock_blocks_second_run(env):
    lock = env.root / "state" / "nightly.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="utf-8")
    env.scenario["source"]["rows"] = [row("a.example")]
    assert env.run() != 0
    assert env.calls() == []


# ---------------------------------------------------------------- encoding (real subprocess, cp1252 locale)
def test_utf8_names_under_legacy_locale(tmp_path, repo):
    root = tmp_path / "root"
    home = tmp_path / "home"
    home.mkdir()
    names = {"ecf.example": "Épicerie Côté Frères", "mf.example": "Mercado Fictício — Brasil"}
    q = root / "state" / "queue" / f"{DATE}.jsonl"
    q.parent.mkdir(parents=True)
    q.write_text("".join(json.dumps(row(d, n), ensure_ascii=False) + "\n" for d, n in names.items()), encoding="utf-8")
    scen = tmp_path / "s.json"
    scen.write_text(json.dumps({"qualify": {d: {"verdict": good_verdict(domain=d, name=n, qualification="MAYBE")}
                                            for d, n in names.items()}}, ensure_ascii=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("HUBSPOT_TOKEN", "PYTHONIOENCODING")}
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.json").write_text('{"hubspot_write": false, "register_results": false}', encoding="utf-8")
    env.update(PYTHONUTF8="0", USERPROFILE=str(home), HOME=str(home), SOURCING_ROOT=str(root),
               SOURCING_CLAUDE_BIN=str(STUBS / "fake_claude.py"), SOURCING_STUB_SCENARIO=str(scen),
               SOURCING_SOURCE_PACK_CMD="{python} " + str(STUBS / "fake_source_pack.py").replace("\\", "/") + " {domain}")
    r = subprocess.run([sys.executable, str(repo / "scripts" / "run_nightly.py"), "--date", DATE, "--resume",
                        "--root", str(root)], env=env, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    md = (root / "runs" / f"{DATE}.md").read_text(encoding="utf-8")
    for n in names.values():
        assert n in md
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines()]
    assert {r["name"] for r in rows} == set(names.values())
    assert all(r["status"] == "done" for r in rows)


# ---------------------------------------------------------------- v2 behaviour
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_replay_full_cli_result(env):
    """A complete CLI result object (report prose followed by the verdict line, fictional company) goes through the
    whole driver: parsed, written to the CRM, prose saved without the JSON line."""
    d = "harrow-and-finch.example"
    env.scenario["source"]["rows"] = [row(d, "Harrow & Finch Grocers")]
    env.scenario["qualify"] = {d: {"raw_file": str(FIXTURES / "claude_result_sample.json")}}
    env.run()
    q = env.read_queue(DATE)[d]
    assert q["status"] == "done" and q["verdict"] == "QUALIFIED" and q["hubspot_action"] == "created"
    created = env.hs.writes()[0][1]
    assert created["domain"] == d and created["sourcing_account_tier"] == "Tier A"
    assert created["sourcing_timing"] == "Active Trigger"
    wd = env.root / "state" / "work" / d
    p1 = (wd / "p1.md").read_text(encoding="utf-8")
    assert len(p1) > 1000 and not p1.rstrip().endswith("}")   # JSON line stripped from the prose
    assert json.loads((wd / "p1.json").read_text(encoding="utf-8"))["domain"] == d


def test_downgraded_company_is_stored_in_the_lowest_tier(env):
    env.scenario["source"]["rows"] = [row("vendor.example"), row("low.example")]
    env.scenario["qualify"] = {"vendor.example": V("vendor.example", buyer_or_vendor="VENDOR", account_tier="TIER_A"),
                               "low.example": V("low.example", qualification="NOT_QUALIFIED", account_tier="TIER_B")}
    env.run()
    by = {c["domain"]: c for c in env.report()["companies"]}
    for d in ("vendor.example", "low.example"):
        assert by[d]["verdict"] == "NOT_QUALIFIED" and by[d]["account_tier"] == "TIER_C"
        saved = json.loads((env.root / "state" / "work" / d / "p1.json").read_text(encoding="utf-8"))
        assert saved["account_tier"] == "TIER_C"
    assert "tier TIER_A->TIER_C" in by["vendor.example"]["backstop"]
    assert env.hs.writes() == []


def test_qualified_lowest_tier_is_held_by_the_shipped_setting(env, repo):
    """A QUALIFIED verdict in TIER_C contradicts itself. The shipped hubspot_tiers keeps it out of the CRM."""
    shipped = json.loads((repo / "config" / "settings.json").read_text(encoding="utf-8"))["hubspot_tiers"]
    assert "TIER_C" not in shipped
    env.settings(hubspot_tiers=shipped)
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", account_tier="TIER_C")}
    env.run()
    co = env.report()["companies"][0]
    assert co["verdict"] == "QUALIFIED" and co["hubspot_action"] == "held"
    assert "contradicts its tier" in co["backstop"]
    assert env.hs.writes() == []


def test_lower_tier_held_out_of_crm(env):
    """hubspot_tiers keeps qualified accounts of other tiers out of the CRM and lists them in a CSV."""
    env.settings(hubspot_tiers=["TIER_A"])
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", account_tier="TIER_A"),
                               "b.example": V("b.example", account_tier="TIER_B")}
    env.run()
    assert [w[1]["domain"] for w in env.hs.writes()] == ["a.example"]
    q = env.read_queue(DATE)
    assert q["b.example"]["status"] == "done" and q["b.example"]["hubspot_action"] == "held"
    assert env.report()["counts"]["held"] == 1
    held = (env.root / rn.HELD_CSV).read_text(encoding="utf-8-sig")
    assert "b.example" in held and "TIER_B" in held and "a.example" not in held


def test_page_fetch_and_stealth_tools_only_when_enabled(tmp_path):
    def allowed(settings):
        a = rn.qualify_args("a.example", "A", "opus", settings, root=tmp_path)
        servers = json.loads(Path(a[a.index("--mcp-config") + 1]).read_text(encoding="utf-8"))["mcpServers"]
        return a[a.index("--allowedTools") + 1].split(","), sorted(servers)

    base = ["Read", "Glob", "Grep", "WebSearch"]
    for off in ({}, {"crawler": {}}, {"crawler": {"session_page_fetch": False}},
                {"crawler": {"session_page_fetch": "yes"}},                       # only a JSON true counts
                {"crawler": {"stealth_fallback": True}},                          # stealth alone gives nothing
                {"qualify_mcp_tools": [rn.STEALTH_MCP_TOOL, "mcp__scrapling__fetch"]}):
        assert allowed(off) == (base, []), off                                    # no tool and no server loaded
    tools, servers = allowed({"crawler": {"session_page_fetch": True}})
    assert tools == base + rn.DEFAULT_QUALIFY_MCP_TOOLS and servers == ["scrapling"]
    assert rn.STEALTH_MCP_TOOL not in tools
    tools, _ = allowed({"crawler": {"session_page_fetch": True}, "qualify_mcp_tools": [rn.STEALTH_MCP_TOOL]})
    assert rn.STEALTH_MCP_TOOL not in tools
    tools, _ = allowed({"crawler": {"session_page_fetch": True, "stealth_fallback": True}})
    assert tools == base + rn.DEFAULT_QUALIFY_MCP_TOOLS + [rn.STEALTH_MCP_TOOL]


# ---------------------------------------------------------------- how Claude is authenticated
def helper_settings(env):
    d = Path(os.environ["USERPROFILE"]) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text('{"apiKeyHelper": "echo key"}', encoding="utf-8")


def test_subscription_mode_is_the_default_and_removes_api_credentials(env):
    env.mp.setenv("ANTHROPIC_AUTH_TOKEN", "token-should-not-leak")
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    assert env.run() == 0
    assert env.report()["claude_auth"] == "subscription"
    calls = env.calls()
    assert calls and not any(c["has_anthropic_key"] or c["has_anthropic_token"] or c["has_hubspot_token"] for c in calls)
    assert rn.stripped_env("subscription") == ("HUBSPOT_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def test_subscription_mode_refuses_to_start_with_an_api_key_helper(env):
    helper_settings(env)
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    assert env.run() == 3
    rep = env.report()
    assert rep["stopped_reason"] == "api_key_guard" and "apiKeyHelper" in rep["stopped_detail"]
    assert env.calls() == [] and env.hs.writes() == []


def test_api_key_mode_passes_credentials_through_and_refuses_nothing(env):
    helper_settings(env)
    env.settings(claude_auth="api_key")
    env.mp.setenv("ANTHROPIC_AUTH_TOKEN", "token-for-the-child")
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    assert env.run() == 0
    rep = env.report()
    assert rep["claude_auth"] == "api_key" and rep["stopped_reason"] in (None, "completed")
    calls = env.calls()
    assert {p for c in calls for p in c["argv"] if p.startswith("/")} >= {"/source 2030-01-15 50"}
    assert all(c["has_anthropic_key"] and c["has_anthropic_token"] for c in calls)
    assert not any(c["has_hubspot_token"] for c in calls)          # the CRM token stays in the driver in every mode
    assert [w[1]["domain"] for w in env.hs.writes()] == ["a.example"]
    assert rn.stripped_env("api_key") == ("HUBSPOT_TOKEN",)


@pytest.mark.parametrize("value", ["apikey", "API_KEY", "", None, 1, ["subscription"]])
def test_unknown_auth_mode_stops_the_run(env, value):
    env.settings(claude_auth=value)
    env.scenario["source"]["rows"] = [row("a.example")]
    assert env.run() == 2
    rep = env.report()
    assert rep["stopped_reason"] == "bad_settings" and "claude_auth" in rep["stopped_detail"]
    assert env.calls() == []


def test_auth_mode_does_not_leak_from_one_run_to_the_next(env):
    env.settings(claude_auth="api_key")
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", qualification="NOT_QUALIFIED")}
    env.run()
    assert rn.child_env().get("ANTHROPIC_API_KEY") == "sk-should-not-leak"
    env.settings()
    env.run()
    assert "ANTHROPIC_API_KEY" not in rn.child_env()


# ---------------------------------------------------------------- crawler identity
def test_no_site_is_crawled_under_the_placeholder_user_agent(env, monkeypatch):
    monkeypatch.delenv("SOURCING_SOURCE_PACK_CMD")               # the real crawler would be used
    started = []
    monkeypatch.setattr(rn, "run_source_pack", lambda *a, **k: started.append(a) or {"rc": 0})
    env.settings(crawler={"user_agent": "account-sourcing-pipeline/0.1 (+contact: set this to your own address)"})
    env.queue(DATE, [row("a.example")])
    env.scenario["qualify"] = {"a.example": V("a.example")}
    assert env.run("--resume") == 0
    rep = env.report()
    assert started == []
    assert any("No site was crawled" in n and "placeholder" in n for n in rep["notes"])
    assert rep["companies"][0]["source_pack"] == "not run (placeholder user agent)"
    assert rep["companies"][0]["status"] == "done"                # qualification still ran


def test_crawler_runs_under_a_real_user_agent(env, monkeypatch):
    monkeypatch.delenv("SOURCING_SOURCE_PACK_CMD")
    started = []
    monkeypatch.setattr(rn, "run_source_pack", lambda d, *a, **k: started.append(d) or {"rc": 0})
    env.settings(crawler={"user_agent": "example-crawler/1 (+contact: ops at the owner's address)"})
    env.queue(DATE, [row("a.example")])
    env.scenario["qualify"] = {"a.example": V("a.example")}
    assert env.run("--resume") == 0
    assert started == ["a.example"] and env.report()["notes"] == []


def test_shipped_settings_would_not_crawl(repo):
    assert "placeholder" in rn.crawler_problem(repo)


# ---------------------------------------------------------------- crawl status in the report
def test_blocked_site_is_named_in_the_report(env, monkeypatch):
    monkeypatch.setenv("SOURCING_STUB_SP_BLOCKED", "b.example")
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": V("a.example"), "b.example": V("b.example", qualification="NOT_QUALIFIED")}
    assert env.run() == 0
    rep = env.report()
    by = {c["domain"]: c for c in rep["companies"]}
    assert by["b.example"]["crawl"] == "blocked (http 403)" and "crawl" not in by["a.example"]
    assert by["b.example"]["status"] == "done"                      # qualification still ran, from web search
    md = (env.root / "runs" / f"{DATE}.md").read_text(encoding="utf-8")
    assert "[crawl: blocked (http 403)]" in md


def test_empty_result_zero_turns_is_error(env):
    env.scenario["source"]["rows"] = [row("a.example"), row("b.example")]
    env.scenario["qualify"] = {"a.example": {"empty_zero_turns": True}, "b.example": V("b.example", qualification="MAYBE")}
    env.run()
    q = env.read_queue(DATE)
    assert q["a.example"]["status"] == "error" and q["b.example"]["status"] == "done"


def test_rerun_report_suffix(env):
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example", qualification="MAYBE")}
    env.run()
    env.run()
    env.run()
    names = sorted(p.name for p in (env.root / "runs").glob(f"{DATE}*.md"))
    assert names == [f"{DATE}-2.md", f"{DATE}-3.md", f"{DATE}.md"]


@pytest.mark.parametrize("existing_status,action", [("Needs Review", "human_verdict_exists")])
def test_needs_review_terminal(env, existing_status, action):
    env.client, env.hs = fake_client({"a.example": ("7", {"sourcing_qualification_status": existing_status})})
    env.scenario["source"]["rows"] = [row("a.example")]
    env.scenario["qualify"] = {"a.example": V("a.example")}
    env.run()
    assert env.read_queue(DATE)["a.example"]["status"] == "needs_review"
    assert env.report()["counts"]["needs_review"] == 1
    env.reset_calls()
    env.run()
    assert env.calls("/qualify") == []  # not retried
    assert env.hs.writes() == []


def test_hubspot_unavailable_stops_before_claude(env, monkeypatch):
    monkeypatch.setattr(hw, "get_client", lambda: None)
    env.queue(DATE, [row("a.example")])
    env.scenario["qualify"] = {"a.example": V("a.example")}
    env.run()
    assert env.report()["stopped_reason"] == "hubspot_unavailable"
    assert env.calls("/qualify") == []
    assert env.read_queue(DATE)["a.example"]["status"] == "pending"


def test_driver_crash_still_writes_report(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(rn, "read_queue", boom)
    env.queue(DATE, [row("a.example")])
    env.run()
    rep = env.report()
    assert rep["stopped_reason"] == "driver_crash"


def test_orphan_source_date_resumed(env):
    """candidates.json for yesterday but no queue file (a /source that died after registering) -> /source <yday> first."""
    d = env.root / "state" / "source" / YDAY
    d.mkdir(parents=True)
    (d / "candidates.json").write_text("{}", encoding="utf-8")
    env.scenario["source"]["rows"] = []
    env.scenario["source"]["mode"] = "empty"
    env.run("--cap", "3")
    prompts = [c["argv"][c["argv"].index("-p") + 1] for c in env.calls("/source")]
    assert prompts and prompts[0].startswith(f"/source {YDAY}")
