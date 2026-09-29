"""Nightly driver: Apollo pull (/source) -> Prompt 1 per company (/qualify) -> HubSpot.

Deterministic control flow lives here; each LLM step is a fresh `claude -p` call.

  python scripts/run_nightly.py [--cap 50] [--date YYYY-MM-DD] [--model opus]
                                [--dry-run] [--resume] [--root DIR]

Order of work for one run:
  1. backlog: pending / retryable error / hubspot_error lines from EARLIER queue
     files (companies already registered in Apollo must never be lost).
  2. today's queue: if state/queue/<date>.jsonl is missing (and not --resume,
     not --dry-run) run `/source <date> <cap - backlog>`; the skill writes the file.
  3. per company, one at a time: source pack (Scrapling, no LLM) ->
     `/qualify <domain> "<name>"` -> parse final-line JSON -> HubSpot upsert if
     QUALIFIED -> queue line rewritten atomically after every company.
  4. `/register-results <date>` once per touched queue date (skipped on dry-run
     and after a usage-limit stop).
  5. runs/<date>.md + runs/<date>.json (dry-run: runs/<date>-dry-run.*).

--dry-run: no Apollo pull, no HubSpot create/update (read-only lookup only),
no /register-results, and queue files are NOT modified. Prompt 1 still runs.
--resume: never call /source; only work through existing queue lines.

Test seams: env SOURCING_CLAUDE_BIN (a .py stub is run with this python),
SOURCING_SOURCE_PACK_CMD (template with {domain} and {python}), SOURCING_ROOT / --root;
module functions run_claude / run_source_pack can be monkeypatched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hubspot_writer as hw  # noqa: E402
import enrich  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SETTINGS = {
    "per_run_cap": 50,
    "model": "opus",
    "max_turns_source": 40,
    "max_turns_qualify": 30,
    "timeout_source_s": 900,
    "timeout_qualify_s": 900,
    "timeout_source_pack_s": 150,
    "timeout_register_s": 900,
    "run_deadline_min": 330,
    "max_attempts": 2,
    "max_consecutive_errors": 5,
    "max_consecutive_transient": 3,
    "hubspot_write": True,
    "register_results": True,
    "claude_auth": "subscription",
}

# How the `claude` CLI is authenticated (settings key claude_auth):
#   subscription  the CLI login of the person running the tool. API credentials are removed from
#                 every child process and the run refuses to start when an apiKeyHelper is configured,
#                 so a run cannot create API charges nobody planned for.
#   api_key       metered API billing. API credentials are passed through and nothing is refused.
AUTH_MODES = ("subscription", "api_key")
API_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# The HubSpot token stays inside this process in every mode.
ALWAYS_STRIPPED_ENV = ("HUBSPOT_TOKEN",)
_auth_mode = "subscription"   # set by main() from the settings


def auth_mode(settings: dict) -> str:
    mode = settings.get("claude_auth", "subscription")
    if mode not in AUTH_MODES:
        raise ValueError(f"claude_auth must be one of {list(AUTH_MODES)}, not {mode!r}")
    return mode


# A usage limit is recognised only in a message of the CLI itself: the phrase must START a line.
# The same words inside a sentence (a session quoting a website's or a connector's error) do not count.
USAGE_LIMIT_RE = re.compile(
    r"^[\s\W]{0,3}(?:error:\s*)?(?:"
    r"claude(?: ai)? usage limit reached"
    r"|usage limit reached"
    r"|api error:? (?:429\b|.{0,40}?rate limit)"
    r"|you(?:'|\u2019)?(?:ve| have) (?:hit|reached) your (?:usage )?limit"
    r"|you(?:'|\u2019)?re out of extra usage"
    r"|(?:\d+-hour|weekly|daily|session|opus|sonnet) limit reached"
    r"|your (?:usage )?limit will reset"
    r")", re.I | re.M)
TRANSIENT_RE = re.compile(r"overloaded|api error: 5\d\d|internal server error|"
                          r"connection (error|reset)|ECONNRESET|ETIMEDOUT|socket hang up", re.I)

RETRYABLE = {"pending", "error", "hubspot_error"}


class VerdictError(ValueError):
    pass


# ============================================================ small helpers
def today_iso() -> str:
    """Real local date: the P1 date is the day a company was qualified, never a batch id."""
    return dt.date.today().isoformat()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def resolve_root(arg_root: str | None) -> Path:
    return Path(arg_root or os.environ.get("SOURCING_ROOT") or REPO_ROOT).resolve()


def load_settings(root: Path) -> dict:
    s = dict(DEFAULT_SETTINGS)
    p = root / "config" / "settings.json"
    if p.exists():
        try:
            s.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # a broken settings file must not kill the run silently
            log(f"WARNING: could not read {p}: {e}; using defaults")
    return s


def stripped_env(mode: str | None = None) -> tuple:
    """Names removed from the environment of every child process in the given auth mode."""
    mode = mode or _auth_mode
    return ALWAYS_STRIPPED_ENV + (API_CREDENTIAL_ENV if mode == "subscription" else ())


def child_env(mode: str | None = None) -> dict:
    drop = stripped_env(mode)
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def api_key_guard(root: Path, mode: str = "subscription") -> str | None:
    """Billing guard for subscription mode: return a reason string if a run could create API charges
    instead of using the CLI login. In api_key mode nothing is refused."""
    if mode == "api_key":
        if not any(os.environ.get(k) for k in API_CREDENTIAL_ENV):
            log("NOTE: claude_auth is api_key but no API credential is set in the environment; "
                "the CLI will use whatever it is configured with")
        return None
    for k in API_CREDENTIAL_ENV:
        if os.environ.get(k):
            log(f"WARNING: {k} is set in the environment; it is stripped from every claude child")
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates = [home / ".claude" / "settings.json", home / ".claude" / "settings.local.json",
                  root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    for c in candidates:
        try:
            if c.exists() and "apiKeyHelper" in c.read_text(encoding="utf-8", errors="replace"):
                return f"apiKeyHelper configured in {c}"
        except OSError:
            pass
    return None


# ============================================================ queue I/O
def read_queue(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            log(f"WARNING: {path.name}:{i} is not valid JSON; ignored")
    return rows


def write_queue(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def is_retryable(row: dict, max_attempts: int) -> bool:
    st = row.get("status", "pending")
    if st == "pending":
        return True
    if st == "hubspot_error":
        return int(row.get("hubspot_attempts", 0)) < max_attempts
    if st == "error":
        return int(row.get("attempts", 0)) < max_attempts
    return False


# ============================================================ subprocess
def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _run(cmd: list[str], timeout: float, cwd: Path) -> dict:
    kwargs = dict(cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", env=child_env())
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        return {"rc": None, "stdout": "", "stderr": str(e), "timed_out": False,
                "duration_s": 0.0, "launch_error": True}
    try:
        out, err = proc.communicate(timeout=max(1.0, timeout))
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
        timed_out = True
    return {"rc": proc.returncode, "stdout": out or "", "stderr": err or "",
            "timed_out": timed_out, "duration_s": round(time.monotonic() - t0, 1)}


def claude_cmd() -> list[str]:
    b = os.environ.get("SOURCING_CLAUDE_BIN", "claude")
    if b.lower().endswith(".py"):
        return [sys.executable, b]
    return [b]


def run_claude(args: list[str], timeout: float, cwd: Path) -> dict:
    """Run `claude <args>`; adds 'json' (parsed stdout object or None)."""
    res = _run(claude_cmd() + args, timeout, cwd)
    res["json"] = None
    s = res["stdout"].strip()
    if s:
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            obj = None
            for line in reversed(s.splitlines()):  # tolerate noise before the JSON
                line = line.strip()
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
        if isinstance(obj, dict):
            res["json"] = obj
    return res


def run_source_pack(domain: str, timeout: float, cwd: Path) -> dict:
    tmpl = os.environ.get("SOURCING_SOURCE_PACK_CMD")
    if tmpl:
        cmd = shlex.split(tmpl.format(domain=domain, python=sys.executable.replace("\\", "/")))
    else:
        cmd = [sys.executable, str(cwd / "scripts" / "source_pack.py"), domain, "--light"]
    return _run(cmd, timeout, cwd)


def crawler_problem(root: Path) -> str | None:
    """Why the crawler would refuse to run with the current settings (placeholder user agent), or None."""
    try:
        import source_pack
    except Exception:          # the crawl library is not installed: the crawler itself will report it
        return None
    return source_pack.identity_problem(source_pack.load_crawler_settings(root / "config" / "settings.json"))


def crawl_status(work_dir: Path) -> str | None:
    """'blocked (http 403)' and the like when the crawler recorded that it did not crawl the site."""
    try:
        st = json.loads((work_dir / "sources" / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or st.get("status") in (None, "ok"):
        return None
    return f"{st['status']} ({st.get('reason') or 'no reason given'})"


def claude_text(res: dict) -> str:
    """All human-readable text of a claude run (for limit / error matching)."""
    j = res.get("json") or {}
    parts = [str(j.get("result") or ""), str(j.get("error") or ""), str(j.get("subtype") or ""),
             res.get("stderr", "")]
    if not j:
        parts.append(res.get("stdout", ""))
    return "\n".join(p for p in parts if p)


def is_usage_limit(returncode, output_text: str, api_error_status=None) -> bool:
    """True when Claude refused because the usage window / rate limit is exhausted."""
    if api_error_status in (429, "429"):
        return True
    if returncode == 0 and not output_text:
        return False
    return bool(USAGE_LIMIT_RE.search(_cli_message(output_text or "")))


def _cli_message(text: str) -> str:
    """The CLI's own message: the result and error fields when the text is the CLI's JSON object."""
    s = text.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return text
        if isinstance(obj, dict):
            return "\n".join(str(obj.get(k) or "") for k in ("result", "error"))
    return text


def is_transient(returncode, output_text: str, api_error_status=None) -> bool:
    try:
        code = int(api_error_status) if api_error_status is not None else None
    except (TypeError, ValueError):
        code = None
    if code is not None and code >= 500:
        return True
    return bool(TRANSIENT_RE.search(output_text or ""))


def classify_claude(res: dict) -> str:
    """ok | usage_limit | transient | timeout | error"""
    if res.get("timed_out"):
        return "timeout"
    j = res.get("json") or {}
    failed = res.get("rc") not in (0,) or bool(j.get("is_error")) or not j
    text = claude_text(res)
    status = j.get("api_error_status")
    if failed and is_usage_limit(res.get("rc"), text, status):
        return "usage_limit"
    if failed and is_transient(res.get("rc"), text, status):
        return "transient"
    return "error" if failed else "ok"


# ============================================================ verdict parsing
def _last_json_line(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    for line in reversed(text.splitlines()):
        line = line.strip().strip("`").strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise VerdictError("no JSON object line in result")


def parse_verdict(result_text: str) -> dict:
    """Final-line JSON of /qualify -> verdict dict.

    Returns error-shaped verdicts ({"status":"error",...}) as-is; raises
    VerdictError for anything malformed (no JSON, bad enum, missing keys).
    """
    v = _last_json_line(result_text)
    if v.get("status") == "error":
        return v
    if v.get("status") != "ok":
        raise VerdictError(f"verdict status {v.get('status')!r}")
    try:
        hw.validate_verdict(v)
    except hw.MappingError as e:
        raise VerdictError(str(e)) from e
    return v


def strip_verdict_line(text: str) -> str:
    """Prompt 1 prose = result minus the trailing JSON verdict line."""
    lines = (text or "").rstrip().splitlines()
    if lines and lines[-1].strip().strip("`").strip().startswith("{"):
        lines.pop()
    while lines and lines[-1].strip() in ("", "```", "```json"):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n"


LOWEST_TIER = "TIER_C"


def apply_backstop(v: dict) -> tuple[dict, str | None]:
    """Rules enforced in code, whatever the model answered:
    - verdicts are binary: a stray MAYBE becomes NOT_QUALIFIED;
    - QUALIFIED requires BUYER (otherwise NOT_QUALIFIED);
    - a NOT_QUALIFIED account is in the lowest tier, so the tier follows the final verdict.
    A QUALIFIED verdict with the lowest tier contradicts itself. It is left as the model returned it
    and noted; hubspot_tiers decides whether it reaches the CRM (the shipped setting holds it out)."""
    if v.get("status") != "ok":
        return v, None
    notes = []
    if v.get("qualification") == "MAYBE":
        v = dict(v, qualification="NOT_QUALIFIED")
        notes.append("MAYBE->NOT_QUALIFIED (verdicts are binary)")
    elif v.get("qualification") == "QUALIFIED" and v.get("buyer_or_vendor") != "BUYER":
        v = dict(v, qualification="NOT_QUALIFIED")
        notes.append(f"downgraded QUALIFIED->NOT_QUALIFIED (buyer_or_vendor={v.get('buyer_or_vendor')})")
    if v.get("qualification") == "NOT_QUALIFIED" and v.get("account_tier") != LOWEST_TIER:
        notes.append(f"tier {v.get('account_tier')}->{LOWEST_TIER} (not qualified)")
        v = dict(v, account_tier=LOWEST_TIER)
    elif v.get("qualification") == "QUALIFIED" and v.get("account_tier") == LOWEST_TIER:
        notes.append(f"QUALIFIED with {LOWEST_TIER}: the verdict contradicts its tier, kept as returned")
    return v, ("; ".join(notes) or None)


# ============================================================ steps
# Built-in tools a /qualify session may use. Everything else (Bash, PowerShell,
# Edit, WebFetch, Agent, ...) is removed with --tools, because user-level
# settings may allow Bash/PowerShell/WebFetch globally and P1 reads untrusted pages.
DEFAULT_QUALIFY_TOOLS = "Read,Glob,Grep,WebSearch"
# MCP tools /qualify may call (read-only page fetches), and only when crawler.session_page_fetch is
# true: these tools fetch whatever URL the session asks for and do not consult robots.txt.
DEFAULT_QUALIFY_MCP_TOOLS = ["mcp__scrapling__make_request", "mcp__scrapling__bulk_get",
                             "mcp__scrapling__fetch", "mcp__scrapling__bulk_fetch"]
# Added only when crawler.stealth_fallback is true as well: a fetch built to pass bot challenges.
STEALTH_MCP_TOOL = "mcp__scrapling__stealthy_fetch"
PAGE_FETCH_SERVER = "mcp__scrapling"
CRAWLER_BASH = "Bash(.venv/Scripts/python.exe scripts/source_pack.py:*)"
# What /source, /register-results and /enrich must never touch (they keep Apollo): the web in every
# form (search, fetch, the page-fetch server, the crawler script) and every other connector.
DEFAULT_SOURCE_DISALLOWED = [
    "PowerShell", "WebFetch", "WebSearch", PAGE_FETCH_SERVER, CRAWLER_BASH,
    "mcp__claude_ai_Gmail", "mcp__claude_ai_Smartlead_mcp", "mcp__claude_ai_HubSpot",
    "mcp__claude_ai_Google_Drive", "mcp__claude_ai_Google_Calendar", "mcp__claude_ai_Notion",
    "mcp__claude_ai_Scrubby", "mcp__claude_ai_Claude_Docs", "mcp__hubspot",
    "mcp__claude_ai_Apollo_io__apollo_emailer_messages_send_now",
    "mcp__claude_ai_Apollo_io__apollo_emailer_campaigns_add_contact_ids",
    "mcp__claude_ai_Apollo_io__apollo_sequences_create",
]
APOLLO_SERVERS = ["mcp__claude_ai_Apollo_io", "mcp__apollo"]
# What /qualify must never touch. The strict MCP configuration already keeps every connector out of
# that session; this list is the second layer, in case a connector is loaded anyway: Apollo as a
# whole, and every connector the Apollo sessions are refused.
DEFAULT_QUALIFY_DISALLOWED = APOLLO_SERVERS + [
    t for t in DEFAULT_SOURCE_DISALLOWED
    if t.startswith("mcp__") and t != PAGE_FETCH_SERVER and not any(t.startswith(a) for a in APOLLO_SERVERS)]
SCRAPLING_FALLBACK = {"type": "stdio", "command": "", "args": [], "env": {}}  # command filled in at runtime


def qualify_mcp_config(root: Path, settings: dict) -> Path:
    """Write state/mcp-qualify.json: only the MCP servers /qualify needs. None by default; the page
    fetcher when crawler.session_page_fetch is true."""
    wanted = (settings.get("qualify_mcp_servers") or ["scrapling"]) if session_page_fetch(settings) else []
    servers = {}
    try:
        servers = json.loads((root / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers", {})
    except Exception:
        pass
    out = {}
    for name in wanted:
        if name in servers:
            out[name] = servers[name]
        elif name == "scrapling":
            out[name] = dict(SCRAPLING_FALLBACK, command=str(root / ".venv" / "Scripts" / "scrapling-mcp.exe"))
    path = root / "state" / "mcp-qualify.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": out}, indent=1), encoding="utf-8")
    return path


def session_page_fetch(settings: dict) -> bool:
    return (settings.get("crawler") or {}).get("session_page_fetch") is True


def qualify_mcp_tools(settings: dict) -> list[str]:
    """Page-fetch tools of the /qualify session. None unless crawler.session_page_fetch is true; the
    stealth tool only when crawler.stealth_fallback is true as well."""
    if not session_page_fetch(settings):
        return []
    mcp_tools = list(settings.get("qualify_mcp_tools") or DEFAULT_QUALIFY_MCP_TOOLS)
    stealth = (settings.get("crawler") or {}).get("stealth_fallback") is True
    if stealth and STEALTH_MCP_TOOL not in mcp_tools:
        mcp_tools.append(STEALTH_MCP_TOOL)
    elif not stealth:
        mcp_tools = [t for t in mcp_tools if t != STEALTH_MCP_TOOL]
    return mcp_tools


def qualify_disallowed(settings: dict) -> list[str]:
    """Tools refused to the /qualify session by name: Apollo, every other connector, and the page
    fetcher (as a whole, or only its stealth tool) when the matching switch is off."""
    dis = list(settings.get("qualify_disallowed_tools") or DEFAULT_QUALIFY_DISALLOWED)
    for a in APOLLO_SERVERS:                        # a settings list cannot take Apollo out
        if a not in dis:
            dis.append(a)
    allowed = qualify_mcp_tools(settings)
    if not allowed:
        dis.append(PAGE_FETCH_SERVER)
    elif STEALTH_MCP_TOOL not in allowed:
        dis.append(STEALTH_MCP_TOOL)
    return dis


def qualify_args(domain: str, name: str, model: str, settings: dict, root: Path | None = None) -> list[str]:
    root = root or REPO_ROOT
    safe_name = (name or domain).replace('"', "").replace("\n", " ").replace("\r", " ").strip()
    tools = settings.get("qualify_tools") or DEFAULT_QUALIFY_TOOLS
    mcp_tools = qualify_mcp_tools(settings)
    a = ["-p", f'/qualify {domain} "{safe_name}"', "--model", model,
         "--output-format", "json", "--permission-mode", "dontAsk",
         "--strict-mcp-config", "--mcp-config", str(qualify_mcp_config(root, settings)),
         "--tools", tools,
         "--allowedTools", ",".join(tools.split(",") + mcp_tools),
         "--disallowedTools", ",".join(qualify_disallowed(settings))]
    if settings.get("max_turns_qualify"):
        a += ["--max-turns", str(settings["max_turns_qualify"])]
    schema = settings.get("qualify_json_schema")  # opt-in: it turns `result` into JSON only (no p1.md prose)
    if schema and (root / schema).exists():
        a += ["--json-schema", json.dumps(json.loads((root / schema).read_text(encoding="utf-8")),
                                          separators=(",", ":"))]
    return a


def _apollo_side_args(settings: dict) -> list[str]:
    dis = settings.get("source_disallowed_tools") or DEFAULT_SOURCE_DISALLOWED
    return ["--disallowedTools", ",".join(dis)]


def source_args(date: str, n: int, model: str, settings: dict) -> list[str]:
    a = ["-p", f"/source {date} {n}", "--model", settings.get("source_model") or model,
         "--output-format", "json", "--permission-mode", "dontAsk"] + _apollo_side_args(settings)
    if settings.get("max_turns_source"):
        a += ["--max-turns", str(settings["max_turns_source"])]
    return a


def register_args(date: str, model: str, settings: dict) -> list[str]:
    a = ["-p", f"/register-results {date}", "--model", settings.get("source_model") or model,
         "--output-format", "json", "--permission-mode", "dontAsk"] + _apollo_side_args(settings)
    if settings.get("max_turns_register") or settings.get("max_turns_source"):
        a += ["--max-turns", str(settings.get("max_turns_register") or settings["max_turns_source"])]
    return a


# Granted to /enrich only, per call: the project allow list keeps enrich tools out so
# /source and /qualify can never spend enrichment credits.
ENRICH_TOOLS = ["mcp__claude_ai_Apollo_io__apollo_organizations_bulk_enrich",
                "mcp__apollo__apollo_organizations_bulk_enrich",
                "Bash(.venv/Scripts/python.exe scripts/enrich.py:*)"]


def enrich_args(model: str, settings: dict) -> list[str]:
    a = ["-p", "/enrich", "--model", settings.get("source_model") or model,
         "--output-format", "json", "--permission-mode", "dontAsk",
         "--allowedTools", ",".join(ENRICH_TOOLS)] + _apollo_side_args(settings)
    if settings.get("max_turns_source"):
        a += ["--max-turns", str(settings["max_turns_source"])]
    return a


def orphan_source_dates(root: Path, before: str) -> list[str]:
    """Earlier /source runs that selected candidates but never wrote a queue file.

    Recovery is `/source <d> <cap>`: the skill resumes at
    registration (bulk_create with dedupe + label, no new search) and then
    builds the queue. A bare `source_queue.py build` would skip registration.
    """
    sdir = root / "state" / "source"
    if not sdir.exists():
        return []
    out = []
    for d in sorted(sdir.iterdir()):
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}[b-z]?", d.name) and d.name < before \
                and (d / "candidates.json").exists() \
                and not (root / "state" / "queue" / f"{d.name}.jsonl").exists():
            out.append(d.name)
    return out


def unregistered_dates(root: Path, before: str) -> list[str]:
    """Earlier queue dates whose terminal lines are not yet in Apollo."""
    try:
        import source_queue  # scripts/source_queue.py
    except Exception:
        return []
    out = []
    for qf in sorted(glob.glob(str(root / "state" / "queue" / "*.jsonl"))):
        qd = Path(qf).stem
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[b-z]?", qd) and qd <= before:
            try:
                if source_queue.unregistered(root, qd):
                    out.append(qd)
            except Exception as e:
                log(f"WARNING: source_queue.unregistered({qd}) failed: {e}")
    return out


def load_verdict(res: dict, work_dir: Path, started: float) -> dict:
    """Verdict from the claude result; fall back to a fresh p1.json."""
    j = res.get("json") or {}
    so = j.get("structured_output")
    if isinstance(so, dict):
        return parse_verdict(json.dumps(so))
    try:
        return parse_verdict(str(j.get("result") or ""))
    except VerdictError as first:
        p = work_dir / "p1.json"
        if p.exists() and p.stat().st_mtime >= started - 1:
            return parse_verdict(p.read_text(encoding="utf-8", errors="replace"))
        raise first


def write_hubspot(row: dict, verdict: dict, date: str, dry_run: bool, settings: dict,
                  client_holder: dict) -> dict:
    props = hw.build_properties(verdict, row, date)
    if "client" not in client_holder:
        client_holder["client"] = hw.get_client()
    client = client_holder["client"]
    do_dry = dry_run or not settings.get("hubspot_write", True)
    return hw.upsert(client, props["domain"], props, dry_run=do_dry)


# ============================================================ report
def build_report(rep: dict) -> str:
    c = rep["counts"]
    lines = [f"# Nightly run {rep['date']}{' (DRY RUN)' if rep['dry_run'] else ''}", "",
             f"- Started {rep['started_at']}, finished {rep.get('finished_at', '')}",
             f"- Model `{rep['model']}`, cap {rep['cap']}",
             f"- HubSpot records given employee count/industry: {c.get('enriched', 0)}",
             f"- Qualified but kept out of HubSpot (tier not in hubspot_tiers): {c.get('held', 0)}"
             + (f", listed in `{HELD_CSV}`" if c.get("held") else ""),
             f"- Stopped: **{rep.get('stopped_reason') or 'completed'}**"
             + (f" ({rep['stopped_detail']})" if rep.get("stopped_detail") else ""),
             "",
             "| pulled | prefiltered | backlog | processed | qualified | not qualified | errors | HubSpot created | updated | exists/unchanged | needs review | still pending |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|",
             f"| {c['pulled']} | {c['prefiltered']} | {c['backlog']} | {c['processed']} | {c['qualified']} | "
             f"{c['not_qualified']} | {c['errors']} | {c['hubspot_created']} | "
             f"{c['hubspot_updated']} | {c['hubspot_skipped']} | {c.get('needs_review', 0)} | {c['pending_left']} |", ""]
    for note in rep.get("notes", []):
        lines.append(f"> {note}")
    if rep.get("notes"):
        lines.append("")
    review = [co for co in rep["companies"] if co.get("status") == "needs_review"]
    if review:
        lines += ["## Needs review (Qualified by P1, NOT written to HubSpot)", ""]
        for co in review:
            lines.append(f"- **{co.get('name', '')}** ({co['domain']}): {co.get('error', '')} {co.get('hubspot_url', '')}")
        lines.append("")
    lines += ["## Companies", "",
              "| # | company | queue | verdict | buyer/vendor | tier | HubSpot | reason / error |",
              "|---|---|---|---|---|---|---|---|"]
    for i, co in enumerate(rep["companies"], 1):
        hs = co.get("hubspot_action") or ""
        if co.get("hubspot_url"):
            hs = f"[{hs}]({co['hubspot_url']})"
        why = co.get("error") or co.get("reason") or ""
        if co.get("hubspot_action") == "dry_run" and co.get("hubspot_note"):
            why = f"({co['hubspot_note']}) {why}"
        if co.get("backstop"):
            why = f"{co['backstop']}. {why}"
        if co.get("scope_warnings"):
            why = f"[scope: {'; '.join(map(str, co['scope_warnings']))}] {why}"
        if co.get("crawl"):
            why = f"[crawl: {co['crawl']}] {why}"
        why = why.replace("|", "/").replace("\n", " ")
        if len(why) > 220:
            why = why[:217] + "..."
        lines.append(f"| {i} | {co.get('name', '')} ({co['domain']}) | {co.get('queue_date', '')} | "
                     f"{co.get('verdict') or co.get('status', '')} | {co.get('buyer_or_vendor', '')} | "
                     f"{co.get('account_tier', '')} | {hs} | {why} |")
    lines.append("")
    return "\n".join(lines)


def new_report(date, cap, model, dry_run) -> dict:
    return {"date": date, "started_at": now_iso(), "finished_at": None, "dry_run": dry_run,
            "cap": cap, "model": model, "stopped_reason": None, "stopped_detail": "",
            "counts": {k: 0 for k in ("pulled", "prefiltered", "backlog", "processed", "qualified",
                                      "not_qualified", "errors", "hubspot_created",
                                      "hubspot_updated", "hubspot_skipped", "needs_review", "pending_left", "held")},
            "source": None, "enrich_runs": [], "register_results": [], "companies": [], "notes": [],
            "total_cost_usd_equiv": 0.0}


def save_report(root: Path, rep: dict) -> Path:
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = rep["date"] + ("-dry-run" if rep["dry_run"] else "")
    stem, n = base, 1
    while (runs / f"{stem}.json").exists() or (runs / f"{stem}.md").exists():
        n += 1
        stem = f"{base}-{n}"
    rep["report_file"] = f"runs/{stem}.md"
    (runs / f"{stem}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    md = runs / f"{stem}.md"
    md.write_text(build_report(rep), encoding="utf-8")
    return md


# ============================================================ main loop
def acquire_lock(root: Path, stale_after_s: float) -> Path | None:
    lock = root / "state" / "nightly.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < stale_after_s:
        return None
    lock.write_text(f"{os.getpid()} {now_iso()}", encoding="utf-8")
    return lock


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nightly sourcing + Prompt 1 driver")
    ap.add_argument("--cap", type=int, help="max companies processed this run (default settings.per_run_cap)")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default today)")
    ap.add_argument("--model", help="Claude model alias/id (default settings.model)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no Apollo pull, no HubSpot writes, no queue changes; P1 still runs")
    ap.add_argument("--resume", action="store_true", help="do not call /source; only drain existing queue lines")
    ap.add_argument("--root", help="project root (default SOURCING_ROOT or the repo)")
    a = ap.parse_args(argv)

    root = resolve_root(a.root)
    settings = load_settings(root)
    date = a.date or dt.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[b-z]?", date):
        ap.error("--date must be YYYY-MM-DD or a same-day batch id like 2030-01-16b")
    cap = a.cap if a.cap is not None else int(settings["per_run_cap"])
    model = a.model or settings["model"]
    dry = a.dry_run
    max_attempts = int(settings["max_attempts"])
    deadline = time.monotonic() + float(settings["run_deadline_min"]) * 60
    rep = new_report(date, cap, model, dry)
    qdir = root / "state" / "queue"
    today_q = qdir / f"{date}.jsonl"

    def remaining() -> float:
        return deadline - time.monotonic()

    def finish(code: int) -> int:
        rep["finished_at"] = now_iso()
        md = save_report(root, rep)
        log(f"report: {md}")
        return code

    global _auth_mode
    try:
        _auth_mode = auth_mode(settings)
    except ValueError as e:
        rep["stopped_reason"], rep["stopped_detail"] = "bad_settings", str(e)
        log(f"ABORT: {e}")
        return finish(2)
    rep["claude_auth"] = _auth_mode
    guard = api_key_guard(root, _auth_mode)
    if guard:
        rep["stopped_reason"], rep["stopped_detail"] = "api_key_guard", guard
        log(f"ABORT: {guard}")
        return finish(3)

    lock = None
    if not dry:
        lock = acquire_lock(root, float(settings["run_deadline_min"]) * 60 + 1800)
        if lock is None:
            rep["stopped_reason"], rep["stopped_detail"] = "locked", "another run holds state/nightly.lock"
            log("ABORT: another run is active (state/nightly.lock)")
            return finish(4)
    try:
        try:
            code = _run_all(a, root, settings, date, cap, model, dry, max_attempts,
                            remaining, rep, qdir, today_q)
        except Exception as e:  # never leave the morning without a report
            import traceback
            rep["stopped_reason"] = "driver_crash"
            rep["stopped_detail"] = f"{type(e).__name__}: {e}"[:300]
            rep["traceback"] = traceback.format_exc()[-2000:]
            log(f"CRASH: {rep['stopped_detail']}")
            code = 1
        return finish(code)
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


def _run_all(a, root, settings, date, cap, model, dry, max_attempts, remaining, rep, qdir, today_q) -> int:
    # ---- 1. backlog from earlier queue files (oldest first)
    if not dry and not a.resume:
        for d in orphan_source_dates(root, date):
            if remaining() < 60:
                break
            log(f"recovering orphaned /source run {d}")
            res = run_claude(source_args(d, cap, model, settings),
                             min(float(settings["timeout_source_s"]), remaining()), root)
            kind = classify_claude(res)
            rep["total_cost_usd_equiv"] += float((res.get("json") or {}).get("total_cost_usd") or 0)
            ok = kind == "ok" and (qdir / f"{d}.jsonl").exists()
            rep["notes"].append(f"orphaned /source run {d}: " + ("recovered" if ok else f"NOT recovered ({kind})"))
            if kind == "usage_limit":
                rep["stopped_reason"], rep["stopped_detail"] = "usage_limit", f"during /source recovery of {d}"
                return 0
    queues: dict[str, list[dict]] = {}
    work: list[tuple[str, dict]] = []  # (queue_date, row)
    seen: set[str] = set()
    for qf in sorted(glob.glob(str(qdir / "*.jsonl"))):
        qd = Path(qf).stem
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[b-z]?", qd) or qd >= date:
            continue
        rows = read_queue(Path(qf))
        queues[qd] = rows
        for r in rows:
            d = hw.normalize_domain(r.get("domain", ""))
            if d and d not in seen and is_retryable(r, max_attempts):
                seen.add(d)
                work.append((qd, r))
    rep["counts"]["backlog"] = len(work)
    if work:
        log(f"backlog from earlier queues: {len(work)}")

    # ---- 2. today's queue
    if not today_q.exists() and not a.resume:
        need = cap - len(work)
        if dry:
            rep["notes"].append("dry-run: /source not called (it spends Apollo credits and writes Apollo).")
        elif need <= 0:
            rep["notes"].append(f"backlog ({len(work)}) fills the cap; /source not called.")
        elif remaining() < 60:
            rep["notes"].append("deadline reached before /source.")
        else:
            log(f"/source {date} {need}")
            res = run_claude(source_args(date, need, model, settings),
                             min(float(settings["timeout_source_s"]), remaining()), root)
            kind = classify_claude(res)
            summary = None
            try:
                summary = _last_json_line(str((res.get("json") or {}).get("result") or ""))
            except VerdictError:
                pass
            rep["source"] = {"kind": kind, "summary": summary, "duration_s": res.get("duration_s"),
                             "rc": res.get("rc"), "tail": claude_text(res)[-500:] if kind != "ok" else ""}
            rep["total_cost_usd_equiv"] += float((res.get("json") or {}).get("total_cost_usd") or 0)
            if kind == "usage_limit":
                rep["stopped_reason"], rep["stopped_detail"] = "usage_limit", "during /source"
                return 0
            s_status = (summary or {}).get("status")
            if summary:
                if summary.get("unregistered"):
                    rep["notes"].append(f"/source: {summary['unregistered']} candidates could not be "
                                        "registered in Apollo (they may resurface in Net New).")
                for w in summary.get("warnings") or []:
                    rep["notes"].append(f"/source warning: {w}")
                if s_status == "error" and summary.get("error"):
                    rep["notes"].append(f"/source error: {summary['error']}")
            if kind == "ok" and s_status == "empty":
                rep["notes"].append("/source found no net-new companies.")
            elif kind != "ok" or s_status == "error" or not today_q.exists():
                rep["notes"].append(f"/source failed ({kind}, status={s_status}); continuing with backlog only.")
                if not work:
                    rep["stopped_reason"] = "source_failed"
                    rep["stopped_detail"] = claude_text(res)[-300:]
                    return 1
    today_rows = read_queue(today_q)
    queues[date] = today_rows
    rep["counts"]["pulled"] = sum(1 for r in today_rows if r.get("status") != "skipped_prefilter")
    rep["counts"]["prefiltered"] = sum(1 for r in today_rows if r.get("status") == "skipped_prefilter")
    for r in today_rows:
        d = hw.normalize_domain(r.get("domain", ""))
        if d and d not in seen and is_retryable(r, max_attempts):
            seen.add(d)
            work.append((date, r))
    if not work:
        rep["stopped_reason"] = rep["stopped_reason"] or "no_companies"
        return 0

    # ---- 3. HubSpot preflight (read-only): fail the run early, not per company
    client_holder: dict = {}
    if settings.get("hubspot_write", True):
        try:
            client_holder["client"] = hw.get_client()
            if client_holder["client"] is not None:
                hw.live_enum_options(client_holder["client"])
            elif not dry:
                raise RuntimeError("no HubSpot token (keyring account-sourcing/HUBSPOT_TOKEN)")
            else:
                rep["notes"].append("dry-run: no HubSpot token, lookups skipped.")
        except Exception as e:
            if not dry:
                rep["stopped_reason"], rep["stopped_detail"] = "hubspot_unavailable", str(e)[:300]
                log(f"ABORT: HubSpot preflight failed: {e}")
                return 1
            rep["notes"].append(f"dry-run: HubSpot preflight failed ({e}); lookups skipped.")
            client_holder["client"] = None

    # ---- 3b. crawler identity: under a placeholder name no site is crawled
    no_crawl = None if os.environ.get("SOURCING_SOURCE_PACK_CMD") else crawler_problem(root)
    if no_crawl:
        rep["notes"].append(f"No site was crawled: {no_crawl}. Qualification ran from web search only.")
        log(f"WARNING: {no_crawl}; no site is crawled")

    # ---- 4. per-company loop
    consecutive_err = consecutive_transient = 0
    touched_dates: set[str] = set()
    processed = 0

    def persist(qd: str) -> None:
        if not dry:
            write_queue(qdir / f"{qd}.jsonl", queues[qd])

    for qd, row in work:
        if processed >= cap:
            rep["stopped_reason"] = "cap_reached"
            break
        if (root / "state" / "stop.flag").exists():  # set by run_until_done --stop-at
            rep["stopped_reason"] = "stop_requested"
            break
        if remaining() < 120:
            rep["stopped_reason"] = "deadline"
            break
        domain = hw.normalize_domain(str(row.get("domain") or ""))
        name = str(row.get("name") or domain)
        co = {"domain": domain, "name": name, "queue_date": qd}
        if row.get("scope_warnings"):
            co["scope_warnings"] = list(row["scope_warnings"]) if isinstance(row["scope_warnings"], list) \
                else [str(row["scope_warnings"])]
        rep["companies"].append(co)
        attempts_before = int(row.get("attempts", 0) or 0)
        try:
            wdir = root / "state" / "work" / domain
            wdir.mkdir(parents=True, exist_ok=True)

            # HubSpot-only retry: P1 already done, do not spend Claude again.
            if row.get("status") == "hubspot_error" and (wdir / "p1.json").exists():
                processed += 1
                try:
                    verdict = json.loads((wdir / "p1.json").read_text(encoding="utf-8"))
                    verdict, _ = apply_backstop(parse_verdict(json.dumps(verdict)))
                    hs = write_hubspot(row, verdict, row.get("p1_date") or date, dry, settings, client_holder)
                    _record_hs(rep, co, row, hs)
                    if not dry and row.get("status") == "hubspot_error":
                        row["status"] = "done"  # _record_hs may have set needs_review instead
                except Exception as e:
                    row["hubspot_attempts"] = int(row.get("hubspot_attempts", 0)) + 1
                    co.update(status="hubspot_error", error=f"HubSpot: {e}")
                    rep["counts"]["errors"] += 1
                co.setdefault("status", row.get("status"))
                co["verdict"] = row.get("verdict")
                touched_dates.add(qd)
                persist(qd)
                continue

            log(f"[{processed + 1}/{min(cap, len(work))}] {name} ({domain})")
            t_start = time.time()
            # a. source pack (non-fatal)
            if no_crawl:
                co["source_pack"] = "not run (placeholder user agent)"
            elif not (wdir / "sources" / "index.txt").exists():
                sp = run_source_pack(domain, min(float(settings["timeout_source_pack_s"]), remaining()), root)
                if sp.get("timed_out") or sp.get("rc") != 0:
                    co["source_pack"] = "timeout" if sp.get("timed_out") else f"rc={sp.get('rc')}"
                    log(f"  source pack failed ({co['source_pack']}); P1 continues without it")
            crawl = crawl_status(wdir)
            if crawl:
                co["crawl"] = crawl
                log(f"  site not crawled: {crawl}")
            # b. inputs for the skill
            apollo = dict(row, date=qd)
            (wdir / "apollo.json").write_text(json.dumps(apollo, ensure_ascii=False, indent=2), encoding="utf-8")
            # c. Prompt 1
            res = run_claude(qualify_args(domain, name, model, settings, root),
                             min(float(settings["timeout_qualify_s"]), remaining()), root)
            j = res.get("json") or {}
            rep["total_cost_usd_equiv"] += float(j.get("total_cost_usd") or 0)
            co["duration_s"] = res.get("duration_s")
            co["num_turns"] = j.get("num_turns")
            kind = classify_claude(res)
            if kind == "usage_limit":
                co.update(status="pending", error="usage limit hit; left pending for next run")
                rep["stopped_reason"] = "usage_limit"
                rep["stopped_detail"] = claude_text(res)[-300:]
                log("  usage limit: stopping cleanly")
                break
            processed += 1
            touched_dates.add(qd)
            row["attempts"] = int(row.get("attempts", 0)) + 1
            row["finished_at"] = now_iso()
            verdict = None
            err = None
            if kind in ("timeout", "transient", "error"):
                err = f"{kind}: {claude_text(res)[-200:].strip()}"
            elif not str(j.get("result") or "").strip() and not j.get("structured_output") \
                    and not j.get("num_turns"):
                err = "empty result with 0 turns (skill not found, or a blocked !`shell` line in the skill)"
            else:
                try:
                    verdict = load_verdict(res, wdir, t_start)
                    if verdict.get("status") == "error":
                        err = f"skill error: {verdict.get('error', '')}"
                        verdict = None
                    elif hw.normalize_domain(verdict.get("domain", "")) != domain:
                        err = f"verdict domain mismatch: {verdict.get('domain')!r}"
                        verdict = None
                except VerdictError as e:
                    err = f"unparseable verdict: {e}"
            consecutive_transient = consecutive_transient + 1 if kind == "transient" else 0

            if err:
                row.update(status="error", error=err)
                co.update(status="error", error=err)
                rep["counts"]["errors"] += 1
                consecutive_err += 1
                log(f"  error: {err[:160]}")
                persist(qd)
                if consecutive_transient >= int(settings["max_consecutive_transient"]):
                    rep["stopped_reason"] = "api_errors"
                    break
                if consecutive_err >= int(settings["max_consecutive_errors"]):
                    rep["stopped_reason"] = "consecutive_errors"
                    break
                continue
            consecutive_err = 0

            verdict, note = apply_backstop(verdict)
            if note:
                co["backstop"] = note
                log(f"  {note}")
            (wdir / "p1.json").write_text(json.dumps(verdict, ensure_ascii=False), encoding="utf-8")
            (wdir / "p1.md").write_text(strip_verdict_line(str(j.get("result") or "")), encoding="utf-8")
            q = verdict["qualification"]
            row.update(verdict=q, buyer_or_vendor=verdict["buyer_or_vendor"], p1_date=today_iso(), error=None)
            co.update(verdict=q, buyer_or_vendor=verdict["buyer_or_vendor"],
                      account_tier=verdict.get("account_tier", ""),
                      buyer_vendor_reason=verdict.get("buyer_vendor_reason", ""),
                      reason=verdict.get("qualification_reason", ""))
            rep["counts"]["qualified" if q == "QUALIFIED" else "not_qualified"] += 1
            row["status"] = "done"
            tiers = settings.get("hubspot_tiers")  # e.g. ["TIER_A"]; unset = every tier
            if q == "QUALIFIED" and tiers and verdict.get("account_tier") not in tiers:
                row["hubspot_action"] = "held"
                co.update(hubspot_action="held",
                          hubspot_note=f"{verdict.get('account_tier')} kept out of HubSpot (hubspot_tiers)")
                rep["counts"]["held"] += 1
                if not dry:
                    record_held(root, row, verdict, qd)
            elif q == "QUALIFIED":
                try:
                    hs = write_hubspot(row, verdict, row["p1_date"], dry, settings, client_holder)
                    _record_hs(rep, co, row, hs)
                except Exception as e:
                    row["status"] = "hubspot_error"
                    row["hubspot_attempts"] = int(row.get("hubspot_attempts", 0)) + 1
                    row["error"] = f"HubSpot: {e}"
                    co["error"] = row["error"]
                    rep["counts"]["errors"] += 1
                    log(f"  HubSpot error: {e}")
            co["status"] = row["status"]
            log(f"  {q} -> {co.get('hubspot_action', 'no HubSpot write')}")
            persist(qd)
            # employee count + industry right away, so the record lands complete
            if not dry and co.get("hubspot_action") in ("created", "updated"):
                if enrich_step(root, settings, model, client_holder, remaining, rep) == "usage_limit":
                    rep["stopped_reason"] = "usage_limit"
                    rep["stopped_detail"] = "during /enrich"
                    break
        except Exception as e:  # one bad company must never kill the night
            if int(row.get("attempts", 0) or 0) == attempts_before:
                processed += 1
            msg = f"driver error: {type(e).__name__}: {e}"[:300]
            log(f"  {msg}")
            row.update(status="error", error=msg,
                       attempts=max(int(row.get("attempts", 0) or 0), attempts_before + 1))
            co.update(status="error", error=msg)
            rep["counts"]["errors"] += 1
            touched_dates.add(qd)
            try:
                persist(qd)
            except Exception as pe:
                log(f"  could not persist queue {qd}: {pe}")
            consecutive_err += 1
            if consecutive_err >= int(settings["max_consecutive_errors"]):
                rep["stopped_reason"] = "consecutive_errors"
                break

    rep["counts"]["processed"] = processed
    if rep["stopped_reason"] is None and processed >= cap and \
            any(is_retryable(r, max_attempts) for rows in queues.values() for r in rows):
        rep["stopped_reason"] = "cap_reached"
    rep["counts"]["pending_left"] = sum(1 for rows in queues.values() for r in rows
                                        if r.get("status") == "pending")

    # ---- 4b. catch-up: enrichment that failed right after a push is retried here
    if not dry and rep["stopped_reason"] != "usage_limit":
        enrich_step(root, settings, model, client_holder, remaining, rep)

    # ---- 5. P1 results back to Apollo (this run's dates + earlier unregistered ones)
    if not dry:
        touched_dates |= set(unregistered_dates(root, date))
    if not dry and settings.get("register_results", True) \
            and (settings.get("apollo") or {}).get("register_results", True) and touched_dates \
            and rep["stopped_reason"] != "usage_limit":
        for qd in sorted(touched_dates):
            if remaining() < 60:
                rep["notes"].append("deadline: /register-results skipped")
                break
            res = run_claude(register_args(qd, model, settings),
                             min(float(settings["timeout_register_s"]), remaining()), root)
            kind = classify_claude(res)
            rep["register_results"].append({"date": qd, "kind": kind,
                                             "tail": claude_text(res)[-300:] if kind != "ok" else ""})
            if kind == "usage_limit":
                rep["notes"].append("usage limit during /register-results; rerun it tomorrow")
                break
    elif dry:
        rep["notes"].append("dry-run: /register-results not called; queue files unchanged.")
    return 0


HELD_CSV = "runs/held_qualified.csv"
HELD_FIELDS = ["p1_date", "batch", "name", "domain", "account_tier", "need_strength", "current_approach",
               "timing", "evidence_confidence", "qualification_reason", "strongest_signal", "main_uncertainty",
               "apollo_account_id"]


def record_held(root: Path, row: dict, verdict: dict, batch: str) -> None:
    """Append a QUALIFIED company that was kept out of HubSpot (e.g. a lower tier) to runs/held_qualified.csv."""
    import csv
    path = root / HELD_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HELD_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({**{k: verdict.get(k, "") for k in HELD_FIELDS}, "p1_date": row.get("p1_date"),
                    "batch": batch, "name": verdict.get("name") or row.get("name"),
                    "domain": row.get("domain"), "apollo_account_id": row.get("apollo_account_id") or ""})


def enrich_step(root: Path, settings: dict, model: str, client_holder: dict, remaining, rep: dict) -> str | None:
    """Employee count + industry for pushed HubSpot records that lack them (1 Apollo credit
    per matched company, each company at most once). Returns the /enrich call kind, or None
    when nothing was run."""
    if not (settings.get("apollo") or {}).get("enrich_pushed", False) \
            or client_holder.get("client") is None or remaining() < 120:
        return None
    try:
        todo = enrich.todo(root, client_holder["client"])  # HubSpot reads only, no credits
    except Exception as e:
        rep["notes"].append(f"/enrich skipped: could not list companies ({e})"[:300])
        return None
    if not todo["count"]:
        return None
    log(f"  /enrich ({', '.join(todo['domains'])})")
    res = run_claude(enrich_args(model, settings), min(float(settings["timeout_register_s"]), remaining()), root)
    kind = classify_claude(res)
    rep["total_cost_usd_equiv"] += float((res.get("json") or {}).get("total_cost_usd") or 0)
    summary = None
    try:
        summary = _last_json_line(str((res.get("json") or {}).get("result") or ""))
    except VerdictError:
        pass
    runs = rep.setdefault("enrich_runs", [])
    runs.append({"kind": kind, "summary": summary, "tail": claude_text(res)[-300:] if kind != "ok" else ""})
    if kind == "usage_limit":
        rep["notes"].append("usage limit during /enrich; the next run retries it")
    elif summary and summary.get("status") in ("ok", "partial"):
        c = rep["counts"]
        c["enriched"] = c.get("enriched", 0) + int(summary.get("enriched", 0))
        for u in summary.get("unmapped_industry") or []:
            rep["notes"].append(f"/enrich: industry not mapped to a HubSpot option: {u}")
        for e in summary.get("errors") or []:
            rep["notes"].append(f"/enrich error: {e}")
        log(f"  /enrich: {summary.get('enriched', 0)} enriched, {summary.get('not_found', 0)} not found")
    else:
        rep["notes"].append(f"/enrich failed ({kind}): {(summary or {}).get('error') or claude_text(res)[-200:]}")
        log(f"  /enrich failed ({kind})")
    return kind


def _record_hs(rep: dict, co: dict, row: dict, hs: dict) -> None:
    act = hs.get("action")
    co.update(hubspot_action=act, hubspot_url=hs.get("url", ""), hubspot_note=hs.get("note", ""))
    row.update(hubspot_action=act, hubspot_id=hs.get("id"))
    if act == "created":
        rep["counts"]["hubspot_created"] += 1
    elif act == "updated":
        rep["counts"]["hubspot_updated"] += 1
    elif act in ("exists", "unchanged"):
        rep["counts"]["hubspot_skipped"] += 1
    if act in ("conflict", "invalid", "human_verdict_exists"):
        # Qualified by P1 but not written: terminal, surfaced for the owner.
        row["status"] = "needs_review"
        rep["counts"]["needs_review"] += 1
        co["error"] = f"HubSpot {act}: {hs.get('note', '')}"


if __name__ == "__main__":
    sys.exit(main())
