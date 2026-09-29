"""The crawler's functions, tested one by one with local files. No network: pages are HTML files in
tests/fixtures/pages, the job feed is tests/fixtures/job_feed.json, robots.txt texts are inline."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from scrapling.engines.toolbelt.custom import Response

import signals as sg
import source_pack as sp

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PAGES = FIXTURES / "pages"
D = "harrow-and-finch.example"


@pytest.fixture(scope="module")
def sig(repo):
    """The shipped sample signals, light mode."""
    return sg.build(sg.load_signals(repo / "config" / "signals.json"), True, sp.BASE_DENY)


def page(name, url=None, status=200):
    url = url or f"https://{D}/{name}"
    return Response(url, (PAGES / f"{name}.html").read_bytes(), status, "", {}, {}, {})


# ---------------------------------------------------------------- arguments
@pytest.mark.parametrize("arg,expected", [
    ("example.com", "example.com"), ("https://www.Example.com/about", "example.com"),
    ("HTTP://shop.example.com", "shop.example.com"), ("localhost", None), ("", None), ("   ", None),
])
def test_clean_domain(arg, expected):
    assert sp.clean_domain(arg) == expected


def test_bad_domain_exits_2_without_writing(tmp_path, capsys):
    assert sp.main(["not-a-domain", "--light", "--out", str(tmp_path / "out")]) == 2
    assert "bad domain" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------- refused pages
@pytest.mark.parametrize("status", sp.BLOCK_STATUSES)
def test_refusal_statuses(status):
    r = page("stores", status=status)
    assert sp.looks_blocked(r) and sp.block_reason(r) == f"http {status}"


def test_challenge_page_and_no_answer():
    r = page("challenge")
    assert sp.looks_blocked(r) and sp.block_reason(r) == "bot challenge"
    assert sp.looks_blocked(None) and sp.block_reason(None) == "no answer"
    assert not sp.looks_blocked(page("stores"))
    assert not sp.looks_blocked(page("not_found", status=404))       # a missing page is not a refusal


# ---------------------------------------------------------------- page ranking
def test_rank_orders_priority_then_secondary_then_rest(sig):
    urls = [f"https://{D}/gift-cards", f"https://{D}/about/history/founders", f"https://{D}/news",
            f"https://{D}/stores/halifax-north", f"https://{D}/stores", f"https://{D}/suppliers"]
    ranked = sorted(urls, key=lambda u: sp.rank(u, sig))
    assert ranked == [f"https://{D}/stores", f"https://{D}/suppliers", f"https://{D}/stores/halifax-north",
                      f"https://{D}/news", f"https://{D}/about/history/founders", f"https://{D}/gift-cards"]
    assert sp.rank(f"https://{D}/stores", sig)[0] == 0
    assert sp.rank(f"https://{D}/careers", sig)[0] == 1
    assert sp.rank(f"https://{D}/gift-cards", sig)[0] == 2


def test_rank_matches_whole_path_words_only(sig):
    assert sp.rank(f"https://{D}/our-stores/", sig)[0] == 0
    assert sp.rank(f"https://{D}/restores", sig)[0] == 2
    assert sp.rank(f"https://stores.{D}/x", sig)[0] == 2              # the path counts, not the host


def test_seed_urls_apply_the_cap_to_sitemap_urls_only(sig):
    sitemap = {f"https://{D}/news/a", f"https://{D}/stores/a", f"https://{D}/gift-cards"}
    seeds = sp.seed_urls(D, {D, f"careers.{D}"}, sitemap, sig, seed_cap=1)
    assert f"https://{D}/stores/a" in seeds and f"https://{D}/news/a" not in seeds
    assert {f"https://{D}/", f"https://www.{D}/", f"https://careers.{D}/"} <= seeds
    assert f"https://news.{D}/" not in seeds                           # that subdomain does not resolve
    assert {f"https://{D}/{p}" for p in sig.seed_paths} <= seeds


def test_sitemap_locs():
    body = b"<urlset><url><loc> https://a.example/x </loc></url><url><loc>https://a.example/y.xml</loc></url></urlset>"
    assert sp.sitemap_locs(body) == ["https://a.example/x", "https://a.example/y.xml"]
    assert sp.sitemap_locs(b"") == [] and sp.sitemap_locs(None) == []


# ---------------------------------------------------------------- pages and the index
def test_page_item_from_html():
    item = sp.page_item(page("stores"), f"https://{D}/stores")
    assert item["title"] == "Our stores | Harrow & Finch Grocers"
    assert item["_file"] == f"{D}-stores.md"
    assert "62 supermarkets" in item["markdown"] and "<p>" not in item["markdown"]
    assert sp.page_item(page("stores"), f"https://{D}/", "-stealth")["_file"] == f"{D}-stealth.md"


def items():
    return [sp.page_item(page(n), f"https://{D}/{n}") for n in ("careers", "stores", "not_found", "short")]


def test_index_rows_keep_usable_pages_and_order_them(sig):
    rows, keep, drop = sp.index_rows(items(), sig.topic_terms)
    assert [r[0] for r in rows] == [f"https://{D}/stores", f"https://{D}/careers"]     # topic terms first
    url, title, terms, words = rows[0]
    assert title == "Our stores / Harrow & Finch Grocers"            # "|" would break the columns
    assert terms == "distribution centre,food waste,fresh departments,fresh produce"
    assert words > sp.MIN_WORDS
    assert rows[1][2] == "-"
    assert keep == {f"{D}-stores.md", f"{D}-careers.md"}
    assert drop == {f"{D}-not_found.md", f"{D}-short.md"}


def test_index_rows_drop_duplicates_and_soft_404s(sig):
    a = sp.page_item(page("stores"), f"https://{D}/stores")
    same_url = dict(a)
    same_text = dict(a, url=f"https://{D}/locations", _file="copy.md")
    soft = dict(a, url=f"https://{D}/old", title="Stores", _file="soft.md",
                markdown="Sorry, this page does not exist. " + a["markdown"])
    port = dict(sp.page_item(page("careers"), f"https://{D}:443/careers"))
    rows, keep, drop = sp.index_rows([a, same_url, same_text, soft, port], sig.topic_terms)
    assert [r[0] for r in rows] == [f"https://{D}/stores", f"https://{D}/careers"]      # :443 is normalised
    assert "copy.md" in drop and "soft.md" in drop and f"{D}-stores.md" in keep


def test_longer_page_first_among_equals(sig):
    a = {"url": "https://a.example/1", "title": "A", "markdown": "word " * 70, "_file": "1.md"}
    b = {"url": "https://a.example/2", "title": "B", "markdown": "other " * 90, "_file": "2.md"}
    rows, _, _ = sp.index_rows([a, b], sig.topic_terms)
    assert [r[0] for r in rows] == ["https://a.example/2", "https://a.example/1"]


def test_build_index_writes_the_file_and_deletes_rejected_pages(tmp_path, sig):
    its = items()
    for it in its:
        (tmp_path / it["_file"]).write_text(it["markdown"], encoding="utf-8")
    rows = sp.build_index(tmp_path, its, sig.topic_terms)
    lines = (tmp_path / "index.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows) == 2
    assert lines[0].split(" | ")[0] == f"https://{D}/stores" and len(lines[0].split(" | ")) == 4
    assert sorted(p.name for p in tmp_path.glob("*.md")) == [f"{D}-careers.md", f"{D}-stores.md"]
    assert sp.build_index(tmp_path, [], sig.topic_terms) == []
    assert (tmp_path / "index.txt").read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------- job boards
def test_job_board_link_found_on_the_careers_page():
    found, other = sp.find_ats(sp.page_item(page("careers"), f"https://{D}/careers")["markdown"])
    assert found == {"greenhouse": [], "lever": ["harrowfinch"], "ashby": []} and other == []


def test_job_board_slugs_and_other_systems():
    # the host names are those of real job-board services: the code has to recognise them. The slugs are made up.
    text = ("boards.greenhouse.io/embed/job_board?for=grocerone job-boards.eu.greenhouse.io/Grocer-Two "
            "jobs.lever.co/grocerone jobs.lever.co/grocerone jobs.ashbyhq.com/grocer.three "
            "https://grocerone.myworkdayjobs.com/x")
    found, other = sp.find_ats(text)
    assert found == {"greenhouse": ["grocerone", "grocer-two"], "lever": ["grocerone"], "ashby": ["grocer.three"]}
    assert other == ["myworkdayjobs.com"]


def test_slug_is_guessed_only_when_the_site_shows_no_job_system():
    assert sp.slug_guesses("harrow-and-finch.example", []) == ["harrow-and-finch", "harrowandfinch"]
    assert sp.slug_guesses("grocer.example", []) == ["grocer"]
    assert sp.slug_guesses("grocer.example", ["icims.com"]) == []


def test_feed_urls_site_links_first_then_guesses_without_repeats():
    found = {"greenhouse": [], "lever": ["grocer"], "ashby": []}
    urls = sp.feed_urls(found, ["grocer"])
    assert urls[0] == ("site_link", "https://api.lever.co/v0/postings/grocer?mode=json")
    assert [o for o, _ in urls] == ["site_link", "guessed", "guessed"]
    assert len({u for _, u in urls}) == 3


@pytest.mark.parametrize("body,count", [
    ('[{"text": "a"}, {"text": "b"}]', 2),                  # Lever: a list
    ('{"jobs": [{"title": "a"}]}', 1),                      # Greenhouse, Ashby: an object
    ('{"jobs": "none"}', 0), ('{"other": []}', 0), ("not json", 0), ("", 0), ("3", 0),
])
def test_job_list_shapes(body, count):
    assert len(sp.job_list(body)) == count


def test_jobs_text_from_the_feed_fixture(sig):
    jobs = sp.job_list((FIXTURES / "job_feed.json").read_text(encoding="utf-8"))
    text, mentions = sp.jobs_text(jobs, "https://feed.example/x", "site_link", "harrowfinch.example", sig)
    head = [l for l in text.splitlines() if l.startswith("#")]
    body = [l.split(" | ") for l in text.splitlines() if not l.startswith("#")]
    assert head[0] == "# source: https://feed.example/x" and "slug_origin: site_link" in head[1]
    assert head[2] == "# postings: 4; postings mentioning 'harrowfinch' or harrowfinch.example: 4"
    assert head[3] == "# title | location | team | url | topic-terms in posting   (roles matching role_terms first)"
    # roles matching role_terms first, and among them the postings with topic terms; then the other
    # postings with topic terms; then the rest
    assert [b[0] for b in body] == ["Replenishment Analyst", "Category Manager, Produce", "Baker", "Cashier"]
    assert body[1][-1] == "-"
    assert body[0] == ["Replenishment Analyst", "Head office", "Supply Chain", "https://jobs.example/harrowfinch/2",
                       "demand forecasting,perishables,replenishment"]
    assert body[2][-1] == "food waste"                       # found inside HTML, tags removed
    assert body[3][-1] == "-" and mentions == 4


def test_jobs_text_is_capped_and_ignores_junk_entries(sig):
    jobs = [{"text": f"Clerk {i:03d}"} for i in range(sp.MAX_JOB_LINES + 5)] + ["junk", None, 7]
    text, mentions = sp.jobs_text(jobs, "src", "guessed", "grocer.example", sig)
    lines = text.splitlines()
    assert lines[2].startswith(f"# postings: {sp.MAX_JOB_LINES + 5};") and mentions == 0
    assert lines[-1] == "# ... 5 more postings in jobs.json"
    assert len([l for l in lines if not l.startswith("#")]) == sp.MAX_JOB_LINES


# ---------------------------------------------------------------- client identity
def test_shipped_settings_switch_everything_off(repo):
    c = sp.load_crawler_settings(repo / "config" / "settings.json")
    assert c == {"stealth_fallback": False, "impersonate_browser": False,
                 "user_agent": "account-sourcing-pipeline/0.1 (+contact: set this to your own address)"}


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"crawler": "x"}', '{"crawler": {}}', "{}",
                                     '{"crawler": {"stealth_fallback": "true", "impersonate_browser": 1, "user_agent": " "}}'])
def test_missing_or_broken_settings_give_the_safe_defaults(tmp_path, content):
    p = tmp_path / "settings.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    assert sp.load_crawler_settings(p) == {"stealth_fallback": False, "impersonate_browser": False,
                                           "user_agent": sp.DEFAULT_USER_AGENT}


def test_only_a_json_true_switches_something_on(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"crawler": {"stealth_fallback": true, "impersonate_browser": true, "user_agent": " bot/1 "}}',
                 encoding="utf-8")
    assert sp.load_crawler_settings(p) == {"stealth_fallback": True, "impersonate_browser": True, "user_agent": "bot/1"}


@pytest.mark.parametrize("ua,refused", [
    (sp.DEFAULT_USER_AGENT, True),
    ("account-sourcing-pipeline/0.1 (+contact: set this to your own address)", True),
    ("Anything (+contact: Set This To Your Own address)", True),
    ("example-crawler/1 (+contact: ops at the owner's address)", False),
    ("bot/1", False),
])
def test_placeholder_user_agent_is_a_reason_not_to_crawl(ua, refused):
    plain = {"impersonate_browser": False, "user_agent": ua, "stealth_fallback": False}
    problem = sp.identity_problem(plain)
    assert (problem is not None) is refused
    if refused:
        assert "crawler.user_agent" in problem and "placeholder" in problem
    assert sp.identity_problem(dict(plain, impersonate_browser=True)) is None     # the user agent is not sent then


def test_shipped_user_agent_is_a_placeholder(repo):
    assert sp.identity_problem(sp.load_crawler_settings(repo / "config" / "settings.json")) is not None
    assert sp.identity_problem(sp.load_crawler_settings(repo / "missing.json")) is not None


def test_fetch_options_and_robots_agent_in_both_modes():
    plain = {"impersonate_browser": False, "user_agent": "bot/1 (+me)", "stealth_fallback": False}
    assert sp.fetch_options(plain) == {"impersonate": None, "stealthy_headers": False,
                                       "headers": {"User-Agent": "bot/1 (+me)"}}
    assert sp.robots_agent(plain) == "bot/1 (+me)"
    browser = dict(plain, impersonate_browser=True)
    assert sp.fetch_options(browser) == {}                   # the library's defaults apply
    assert sp.robots_agent(browser) == "*"


# ---------------------------------------------------------------- robots.txt
ROBOTS = "User-agent: *\nDisallow: /private\n\nUser-agent: bot\nDisallow: /stores\nCrawl-delay: 7\n"


def fetcher(answers, calls):
    def fetch(url):
        calls.append(url)
        a = answers.get(url)
        if a is None:
            raise ConnectionError(url)
        return SimpleNamespace(status=a[0], body=a[1].encode("utf-8"))
    return fetch


def test_robots_is_matched_against_the_given_agent():
    calls = []
    answers = {"https://a.example/robots.txt": (200, ROBOTS)}
    ours = sp.Robots(fetcher(answers, calls), "bot/1 (+me)")
    assert not ours.allowed("https://a.example/stores/1") and ours.allowed("https://a.example/private/x")
    anyone = sp.Robots(fetcher(answers, []), "*")
    assert anyone.allowed("https://a.example/stores/1") and not anyone.allowed("https://a.example/private/x")
    assert calls == ["https://a.example/robots.txt"]         # read once per host


@pytest.mark.parametrize("answer,allowed", [
    ((200, ""), True), ((404, "nothing here"), True), ((410, ""), True),
    ((401, ""), False), ((403, "denied"), False), ((500, ""), False), ((503, ""), False),
    (None, False),                                            # robots.txt unreachable
    ((200, "User-agent: *\nDisallow: /\n"), False),
])
def test_robots_answers(answer, allowed):
    answers = {} if answer is None else {"https://a.example/robots.txt": answer}
    r = sp.Robots(fetcher(answers, []), "bot/1")
    assert r.allowed("https://a.example/") is allowed and r.allowed("https://a.example/about") is allowed


def test_robots_is_per_host():
    calls = []
    answers = {"https://a.example/robots.txt": (200, "User-agent: *\nDisallow: /\n"),
               "http://b.example/robots.txt": (404, "")}
    r = sp.Robots(fetcher(answers, calls), "bot/1")
    assert not r.allowed("https://a.example/x") and r.allowed("http://b.example/x")
    assert calls == ["https://a.example/robots.txt", "http://b.example/robots.txt"]


def test_spider_robots_use_the_agent_and_the_same_rules():
    import scrapling.spiders.engine as engine
    original = getattr(engine.RobotsTxtManager, "_sourcing_base", engine.RobotsTxtManager)
    try:
        assert sp.install_spider_robots("bot/1 (+me)") is True
        cls = engine.RobotsTxtManager
        assert cls._sourcing_agent == "bot/1 (+me)" and issubclass(cls, original)
        seen = []

        async def fetch(url, sid):
            seen.append(url)
            if "refused" in url:
                return SimpleNamespace(status=403, body=b"", encoding="utf-8")
            return SimpleNamespace(status=200, body=ROBOTS.encode(), encoding="utf-8")

        mgr = cls(fetch)
        assert asyncio.run(mgr.can_fetch("https://a.example/stores/1", "s")) is False     # our agent's rule
        assert asyncio.run(mgr.can_fetch("https://a.example/private/x", "s")) is True
        assert asyncio.run(mgr.can_fetch("https://refused.example/", "s")) is False       # 403 on robots.txt
        assert asyncio.run(mgr.get_delay_directives("https://a.example/", "s")) == (7.0, None)
        assert seen == ["https://a.example/robots.txt", "https://refused.example/robots.txt"]
        sp.install_spider_robots("*")                                                   # installing twice does not stack
        assert engine.RobotsTxtManager._sourcing_base is original
        assert asyncio.run(engine.RobotsTxtManager(fetch).can_fetch("https://a.example/stores/1", "s")) is True
    finally:
        engine.RobotsTxtManager = original
