"""Run scripts/source_pack.py with every network call replaced by a scenario file. Nothing leaves the machine.

  python tests/stubs/run_source_pack_offline.py <scenario.json> <source_pack arguments...>

Scenario keys (all optional):
  live_hosts   host names that "resolve"; every other lookup fails
  responses    {url: {"status": 200, "body": "text"}} for the plain fetcher; any other URL raises.
               A robots.txt URL that is not listed answers 404 (no robots.txt), unless
               "robots_default" says otherwise
  robots_default  status for robots.txt URLs that are not listed (default 404); "raise" = unreachable
  stealth      {url: {"status": 200, "title": "...", "body": "<html>...</html>"}} for the stealth fetcher
  log          path; receives {"fetch": [{"url", "kwargs"}], "stealth": [urls], "spider": {...} | null,
               "allow": [...], "deny": [...], "session": {...} | null, "spider_robots_agent": "...", "exit": code}

The spider itself is not run (it needs the network): its start() is replaced, and the seeds, link
rules and session options it was given are written to the log instead.
"""
import json
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
log = {"fetch": [], "stealth": [], "spider": None, "allow": None, "deny": None, "session": None,
       "spider_robots_agent": None, "exit": 0}


def response(url, spec):
    from scrapling.engines.toolbelt.custom import Response
    return Response(url, (spec.get("body") or "").encode("utf-8"), spec.get("status", 200), "", {}, {}, {})


def fake_resolve(host):
    if host in (scenario.get("live_hosts") or []):
        return "192.0.2.1"            # TEST-NET-1, never routed
    raise OSError("offline harness: host does not resolve")


def fake_get(url, **kw):
    log["fetch"].append({"url": url, "kwargs": {k: kw[k] for k in sorted(kw) if k in (
        "impersonate", "stealthy_headers", "headers")}})
    spec = (scenario.get("responses") or {}).get(url)
    if spec is None and url.endswith("/robots.txt"):
        default = scenario.get("robots_default", 404)
        if default == "raise":
            raise ConnectionError("offline harness: robots.txt unreachable")
        spec = {"status": default, "body": ""}
    if spec is None:
        raise ConnectionError("offline harness: no response for " + url)
    return response(url, spec)


def fake_stealth(url, **kw):
    log["stealth"].append(url)
    spec = (scenario.get("stealth") or {}).get(url)
    if spec is None:
        raise ConnectionError("offline harness: no stealth response for " + url)
    return response(url, spec)


def fake_start(self, *a, **kw):
    log["spider"] = {"start_urls": sorted(self.start_urls), "max_pages": self.max_pages,
                     "robots_txt_obey": self.robots_txt_obey,
                     "allowed_domains": sorted(self.allowed_domains)}

    class Manager:
        def add(self, name, session):
            log["session"] = {"impersonate": session._default_impersonate, "stealthy_headers": session._stealth,
                              "headers": dict(session._default_headers)}
    self.configure_sessions(Manager())
    self.rules()
    return None


socket.gethostbyname = fake_resolve
import scrapling.fetchers as fetchers  # noqa: E402
import source_pack  # noqa: E402

real_extractor = source_pack.LinkExtractor


def recording_extractor(*a, **kw):
    log["allow"], log["deny"] = list(kw.get("allow") or []), list(kw.get("deny") or [])
    return real_extractor(*a, **kw)


fetchers.Fetcher.get = staticmethod(fake_get)
fetchers.StealthyFetcher.fetch = staticmethod(fake_stealth)
source_pack.SiteToMarkdownSpider.start = fake_start
source_pack.LinkExtractor = recording_extractor

try:
    log["exit"] = source_pack.main(sys.argv[2:])
except SystemExit as e:
    log["exit"] = e.code if isinstance(e.code, int) else 1
finally:
    try:
        import scrapling.spiders.engine as engine
        log["spider_robots_agent"] = getattr(engine.RobotsTxtManager, "_sourcing_agent", None)
    except Exception:
        pass
    if scenario.get("log"):
        Path(scenario["log"]).write_text(json.dumps(log, indent=1), encoding="utf-8")
sys.exit(log["exit"])
