"""Crawl a company's public pages into markdown (source pack) for Prompt 1.

The default mode is a larger crawl; --light is the small, time-capped mode the nightly
Prompt 1 run uses. Both write the same format, so a later research step can reuse the folder.

Usage:
  python scripts/source_pack.py example.com [max_pages=50] [seed_cap=40]
  python scripts/source_pack.py example.com --light            (P1: ~90 s cap)
  python scripts/source_pack.py example.com --light --out tmp/pack --timeout 60

Writes (default dir = <repo>/state/work/<domain>/sources/, independent of cwd):
  *.md         one file per page
  index.txt    url | title | topic-terms | words   (pages with topic terms first)
  jobs.json    Lever / Greenhouse / Ashby feed body if one was found,
               else {"jobs": [], "source": null}
  jobs.txt     compact listing of that feed (roles matching role_terms first)
  status.json  {"status": "ok" | "blocked" | "disallowed" | "unreachable" | "no_host", "reason", "pages", ...}

Which pages and postings matter is configuration, not code: config/signals.json
(see scripts/signals.py). A missing or malformed signals file stops the run with exit code 2.

Rules the crawler follows (settings under "crawler" in config/settings.json):
- robots.txt is read once per host and consulted before EVERY request: the homepage
  probe, sitemap files, every page of the crawl, job-board feeds and the stealth fetch.
  A robots.txt that answers 401 or 403, or cannot be read because of a server error,
  counts as "disallow everything".
- A site that refuses the first request (401/403/407/429/503 or a bot challenge) is
  recorded as blocked in status.json and skipped: no sitemap, no crawl, no job feed.
- stealth_fallback (default false): only when true is a refused page fetched again with
  a stealth browser (StealthyFetcher) that is built to pass bot challenges.
- impersonate_browser (default false): when false, requests send the plain user agent
  from crawler.user_agent, without browser-like headers or TLS fingerprint, and that
  same user agent is the one matched against robots.txt. When true, the fetch library's
  defaults apply (it presents itself as a browser) and robots.txt is matched as "*".

- While crawler.user_agent is still the shipped placeholder (and impersonate_browser is off),
  the crawler refuses to run: exit code 2, no request made, nothing written.

No LLM involved. Exit code 0 even when nothing was fetched (the caller decides
what an empty pack means); 2 on bad arguments, a bad signals file or a placeholder user agent.
Last stdout line: "<n> usable pages -> <dir>".
"""
import argparse, hashlib, html as _html, json, logging, os, re, socket, sys, threading, time
from pathlib import Path
from urllib.parse import urlparse

logging.disable(logging.INFO)   # Scrapling logs every page at DEBUG/INFO; keep the terminal readable

from protego import Protego
from scrapling.fetchers import Fetcher, FetcherSession
from scrapling.spiders import CrawlRule, LinkExtractor, SiteToMarkdownSpider

sys.path.insert(0, str(Path(__file__).resolve().parent))
import signals as sg  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Technical skips only (query strings, archives, files, account pages). Targeting lives in the signals file.
BASE_DENY = [r"\?", r"/tag/", r"/tags/", r"/category/", r"/page/\d+", r"/author/",
             r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|css|js)$", r"/login", r"/signin", r"/signup", r"/cart",
             r"/checkout", r"/wp-json/", r"/feed/?$", r"/rss", r"/search"]

BLOCK_STATUSES = (401, 403, 407, 429, 503)
BLOCK_MARKERS = (b"cf-chl", b"challenge-platform", b"Just a moment...", b"Attention Required! | Cloudflare",
                 b"cf-browser-verification", b"_Incapsula_Resource", b"px-captcha")

NOT_FOUND = re.compile(r"page not found|404|not found", re.I)
SOFT_404 = re.compile(r"page (?:isn't|is not|doesn't|does not|can't be|cannot be) (?:around|exist|found)|"
                      r"page not found|we can't find (?:that|this|the) page|nothing (?:was )?found here|error 404", re.I)

ATS_LINKS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I),
}
OTHER_ATS = re.compile(r"myworkdayjobs\.com|icims\.com|smartrecruiters\.com|workable\.com|bamboohr\.com|"
                       r"jobvite\.com|successfactors|taleo\.net|recruitee\.com|breezy\.hr|rippling-ats|"
                       r"teamtailor\.com|dayforcehcm\.com|ultipro\.com|paylocity\.com", re.I)
NOT_SLUGS = {"embed", "job_board", "js", "v1", "boards", "api", "jobs"}
TAG_RE = re.compile(r"<[^>]+>")

RESERVE = 12            # seconds kept back for job feeds + index when capped
MIN_WORDS = 60          # a page with fewer words is not listed
MAX_JOB_LINES = 150
PACK_FILES = ("index.txt", "jobs.json", "jobs.txt", "status.json")
DEFAULT_USER_AGENT = "account-sourcing-pipeline/0.1 (+contact: set crawler.user_agent in config/settings.json)"
# Parts of the shipped sample value and of the default above. A user agent that still contains one
# of them names nobody, so the crawler refuses to make a request under it.
PLACEHOLDER_MARKERS = ("set this to your own", "set crawler.user_agent")


# =========================================================================== settings
def load_crawler_settings(settings_path=None) -> dict:
    """The "crawler" block of config/settings.json. A missing or broken file gives the safe defaults:
    no stealth, no browser impersonation, the default user agent."""
    p = Path(settings_path) if settings_path else REPO / "config" / "settings.json"
    block = {}
    try:
        block = json.loads(p.read_text(encoding="utf-8")).get("crawler") or {}
    except (OSError, ValueError, AttributeError):
        block = {}
    if not isinstance(block, dict):
        block = {}
    ua = block.get("user_agent")
    return {"stealth_fallback": block.get("stealth_fallback") is True,
            "impersonate_browser": block.get("impersonate_browser") is True,
            "user_agent": ua.strip() if isinstance(ua, str) and ua.strip() else DEFAULT_USER_AGENT}


def identity_problem(crawler: dict):
    """A reason not to crawl, or None. The crawler must say who it is: while crawler.user_agent is
    still the shipped placeholder it makes no request. Not checked when impersonate_browser is on,
    because the user agent is not sent then."""
    if crawler["impersonate_browser"]:
        return None
    ua = crawler["user_agent"].lower()
    if any(m in ua for m in PLACEHOLDER_MARKERS):
        return ("crawler.user_agent in config/settings.json is still the shipped placeholder: "
                "set it to a name and a contact address of your own")
    return None


def fetch_options(crawler: dict) -> dict:
    """Keyword arguments for every request. With impersonation off: no browser fingerprint, no
    generated browser headers, and the plain user agent. With it on: the fetch library's defaults."""
    if crawler["impersonate_browser"]:
        return {}
    return {"impersonate": None, "stealthy_headers": False, "headers": {"User-Agent": crawler["user_agent"]}}


def robots_agent(crawler: dict) -> str:
    """The user agent matched against robots.txt: the real one, or "*" when the client poses as a browser."""
    return "*" if crawler["impersonate_browser"] else crawler["user_agent"]


# =========================================================================== robots.txt
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def robots_rules(status, body) -> str:
    """robots.txt text to apply for a fetch result. 200: the file. 401/403 or a server error: nothing is
    allowed. Any other answer (for example 404): no file, so everything is allowed."""
    if status == 200:
        return body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    if status in (401, 403) or status is None or status >= 500:
        return DISALLOW_ALL
    return ""


class Robots:
    """robots.txt per host, fetched once with the crawler's own fetch function and user agent."""

    def __init__(self, fetch, agent: str):
        self._fetch = fetch          # fetch(url) -> response with .status and .body, or raises
        self.agent = agent
        self._cache = {}

    def parser(self, url: str):
        p = urlparse(url)
        host = p.netloc
        if host not in self._cache:
            status, body = None, b""
            try:
                r = self._fetch(f"{p.scheme or 'https'}://{host}/robots.txt")
                status, body = r.status, (r.body or b"")
            except Exception:
                status = None       # unreachable: nothing is allowed
            self._cache[host] = Protego.parse(robots_rules(status, body))
        return self._cache[host]

    def allowed(self, url: str) -> bool:
        try:
            return bool(self.parser(url).can_fetch(url, self.agent))
        except Exception:
            return False


def install_spider_robots(agent: str) -> bool:
    """Make the spider match robots.txt against `agent` with the same rules as Robots (the library
    matches against "*" and treats an unreadable robots.txt as "allow"). Returns False, and changes
    nothing, when the library does not have the expected hook."""
    try:
        import scrapling.spiders.engine as engine
        base = getattr(engine.RobotsTxtManager, "_sourcing_base", engine.RobotsTxtManager)
    except Exception:
        return False

    class AgentRobots(base):
        _sourcing_base = base
        _sourcing_agent = agent

        async def _get_parser(self, url, sid):
            p = urlparse(url)
            host = p.netloc
            if host in self._cache:
                return self._cache[host]
            status, body = None, b""
            try:
                r = await self._fetch_fn(f"{p.scheme or 'https'}://{host}/robots.txt", sid)
                status, body = r.status, (r.body or b"")
            except Exception:
                status = None
            self._cache[host] = Protego.parse(robots_rules(status, body))
            return self._cache[host]

        async def can_fetch(self, url, sid):
            return (await self._get_parser(url, sid)).can_fetch(url, agent)

        async def get_delay_directives(self, url, sid):
            parser = await self._get_parser(url, sid)
            c_delay, rate = parser.crawl_delay(agent), parser.request_rate(agent)
            return (float(c_delay) if c_delay is not None else None,
                    (rate.requests, rate.seconds) if rate is not None else None)

    engine.RobotsTxtManager = AgentRobots
    return True


# =========================================================================== pure helpers
def clean_domain(arg: str):
    d = (arg or "").lower().strip().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
    return d if d and "." in d else None


def looks_blocked(resp) -> bool:
    if resp is None:
        return True
    if resp.status in BLOCK_STATUSES:
        return True
    return any(m in (resp.body or b"")[:20000] for m in BLOCK_MARKERS)


def block_reason(resp) -> str:
    if resp is None:
        return "no answer"
    if resp.status in BLOCK_STATUSES:
        return f"http {resp.status}"
    return "bot challenge"


def rank(url: str, sig) -> tuple:
    """Sort key for sitemap URLs: priority_paths first, then secondary_paths, then the rest; short paths first."""
    path = urlparse(url).path
    return (0 if sig.priority.search(path) else 1 if sig.secondary.search(path) else 2, len(path))


def seed_urls(domain: str, live_hosts, sitemap, sig, seed_cap: int) -> set:
    seed = {f"https://{domain}/", f"https://www.{domain}/"}
    seed |= {f"https://{s}.{domain}/" for s in sig.seed_subdomains if f"{s}.{domain}" in live_hosts}
    seed |= {f"https://{domain}/{p}" for p in sig.seed_paths}
    seed |= set(sorted(sitemap, key=lambda u: rank(u, sig))[:seed_cap])
    return seed


def sitemap_locs(body: bytes) -> list:
    return [loc.decode(errors="ignore") for loc in re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", body or b"")]


def page_name(url: str) -> str:
    u = urlparse(url)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", f"{u.netloc}{u.path}".strip("/")).strip("-")[:80] or "index"


def page_item(resp, url: str, suffix: str = "") -> dict:
    """A fetched page as the index needs it: markdown of the main content, title, file name."""
    md = resp.markdown(None, True) or ""
    title = str(resp.css("title::text").get() or "").strip()
    return {"url": url, "title": title, "markdown": md, "_file": f"{page_name(url)}{suffix}.md"}


def index_rows(items, topic_terms):
    """(rows, keep, drop). rows = (url, title, topic terms, words) for usable pages, pages with topic
    terms first and longer pages before shorter ones. Duplicates, error pages, soft 404s and pages
    under MIN_WORDS are dropped; keep/drop are the page files to keep and to delete."""
    seen, hashes, rows, keep, drop = set(), set(), [], set(), set()
    for item in items:
        url = item["url"].replace(":443/", "/")
        title = (item.get("title") or "").strip().replace("|", "/")[:90]
        md = item.get("markdown") or ""
        h = hashlib.sha1(md.strip().encode("utf-8", "ignore")).hexdigest()
        f = item.get("_file")
        if (url in seen or h in hashes or NOT_FOUND.search(title) or SOFT_404.search(md[:600])
                or len(md.split()) < MIN_WORDS):
            if f:
                drop.add(f)
            continue
        seen.add(url); hashes.add(h)
        if f:
            keep.add(f)
        terms = sorted({m.lower() for m in topic_terms.findall(md)})
        rows.append((url, title, ",".join(terms) or "-", len(md.split())))
    rows.sort(key=lambda t: (t[2] == "-", -t[3]))
    return rows, keep, drop


def build_index(out: Path, items, topic_terms) -> list:
    """index.txt lists usable pages only; .md files of rejected pages are deleted so nothing in sources/ is junk."""
    rows, keep, drop = index_rows(items, topic_terms)
    for f in drop - keep:
        try:
            (out / f).unlink()
        except OSError:
            pass
    (out / "index.txt").write_text("\n".join(" | ".join(map(str, t)) for t in rows), encoding="utf-8")
    return rows


def find_ats(corpus: str):
    """(slugs per job board found as links in the text, other applicant tracking systems seen)."""
    found = {k: [] for k in ATS_LINKS}
    for kind, rx in ATS_LINKS.items():
        for s in rx.findall(corpus):
            s = s.strip().lower()
            if s and s not in NOT_SLUGS and s not in found[kind]:
                found[kind].append(s)
    return found, sorted({m.group(0).lower() for m in OTHER_ATS.finditer(corpus)})


def slug_guesses(domain: str, other_ats) -> list:
    """Guessed slugs can hit an unrelated company's board. Only guess when the site showed no ATS at
    all; the result is labelled "guessed" so /qualify treats it as unverified."""
    base = domain.split(".")[0]
    return [] if other_ats else [base] + ([base.replace("-", "")] if "-" in base else [])


def feed_urls(found_slugs: dict, guesses) -> list:
    urls = []
    for s in found_slugs["greenhouse"]:
        urls.append(("site_link", f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true"))
    for s in found_slugs["lever"]:
        urls.append(("site_link", f"https://api.lever.co/v0/postings/{s}?mode=json"))
    for s in found_slugs["ashby"]:
        urls.append(("site_link", f"https://api.ashbyhq.com/posting-api/job-board/{s}"))
    for s in guesses:
        urls += [("guessed", f"https://api.lever.co/v0/postings/{s}?mode=json"),
                 ("guessed", f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true"),
                 ("guessed", f"https://api.ashbyhq.com/posting-api/job-board/{s}")]
    seen, res = set(), []
    for o, u in urls:
        if u not in seen:
            seen.add(u); res.append((o, u))
    return res


def job_list(body) -> list:
    try:
        data = json.loads(body)
    except Exception:
        return []
    if isinstance(data, list):                      # Lever
        return data
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):   # Greenhouse / Ashby
        return data["jobs"]
    return []


def _txt(x) -> str:
    return re.sub(r"\s+", " ", _html.unescape(TAG_RE.sub(" ", _html.unescape(str(x or ""))))).strip()


def jobs_text(jobs, src: str, origin: str, domain: str, sig):
    """(text of jobs.txt, postings that mention the company). One line per posting, roles matching
    role_terms first, then postings with topic terms, capped at MAX_JOB_LINES."""
    base = domain.split(".")[0]
    rows, mentions = [], 0
    name_rx = re.compile(re.escape(base) + r"|" + re.escape(domain), re.I)
    for j in jobs:
        if not isinstance(j, dict):
            continue
        title = _txt(j.get("title") or j.get("text"))
        loc = j.get("location") or (j.get("categories") or {}).get("location") or ""
        loc = _txt(loc.get("name") if isinstance(loc, dict) else loc)
        dept = _txt((j.get("categories") or {}).get("team") or j.get("department") or j.get("team")
                    or ", ".join(d.get("name", "") for d in (j.get("departments") or []) if isinstance(d, dict)))
        url = j.get("absolute_url") or j.get("hostedUrl") or j.get("jobUrl") or j.get("applyUrl") or ""
        body = _txt(j.get("content") or j.get("descriptionPlain") or j.get("description") or j.get("descriptionHtml"))
        if name_rx.search(body) or name_rx.search(url):
            mentions += 1
        terms = sorted({m.lower() for m in sig.topic_terms.findall(title + " " + body)})
        role = bool(sig.role_terms.search(title))
        rows.append((not role, not terms, title, loc, dept, url, ",".join(terms) or "-"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    head = [f"# source: {src}",
            f"# slug_origin: {origin}   (guessed = board found by guessing the slug from the domain; "
            f"it may belong to another company unless postings mention {domain})",
            f"# postings: {len(rows)}; postings mentioning '{base}' or {domain}: {mentions}",
            "# title | location | team | url | topic-terms in posting   (roles matching role_terms first)"]
    body_lines = [" | ".join(r[2:]) for r in rows[:MAX_JOB_LINES]]
    if len(rows) > MAX_JOB_LINES:
        body_lines.append(f"# ... {len(rows) - MAX_JOB_LINES} more postings in jobs.json")
    return "\n".join(head + body_lines), mentions


def resolves(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


# =========================================================================== the run
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Build a Scrapling source pack for one company.")
    ap.add_argument("domain")
    ap.add_argument("max_pages", nargs="?", type=int, default=None)
    ap.add_argument("seed_cap", nargs="?", type=int, default=None)
    ap.add_argument("--light", action="store_true", help="small, time-capped pack for Prompt 1")
    ap.add_argument("--out", help="write the pack into this directory instead of state/work/<domain>/sources")
    ap.add_argument("--timeout", type=float, default=None, help="wall-clock cap in seconds (light default 90)")
    ap.add_argument("--signals", default=None, help="signals file (default config/signals.json)")
    ap.add_argument("--settings", default=None, help="settings file (default config/settings.json)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    t0 = time.time()
    args = parse_args(argv)
    domain = clean_domain(args.domain)
    if not domain:
        print(f"bad domain: {args.domain!r}", file=sys.stderr)
        return 2

    light = args.light
    max_pages = args.max_pages or (15 if light else 50)
    seed_cap = args.seed_cap if args.seed_cap is not None else (12 if light else 40)
    timeout = args.timeout if args.timeout is not None else (90.0 if light else None)
    deadline = t0 + timeout if timeout else None
    req_timeout = 10 if light else 30

    try:
        sig = sg.build(sg.load_signals(args.signals), light, BASE_DENY)
    except sg.SignalsError as e:
        print(f"signals config error: {e}", file=sys.stderr)
        return 2
    crawler = load_crawler_settings(args.settings)
    problem = identity_problem(crawler)
    if problem:
        print(f"crawler config error: {problem}", file=sys.stderr)
        return 2
    opts = fetch_options(crawler)
    agent = robots_agent(crawler)

    out = Path(args.out) if args.out else REPO / "state" / "work" / domain / "sources"
    out.mkdir(parents=True, exist_ok=True)
    # A retry must not mix a previous crawl's pages with the new index: clear old pack files.
    for f in list(out.glob("*.md")) + [out / n for n in PACK_FILES]:
        try:
            f.unlink()
        except OSError:
            pass

    def left():
        """Seconds left before the wall-clock cap (large number when uncapped)."""
        return deadline - time.time() if deadline else 1e9

    def get(url, **kw):
        return Fetcher.get(url, **{**opts, **kw})

    robots = Robots(lambda u: get(u, timeout=6, retries=0), agent)
    skipped_by_robots = []

    def allowed(url) -> bool:
        ok = robots.allowed(url)
        if not ok:
            skipped_by_robots.append(url)
        return ok

    early_items = []        # pages fetched outside the spider (stealth fallback)
    crawled = []            # filled as pages arrive, so a timeout keeps what we have
    state = {"status": "ok", "reason": "", "stealth_used": False}

    def finish(rows) -> int:
        (out / "status.json").write_text(json.dumps({
            "domain": domain, "status": state["status"], "reason": state["reason"], "pages": len(rows),
            "stealth_used": state["stealth_used"], "skipped_by_robots": len(skipped_by_robots),
            "impersonate_browser": crawler["impersonate_browser"], "robots_agent": agent}, indent=1), encoding="utf-8")
        print(f"elapsed: {time.time()-t0:.0f}s")
        print("topic-term pages:", sum(1 for t in rows if t[2] != "-"))
        print(f"{len(rows)} usable pages -> {out}")
        return 0

    def stop(status, reason) -> int:
        """The site is not crawled: record why, write the empty pack files, make no further request."""
        state.update(status=status, reason=reason)
        print(f"site {status} ({reason}): recorded in status.json, not crawled")
        (out / "jobs.json").write_text(json.dumps({"jobs": [], "source": None}), encoding="utf-8")
        (out / "jobs.txt").write_text(f"# source: none (site {status}: {reason})\n", encoding="utf-8")
        return finish(build_index(out, [], sig.topic_terms))

    hosts = {domain} | {f"{s}.{domain}" for s in sig.probe_subdomains}
    live_hosts = {h for h in hosts if resolves(h)}
    print(f"live hosts: {sorted(live_hosts)}")

    # ---------------------------------------------------------------------- homepage probe
    probe_html = b""
    home, home_url, tried = None, None, False
    for h in (domain, f"www.{domain}"):
        if h not in live_hosts:
            continue
        url = f"https://{h}/"
        if not allowed(url):
            continue
        tried = True
        try:
            home = get(url, timeout=req_timeout, retries=0)
        except Exception:
            home = None
        home_url = url
        if home is not None and not looks_blocked(home):
            probe_html = home.body or b""
            break
    forced = os.environ.get("SOURCE_PACK_FORCE_STEALTH") == "1"   # test hook: treat the homepage as refused
    if live_hosts and not tried:
        return stop("disallowed", "robots.txt does not allow the homepage")
    refused = bool(live_hosts) and (home is None or looks_blocked(home) or forced)

    if refused:
        reason = "forced by test hook" if forced and home is not None and not looks_blocked(home) else block_reason(home)
        if not crawler["stealth_fallback"]:
            return stop("unreachable" if home is None else "blocked", reason)
        if left() > 30:
            print(f"homepage refused ({reason}) -> StealthyFetcher fallback (crawler.stealth_fallback is on)")
            budget = 30 if light else 90                    # seconds for the whole stealth fallback
            s_end = time.time() + min(budget, left() - 25)

            def _stealth(url, box):
                try:
                    from scrapling.fetchers import StealthyFetcher
                    box["r"] = StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True, timeout=20000)
                except Exception as e:
                    box["e"] = e

            for p in sig.stealth_paths:
                if time.time() > s_end - 5:
                    break
                url = f"https://www.{domain}/{p}" if f"www.{domain}" in live_hosts else f"https://{domain}/{p}"
                if not allowed(url):
                    print(f"  stealth fetch skipped, robots.txt disallows {url}")
                    continue
                state["stealth_used"] = True
                box = {}
                th = threading.Thread(target=_stealth, args=(url, box), daemon=True)
                th.start()
                th.join(max(1.0, s_end - time.time()))     # wall clock wins over a hung browser
                if th.is_alive():
                    print(f"  stealth fetch timed out {url}")
                    break
                if "e" in box:
                    print(f"  stealth fetch failed {url}: {type(box['e']).__name__}")
                    continue
                r = box.get("r")
                if r is not None and r.status == 200 and not looks_blocked(r):
                    item = page_item(r, url, "-stealth")
                    (out / item["_file"]).write_text(item["markdown"], encoding="utf-8")
                    early_items.append(item)
                    if not p:
                        probe_html = r.body or b""
            print(f"  stealth pages: {len(early_items)}")
        if not early_items:
            return stop("blocked", reason)

    # ---------------------------------------------------------------------- seeds
    def sitemap_urls():
        """Pull candidate URLs from the sitemaps of hosts that actually resolve. Fast-fail everything."""
        found = set()
        s0 = time.time()
        budget = 15 if light else 1e9
        nested_cap = 3 if light else 1000
        nested = 0
        cands = (domain, f"www.{domain}") + tuple(f"{s}.{domain}" for s in sig.sitemap_subdomains)
        for host in [h for h in cands if h in live_hosts]:
            paths = ("/hc/sitemap.xml",) if host.startswith("help.") else ("/sitemap.xml", "/sitemap_index.xml")
            for path in paths:
                if time.time() - s0 > budget or left() < 45:
                    break
                if not allowed(f"https://{host}{path}"):
                    continue
                try:
                    r = get(f"https://{host}{path}", timeout=6, retries=0)
                except Exception:
                    continue
                if r.status != 200 or b"<loc>" not in r.body:
                    continue
                for u in sitemap_locs(r.body):
                    if u.endswith(".xml"):          # nested sitemap
                        if nested >= nested_cap or time.time() - s0 > budget or not allowed(u):
                            continue
                        nested += 1
                        try:
                            r2 = get(u, timeout=6, retries=0)
                            for u2 in sitemap_locs(r2.body):
                                if sig.sitemap_pick.search(urlparse(u2).path) and not sig.deny_re.search(u2):
                                    found.add(u2)
                        except Exception:
                            pass
                    elif sig.sitemap_pick.search(urlparse(u).path) and not sig.deny_re.search(u):
                        found.add(u)
        print(f"sitemaps read in {time.time()-s0:.0f}s")
        return found

    sm = sitemap_urls() if live_hosts else set()
    print(f"sitemap candidates: {len(sm)}")
    # Seeds on hosts that do not resolve are dropped without a robots.txt request.
    seed = {u for u in seed_urls(domain, live_hosts, sm, sig, seed_cap)
            if urlparse(u).netloc in live_hosts and allowed(u)}

    # ---------------------------------------------------------------------- crawl
    page_files = {}         # url -> .md filename written by the spider
    session_options = dict(opts, timeout=req_timeout, retries=1 if light else 3)
    spider_robots = install_spider_robots(agent)
    if not spider_robots:
        print("WARNING: could not set the robots.txt user agent of the spider; it matches robots.txt as '*'")

    class SourcePack(SiteToMarkdownSpider):
        name = f"sources-{domain}"
        start_urls = sorted(seed)
        allowed_domains = live_hosts or {domain}
        output_dir = str(out)
        main_content_only = True
        robots_txt_obey = True
        concurrent_requests = 4
        download_delay = 0.4 if not light else 0.2

        max_depth = 2   # honored if this Scrapling version supports it; harmless otherwise

        def configure_sessions(self, manager):
            manager.add("default", FetcherSession(**session_options))

        def rules(self):
            return [CrawlRule(LinkExtractor(allow=sig.allow, deny=sig.deny))]

        def _filename_for(self, url):
            # Windows MAX_PATH: keep names short; hash suffix keeps long URLs unique.
            name = super()._filename_for(url)
            if len(name) > 90:
                name = f"{name[:80]}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
            page_files[url] = name + ".md"
            return name

        async def on_scraped_item(self, item):
            try:
                item = await super().on_scraped_item(item)
            except OSError as e:          # file write failed: keep the page in the index anyway
                print(f"write failed for {item.get('url')}: {type(e).__name__}")
            if item is not None:
                item["_file"] = page_files.get(item["url"])
                crawled.append(item)
            return item

    SourcePack.max_pages = max_pages

    def hard_stop():
        """Last resort if the spider ignores pause: write what we have and exit 0."""
        rows = build_index(out, early_items + list(crawled), sig.topic_terms)
        if not (out / "jobs.json").exists():
            (out / "jobs.json").write_text(json.dumps({"jobs": [], "source": None}), encoding="utf-8")
        print(f"HARD TIMEOUT after {time.time()-t0:.0f}s (crawl did not stop); jobs feeds skipped")
        state["reason"] = "hard timeout"
        finish(rows)
        sys.stdout.flush()
        os._exit(0)

    timers = []
    if live_hosts and seed and (not deadline or left() > RESERVE + 5):
        spider = SourcePack()
        if deadline:
            crawl_budget = max(5.0, left() - RESERVE)

            def _pause():
                try:
                    spider.pause()
                except Exception:
                    pass
            timers = [threading.Timer(crawl_budget, _pause),            # graceful: finish in-flight pages
                      threading.Timer(crawl_budget + 6, _pause),        # second call = force stop
                      threading.Timer(max(crawl_budget + 20, left() + 10), hard_stop)]
            for t in timers:
                t.daemon = True
                t.start()
        try:
            spider.start()
        except Exception as e:
            print(f"crawl error: {type(e).__name__}: {e}")
        for t in timers:
            t.cancel()
    else:
        print("crawl skipped (no live host, no allowed seed or no time left)")
    print(f"crawl done at {time.time()-t0:.0f}s, {len(crawled)} pages")
    if not live_hosts:
        state.update(status="no_host", reason="no host of the domain resolves")

    # ---------------------------------------------------------------------- job feeds
    # Lever / Greenhouse / Ashby. Slugs from ATS links on the site first, then guesses from the domain.
    corpus = probe_html.decode(errors="ignore") + "\n" + "\n".join(
        (i.get("markdown") or "") for i in early_items + crawled if re.search(r"career|job", i.get("url", ""), re.I)
        or re.search(r"greenhouse|lever\.co|ashbyhq", i.get("markdown") or "", re.I))
    found_slugs, other_ats = find_ats(corpus)
    jobs_src = None
    for origin, url in feed_urls(found_slugs, slug_guesses(domain, other_ats)):
        if deadline and left() < 3:
            print("job feeds: out of time")
            break
        if not allowed(url):
            print(f"job feed skipped, robots.txt disallows {url}")
            continue
        try:
            r = get(url, timeout=min(8 if light else 20, max(2, int(left()) - 1)), retries=0)
            jobs = job_list(r.body) if r.status == 200 else []
            if jobs:
                (out / "jobs.json").write_bytes(r.body)
                text, m = jobs_text(jobs, url, origin, domain, sig)
                (out / "jobs.txt").write_text(text, encoding="utf-8")
                jobs_src = url
                print(f"jobs feed ({origin}): {url}  postings={len(jobs)} name-mentions={m}")
                break
        except Exception:
            pass
    if jobs_src is None:
        (out / "jobs.json").write_text(json.dumps({"jobs": [], "source": None}), encoding="utf-8")
        note = f"other ATS seen on site: {', '.join(other_ats)}" if other_ats else "no ATS link seen on site"
        (out / "jobs.txt").write_text(f"# source: none (lever/greenhouse/ashby not found; {note})\n", encoding="utf-8")
        print(f"ats: none found (lever/greenhouse/ashby); {note}")

    return finish(build_index(out, early_items + crawled, sig.topic_terms))


if __name__ == "__main__":
    sys.exit(main())
