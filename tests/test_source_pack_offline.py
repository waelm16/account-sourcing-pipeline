"""source_pack.py end to end with every network call replaced (tests/stubs/run_source_pack_offline.py).
Covers: robots.txt before every request, refused sites recorded and skipped, the stealth option,
the client identity in both modes, and what the crawler takes from the signals file."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "stubs" / "run_source_pack_offline.py"
PAGES = Path(__file__).resolve().parent / "fixtures" / "pages"
FEED = Path(__file__).resolve().parent / "fixtures" / "job_feed.json"
D = "grocer.example"
HOME, WWW = f"https://{D}/", f"https://www.{D}/"
UA = "test-crawler/1 (+contact: tests)"
PLAIN = {"impersonate": None, "stealthy_headers": False, "headers": {"User-Agent": UA}}
OK_HOME = {"status": 200, "body": "<html><head><title>Grocer</title></head><body>hello</body></html>"}


def html(name):
    return (PAGES / f"{name}.html").read_text(encoding="utf-8")


def clean_env(force_blocked=False):
    """The machine's own environment must not decide the outcome."""
    env = {k: v for k, v in os.environ.items() if k != "SOURCE_PACK_FORCE_STEALTH"}
    if force_blocked:
        env["SOURCE_PACK_FORCE_STEALTH"] = "1"
    return env


def crawler(**over):
    return {"crawler": dict({"user_agent": UA}, **over)}


def run(tmp_path, scenario, *args, settings=None, signals=None, force_blocked=False, positional=()):
    log = tmp_path / "log.json"
    scen = tmp_path / "scenario.json"
    scen.write_text(json.dumps(dict(scenario, log=str(log))), encoding="utf-8")
    out = tmp_path / "out"
    s = tmp_path / "settings.json"
    s.write_text(json.dumps(crawler() if settings is None else settings), encoding="utf-8")
    cmd = [sys.executable, str(HARNESS), str(scen), D, *positional, "--light", "--out", str(out),
           "--timeout", "60", "--settings", str(s), *args]
    if signals is not None:
        cmd += ["--signals", str(signals)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                       env=clean_env(force_blocked))
    res = {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr, "out": out,
           "log": json.loads(log.read_text(encoding="utf-8")) if log.exists() else None, "status": None}
    if (out / "status.json").exists():
        res["status"] = json.loads((out / "status.json").read_text(encoding="utf-8"))
    res["urls"] = [f["url"] for f in res["log"]["fetch"]] if res["log"] else []
    return res


def site(home, **more):
    """A site whose two hosts resolve. `home` answers on both."""
    return {"live_hosts": [D, f"www.{D}"], "responses": dict({HOME: home, WWW: home}, **more)}


def assert_not_crawled(r, status, reason):
    assert r["rc"] == 0, r["stderr"][-2000:]
    assert r["status"]["status"] == status and r["status"]["reason"] == reason and r["status"]["pages"] == 0
    assert r["log"]["spider"] is None and r["log"]["stealth"] == []
    assert not any("sitemap" in u for u in r["urls"])
    assert not any(h in u for u in r["urls"] for h in ("lever.co", "greenhouse.io", "ashbyhq.com"))
    assert (r["out"] / "index.txt").read_text(encoding="utf-8") == ""
    assert json.loads((r["out"] / "jobs.json").read_text(encoding="utf-8")) == {"jobs": [], "source": None}
    assert (r["out"] / "jobs.txt").read_text(encoding="utf-8").startswith(f"# source: none (site {status}")
    assert f"site {status} ({reason})" in r["stdout"]
    assert r["stdout"].strip().splitlines()[-1].startswith("0 usable pages -> ")


# ---------------------------------------------------------------- a refused site is recorded and skipped
@pytest.mark.parametrize("code", [401, 403, 407, 429, 503])
def test_refused_site_is_recorded_as_blocked_and_skipped(tmp_path, code):
    r = run(tmp_path, site({"status": code, "body": "no"}))
    assert_not_crawled(r, "blocked", f"http {code}")
    # the only requests: robots.txt of each host, then the homepage on each host
    assert r["urls"] == [f"https://{D}/robots.txt", HOME, f"https://www.{D}/robots.txt", WWW]
    assert r["status"]["stealth_used"] is False


def test_bot_challenge_counts_as_refused(tmp_path):
    r = run(tmp_path, site({"status": 200, "body": html("challenge")}))
    assert_not_crawled(r, "blocked", "bot challenge")


def test_site_that_does_not_answer_is_recorded_as_unreachable(tmp_path):
    r = run(tmp_path, {"live_hosts": [D], "responses": {}})
    assert_not_crawled(r, "unreachable", "no answer")


@pytest.mark.parametrize("over", [{}, {"stealth_fallback": False}, {"stealth_fallback": "yes"},
                                  {"stealth_fallback": 1}, {"stealth_fallback": None}])
def test_stealth_is_off_unless_the_setting_is_true(tmp_path, over):
    r = run(tmp_path, site({"status": 403, "body": "no"}), settings=crawler(**over))
    assert_not_crawled(r, "blocked", "http 403")


# ---------------------------------------------------------------- the crawler must say who it is
SHIPPED_UA = "account-sourcing-pipeline/0.1 (+contact: set this to your own address)"


@pytest.mark.parametrize("settings", [{}, {"crawler": "broken"}, {"crawler": {}}, {"crawler": {"user_agent": ""}},
                                      {"crawler": {"user_agent": SHIPPED_UA}},
                                      {"crawler": {"user_agent": "MyBot/2 (+contact: SET THIS TO YOUR OWN address)"}},
                                      {"crawler": {"user_agent": SHIPPED_UA, "stealth_fallback": True}}])
def test_placeholder_user_agent_refuses_to_crawl(tmp_path, settings):
    r = run(tmp_path, site(OK_HOME), settings=settings)
    assert r["rc"] == 2
    assert "crawler config error" in r["stderr"] and "crawler.user_agent" in r["stderr"]
    assert "placeholder" in r["stderr"]
    assert r["log"]["fetch"] == [] and r["log"]["stealth"] == [] and r["log"]["spider"] is None
    assert not r["out"].exists()                                  # no request made, nothing written


def test_shipped_settings_refuse_to_crawl(tmp_path):
    shipped = json.loads((Path(__file__).resolve().parents[1] / "config" / "settings.json").read_text(encoding="utf-8"))
    r = run(tmp_path, site(OK_HOME), settings=shipped)
    assert r["rc"] == 2 and "placeholder" in r["stderr"] and r["log"]["fetch"] == []


def test_placeholder_is_not_checked_when_posing_as_a_browser(tmp_path):
    """With impersonation on the user agent is not sent, so its value does not matter."""
    r = run(tmp_path, site(OK_HOME), settings={"crawler": {"user_agent": SHIPPED_UA, "impersonate_browser": True}})
    assert r["rc"] == 0 and r["status"]["status"] == "ok"


def test_forcing_the_refused_state_does_not_turn_stealth_on(tmp_path):
    r = run(tmp_path, site(OK_HOME), force_blocked=True)
    assert_not_crawled(r, "blocked", "forced by test hook")


# ---------------------------------------------------------------- robots.txt before every request
def test_robots_disallowing_the_homepage_stops_everything(tmp_path):
    robots = {"status": 200, "body": "User-agent: *\nDisallow: /\n"}
    scen = site(OK_HOME, **{f"https://{D}/robots.txt": robots, f"https://www.{D}/robots.txt": robots})
    r = run(tmp_path, scen)
    assert_not_crawled(r, "disallowed", "robots.txt does not allow the homepage")
    assert r["urls"] == [f"https://{D}/robots.txt", f"https://www.{D}/robots.txt"]      # the homepage was never asked for
    assert r["status"]["skipped_by_robots"] == 2


@pytest.mark.parametrize("answer", [{"status": 401, "body": ""}, {"status": 403, "body": ""},
                                    {"status": 500, "body": ""}, {"status": 503, "body": ""}])
def test_refused_or_failing_robots_txt_means_nothing_is_allowed(tmp_path, answer):
    scen = site(OK_HOME, **{f"https://{D}/robots.txt": answer, f"https://www.{D}/robots.txt": answer})
    r = run(tmp_path, scen)
    assert_not_crawled(r, "disallowed", "robots.txt does not allow the homepage")
    assert HOME not in r["urls"] and WWW not in r["urls"]


def test_unreachable_robots_txt_means_nothing_is_allowed(tmp_path):
    r = run(tmp_path, dict(site(OK_HOME), robots_default="raise"))
    assert_not_crawled(r, "disallowed", "robots.txt does not allow the homepage")


def test_rules_for_our_own_user_agent_are_obeyed(tmp_path):
    robots = {"status": 200, "body": "User-agent: *\nAllow: /\n\nUser-agent: test-crawler\nDisallow: /\n"}
    scen = site(OK_HOME, **{f"https://{D}/robots.txt": robots, f"https://www.{D}/robots.txt": robots})
    assert_not_crawled(run(tmp_path, scen), "disallowed", "robots.txt does not allow the homepage")
    # posing as a browser, the same file is matched as "*", which it allows
    r = run(tmp_path, scen, settings=crawler(impersonate_browser=True))
    assert r["status"]["status"] == "ok" and r["log"]["spider"] is not None


def test_sitemaps_seeds_and_nested_sitemaps_are_checked(tmp_path):
    robots = {"status": 200, "body": "User-agent: *\nDisallow: /careers\nDisallow: /sitemap_index.xml\n"
                                     "Disallow: /maps/private.xml\n"}
    locs = "".join(f"<url><loc>https://{D}{p}</loc></url>" for p in (
        "/stores/halifax", "/careers/baker", "/maps/private.xml", "/maps/public.xml"))
    scen = {"live_hosts": [D], "responses": {
        HOME: OK_HOME, f"https://{D}/robots.txt": robots,
        f"https://{D}/sitemap.xml": {"status": 200, "body": f"<urlset>{locs}</urlset>"},
        f"https://{D}/maps/public.xml": {"status": 200, "body": f"<urlset><url><loc>https://{D}/news/opening</loc></url></urlset>"}}}
    r = run(tmp_path, scen)
    assert r["rc"] == 0 and r["status"]["status"] == "ok"
    assert f"https://{D}/sitemap.xml" in r["urls"] and f"https://{D}/maps/public.xml" in r["urls"]
    assert f"https://{D}/sitemap_index.xml" not in r["urls"]          # disallowed sitemap file
    assert f"https://{D}/maps/private.xml" not in r["urls"]           # disallowed nested sitemap
    seeds = set(r["log"]["spider"]["start_urls"])
    assert {f"https://{D}/stores/halifax", f"https://{D}/news/opening", f"https://{D}/stores"} <= seeds
    assert not any("/careers" in s for s in seeds)                    # seed path and sitemap URL both dropped
    assert not any(s.startswith(f"https://www.{D}") for s in seeds)   # that host does not resolve
    assert r["log"]["spider"]["robots_txt_obey"] is True
    assert r["status"]["skipped_by_robots"] >= 3


def test_job_feed_is_checked_against_the_job_board_robots_txt(tmp_path):
    feed = "https://api.lever.co/v0/postings/grocer?mode=json"
    scen = {"live_hosts": [], "responses": {
        feed: {"status": 200, "body": FEED.read_text(encoding="utf-8")},
        "https://api.lever.co/robots.txt": {"status": 200, "body": "User-agent: *\nDisallow: /\n"}}}
    r = run(tmp_path, scen)
    assert r["rc"] == 0 and feed not in r["urls"]
    assert "job feed skipped, robots.txt disallows " + feed in r["stdout"]
    assert (r["out"] / "jobs.txt").read_text(encoding="utf-8").startswith("# source: none")
    assert r["status"]["status"] == "no_host"


def test_every_request_is_preceded_by_its_robots_txt(tmp_path):
    scen = {"live_hosts": [D, f"www.{D}"], "responses": {
        HOME: OK_HOME, f"https://{D}/sitemap.xml": {"status": 200, "body": "<urlset></urlset>"},
        "https://api.lever.co/v0/postings/grocer?mode=json": {"status": 200, "body": FEED.read_text(encoding="utf-8")}}}
    r = run(tmp_path, scen)
    assert r["rc"] == 0, r["stderr"][-2000:]
    seen = set()
    for u in r["urls"]:
        host = u.split("/")[2]
        if u.endswith("/robots.txt"):
            assert host not in seen, "robots.txt is read once per host"
            seen.add(host)
        else:
            assert host in seen, f"{u} was requested before the robots.txt of {host}"
    assert {D, "api.lever.co"} <= seen


# ---------------------------------------------------------------- the stealth option
def stealth_scenario(**stealth):
    return dict(site({"status": 403, "body": "no"}), stealth=stealth)


def test_stealth_runs_only_when_switched_on(tmp_path):
    scen = stealth_scenario(**{WWW: {"status": 200, "body": html("stores")},
                               f"https://www.{D}/careers": {"status": 200, "body": html("careers")}})
    r = run(tmp_path, scen, settings=crawler(stealth_fallback=True))
    assert r["rc"] == 0, r["stderr"][-2000:]
    assert r["log"]["stealth"] == [WWW, f"https://www.{D}/careers"]          # light.stealth_paths
    assert r["status"]["status"] == "ok" and r["status"]["stealth_used"] is True and r["status"]["pages"] == 2
    lines = (r["out"] / "index.txt").read_text(encoding="utf-8").splitlines()
    url, title, terms, words = lines[0].split(" | ")
    assert url == WWW and title == "Our stores / Harrow & Finch Grocers"
    assert terms == "distribution centre,food waste,fresh departments,fresh produce" and int(words) > 60
    assert lines[1].split(" | ")[2] == "-"
    assert "topic-term pages: 1" in r["stdout"]
    # the job board link on the careers page was found and used
    assert "https://api.lever.co/v0/postings/harrowfinch?mode=json" in r["urls"]


def test_stealth_also_obeys_robots_txt(tmp_path):
    robots = {"status": 200, "body": "User-agent: *\nDisallow: /careers\n"}
    scen = stealth_scenario(**{WWW: {"status": 200, "body": html("stores")},
                               f"https://www.{D}/careers": {"status": 200, "body": html("careers")}})
    scen["responses"][f"https://www.{D}/robots.txt"] = robots
    r = run(tmp_path, scen, settings=crawler(stealth_fallback=True))
    assert r["log"]["stealth"] == [WWW]
    assert "stealth fetch skipped, robots.txt disallows" in r["stdout"]


def test_stealth_that_gets_nothing_leaves_the_site_blocked(tmp_path):
    scen = stealth_scenario(**{WWW: {"status": 200, "body": html("challenge")}})
    r = run(tmp_path, scen, settings=crawler(stealth_fallback=True))
    assert r["rc"] == 0 and r["status"]["status"] == "blocked" and r["status"]["stealth_used"] is True
    assert r["log"]["spider"] is None and r["status"]["pages"] == 0


def test_no_page_is_refetched_when_the_homepage_answers(tmp_path):
    r = run(tmp_path, site(OK_HOME), settings=crawler(stealth_fallback=True))
    assert r["rc"] == 0 and r["log"]["stealth"] == [] and r["status"]["stealth_used"] is False


# ---------------------------------------------------------------- client identity
def test_plain_identity_by_default(tmp_path):
    scen = site(OK_HOME, **{f"https://{D}/sitemap.xml": {"status": 200, "body": "<urlset></urlset>"}})
    r = run(tmp_path, scen)
    assert r["rc"] == 0 and len(r["log"]["fetch"]) >= 5
    for f in r["log"]["fetch"]:                                   # robots.txt, homepage, sitemaps, job feeds
        assert f["kwargs"] == PLAIN, f
    assert r["log"]["session"] == {"impersonate": None, "stealthy_headers": False, "headers": {"User-Agent": UA}}
    assert r["log"]["spider_robots_agent"] == UA
    assert r["status"]["impersonate_browser"] is False and r["status"]["robots_agent"] == UA


def test_browser_identity_only_when_switched_on(tmp_path):
    r = run(tmp_path, site(OK_HOME), settings=crawler(impersonate_browser=True))
    assert r["rc"] == 0
    for f in r["log"]["fetch"]:
        assert f["kwargs"] == {}, f                               # nothing passed: the library's defaults apply
    assert r["log"]["session"] == {"impersonate": "chrome", "stealthy_headers": True, "headers": {}}
    assert r["log"]["spider_robots_agent"] == "*"
    assert r["status"]["impersonate_browser"] is True and r["status"]["robots_agent"] == "*"


# ---------------------------------------------------------------- seeds and link rules come from the signals file
def custom_signals(tmp_path):
    mode = {"probe_subdomains": ["www", "jobs"], "seed_subdomains": ["jobs"], "seed_paths": ["alpha", "beta"],
            "sitemap_subdomains": [], "skip_patterns": ["/light-only/"], "stealth_paths": [""]}
    cfg = {"topic_terms": ["zebra\\w*"], "role_terms": ["keeper"], "path_tokens": ["alpha", "beta", "gamma"],
           "priority_paths": ["gamma"], "secondary_paths": ["alpha"], "skip_patterns": ["/never/"],
           "extra_allow_patterns": [], "extra_sitemap_patterns": [], "light": mode, "full": dict(mode, skip_patterns=[])}
    p = tmp_path / "custom-signals.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_spider_gets_seeds_and_rules_from_the_file(tmp_path):
    sitemap = "".join(f"<url><loc>https://{D}{p}</loc></url>" for p in (
        "/gamma/one", "/alpha/two", "/beta/three-long-path", "/delta/four", "/never/gamma", "/light-only/alpha",
        "/gamma/file.pdf"))
    scen = {"live_hosts": [D, f"jobs.{D}"],
            "responses": {HOME: OK_HOME, f"https://{D}/sitemap.xml": {"status": 200, "body": f"<urlset>{sitemap}</urlset>"}}}
    r = run(tmp_path, scen, signals=custom_signals(tmp_path))
    assert r["rc"] == 0, r["stderr"][-2000:]
    sp = r["log"]["spider"]
    assert sp["robots_txt_obey"] is True and sp["max_pages"] == 15
    assert sp["allowed_domains"] == sorted([D, f"jobs.{D}"])
    assert set(sp["start_urls"]) == {
        HOME, f"https://jobs.{D}/",                                              # home and the live seed subdomain
        f"https://{D}/alpha", f"https://{D}/beta",                               # seed_paths
        f"https://{D}/gamma/one", f"https://{D}/alpha/two", f"https://{D}/beta/three-long-path"}   # sitemap picks
    assert r["log"]["deny"][-2:] == ["/never/", "/light-only/"]
    assert "alpha|beta|gamma" in r["log"]["allow"][0]
    assert "sitemap candidates: 3" in r["stdout"]


def test_sitemap_candidates_are_ranked_before_the_seed_cap(tmp_path):
    sitemap = "".join(f"<url><loc>https://{D}{p}</loc></url>" for p in ("/beta/b", "/alpha/a", "/gamma/g"))
    scen = {"live_hosts": [D],
            "responses": {HOME: OK_HOME, f"https://{D}/sitemap.xml": {"status": 200, "body": f"<urlset>{sitemap}</urlset>"}}}
    # positional arguments: max_pages 15, seed_cap 1 -> only the best-ranked sitemap URL is seeded
    r = run(tmp_path, scen, signals=custom_signals(tmp_path), positional=("15", "1"))
    assert r["rc"] == 0, r["stderr"][-2000:]
    seeds = set(r["log"]["spider"]["start_urls"])
    assert f"https://{D}/gamma/g" in seeds                       # priority_paths win
    assert f"https://{D}/alpha/a" not in seeds and f"https://{D}/beta/b" not in seeds


# ---------------------------------------------------------------- job feed
def test_job_listing_is_written_from_the_feed(tmp_path):
    feed = "https://api.lever.co/v0/postings/grocer?mode=json"
    scen = {"live_hosts": [], "responses": {feed: {"status": 200, "body": FEED.read_text(encoding="utf-8")}}}
    r = run(tmp_path, scen)
    assert r["rc"] == 0, r["stderr"][-2000:]
    assert r["log"]["spider"] is None and "crawl skipped" in r["stdout"]          # no live host
    assert r["urls"] == ["https://api.lever.co/robots.txt", feed]
    lines = (r["out"] / "jobs.txt").read_text(encoding="utf-8").splitlines()
    head, body = [l for l in lines if l.startswith("#")], [l for l in lines if not l.startswith("#")]
    assert "slug_origin: guessed" in head[1]
    assert [l.split(" | ")[0] for l in body] == ["Replenishment Analyst", "Category Manager, Produce", "Baker", "Cashier"]
    assert json.loads((r["out"] / "jobs.json").read_text(encoding="utf-8")) == json.loads(FEED.read_text(encoding="utf-8"))


def test_no_feed_and_no_pages_still_writes_the_files(tmp_path):
    r = run(tmp_path, {"live_hosts": []})
    assert r["rc"] == 0 and r["status"]["status"] == "no_host"
    assert json.loads((r["out"] / "jobs.json").read_text(encoding="utf-8")) == {"jobs": [], "source": None}
    assert (r["out"] / "jobs.txt").read_text(encoding="utf-8").startswith("# source: none")
    assert (r["out"] / "index.txt").exists()


def test_a_second_run_clears_the_first(tmp_path):
    scen = stealth_scenario(**{WWW: {"status": 200, "body": html("stores")}})
    first = run(tmp_path, scen, settings=crawler(stealth_fallback=True))
    assert first["status"]["pages"] == 1 and list(first["out"].glob("*.md"))
    second = run(tmp_path, site({"status": 403, "body": "no"}))
    assert second["status"]["status"] == "blocked" and list(second["out"].glob("*.md")) == []


# ---------------------------------------------------------------- bad signals file
@pytest.mark.parametrize("content,needle", [
    (None, "not readable"),
    ("{broken", "not valid JSON"),
    ('{"topic_terms": ["x"]}', "missing key"),
])
def test_bad_signals_file_stops_before_any_request(tmp_path, content, needle):
    p = tmp_path / "signals.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    r = run(tmp_path, site(OK_HOME), signals=p)
    assert r["rc"] == 2
    assert "signals config error" in r["stderr"] and needle in r["stderr"]
    assert r["log"]["fetch"] == [] and r["log"]["stealth"] == [] and r["log"]["spider"] is None
    assert not r["out"].exists()                                  # nothing was written either
