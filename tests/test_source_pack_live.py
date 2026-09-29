"""source_pack.py <domain> --light against a real public site (no credits, no CRM).
Opt-in: set SOURCING_LIVE_DOMAIN to a public site you may crawl, then run pytest tests --live-web"""
import json
import os
import re
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.live_web

DOMAIN = os.environ.get("SOURCING_LIVE_DOMAIN", "")


@pytest.fixture(scope="module")
def pack(tmp_path_factory, repo):
    if not DOMAIN:
        pytest.skip("set SOURCING_LIVE_DOMAIN to the public site to crawl")
    out = tmp_path_factory.mktemp("pack")
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(repo / "scripts" / "source_pack.py"), DOMAIN, "--light", "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    return {"out": out, "r": r, "secs": time.monotonic() - t0}


def test_exit_zero(pack):
    assert pack["r"].returncode == 0, pack["r"].stderr[-2000:]


def test_under_two_minutes(pack):
    assert pack["secs"] < 120, pack["secs"]


def test_outputs_exist(pack):
    out = pack["out"]
    assert (out / "index.txt").exists()
    assert (out / "jobs.json").exists()
    json.loads((out / "jobs.json").read_text(encoding="utf-8"))


def test_jobs_txt_compact(pack):
    t = (pack["out"] / "jobs.txt").read_text(encoding="utf-8")
    assert t.startswith("# source:")
    assert len(t) < 40_000, len(t)  # P1 reads this, never jobs.json


def test_every_md_is_indexed(pack):
    idx = (pack["out"] / "index.txt").read_text(encoding="utf-8")
    n_idx = len([l for l in idx.splitlines() if l.strip()])
    assert len(list(pack["out"].glob("*.md"))) == n_idx


def test_has_pages(pack):
    last = pack["r"].stdout.strip().splitlines()[-1]
    m = re.match(r"(\d+) usable pages -> ", last)
    assert m, last
    assert int(m.group(1)) >= 1
    assert list(pack["out"].glob("*.md"))
    assert int(m.group(1)) <= 15  # --light page cap


def test_bad_domain_does_not_crash(tmp_path, repo):
    r = subprocess.run([sys.executable, str(repo / "scripts" / "source_pack.py"), "no-such-domain-sourcing-qa.invalid",
                        "--light", "--out", str(tmp_path), "--timeout", "30"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert (tmp_path / "index.txt").exists() and (tmp_path / "jobs.json").exists()
