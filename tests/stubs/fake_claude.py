"""Stand-in for `claude -p ... --output-format json`, driven by a scenario file.

Env:
  SOURCING_STUB_SCENARIO  path to scenario JSON:
     {"source": {"date": "YYYY-MM-DD", "rows": [...], "mode": "ok|usage_limit|fail|none"},
      "qualify": {"<domain>": {"verdict": {...}} | {"usage_limit": true} | {"crash": true}
                              | {"garbage": "text"} | {"is_error": "msg"} | {"max_turns": true}}}
  SOURCING_STUB_LOG       path; one JSON line per invocation {argv, env_flags}
  SOURCING_ROOT           repo root the driver is using (queue is written under it)
"""
import json
import os
import sys
from pathlib import Path


def out(result, is_error=False, code=0, subtype="success"):
    print(json.dumps({"type": "result", "subtype": subtype, "result": result, "is_error": is_error,
                      "session_id": "stub", "total_cost_usd": 0.0}, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(code)


def main():
    argv = sys.argv[1:]
    scen = json.loads(Path(os.environ["SOURCING_STUB_SCENARIO"]).read_text(encoding="utf-8"))
    log = os.environ.get("SOURCING_STUB_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "argv": argv,
                "has_anthropic_key": "ANTHROPIC_API_KEY" in os.environ,
                "has_anthropic_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
                "has_hubspot_token": "HUBSPOT_TOKEN" in os.environ,
            }, ensure_ascii=False) + "\n")
    prompt = next((a for a in argv if a.startswith("/")), " ".join(argv))
    root = Path(os.environ.get("SOURCING_ROOT", "."))

    if prompt.startswith("/source"):
        s = scen.get("source", {})
        mode = s.get("mode", "ok")
        if mode == "usage_limit":
            out("Claude AI usage limit reached|1900000000", True, 1)
        if mode == "fail":
            out("Apollo MCP not connected", True, 1)
        q = root / "state" / "queue" / f"{s['date']}.jsonl"
        if mode == "empty":   # real /source: build reports status empty and writes NO queue file
            out("Nothing net-new.\n" + json.dumps({"status": "empty", "date": s["date"], "pulled": 0, "pending": 0,
                                                     "prefiltered": 0, "queue_file": None, "error": None}))
        if mode != "none":
            q.parent.mkdir(parents=True, exist_ok=True)
            with open(q, "w", encoding="utf-8") as f:
                for r in s.get("rows", []):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        n = len(s.get("rows", [])) if mode != "none" else 0
        out("Pulled companies.\n" + json.dumps({"status": "ok", "pulled": n, "queue": str(q)}))

    if prompt.startswith("/qualify"):
        parts = prompt.split()
        domain = parts[1] if len(parts) > 1 else ""
        b = scen.get("qualify", {}).get(domain, {})
        if b.get("usage_limit"):
            out("Claude AI usage limit reached|1900000000", True, 1)
        if "raw_file" in b:  # replay a captured real `claude -p --output-format json` stdout
            sys.stdout.write(Path(b["raw_file"]).read_text(encoding="utf-8"))
            sys.exit(0)
        if b.get("empty_zero_turns"):
            print(json.dumps({"type": "result", "subtype": "success", "result": "", "is_error": False,
                              "num_turns": 0, "session_id": "stub", "total_cost_usd": 0.0}))
            sys.exit(0)
        if b.get("sleep"):
            import time
            time.sleep(float(b["sleep"]))
        if b.get("crash"):
            sys.stderr.write("Traceback: boom\n")
            sys.exit(3)
        if "garbage" in b:
            out(b["garbage"])
        if "is_error" in b:
            out(b["is_error"], True, 1)
        if b.get("max_turns"):
            out("", True, 1, subtype="error_max_turns")
        v = b.get("verdict")
        if v is None:
            out("no scenario for " + domain, True, 1)
        out("Wrote p1.md.\n" + json.dumps(v, ensure_ascii=False))

    if prompt.startswith("/register-results"):
        out(json.dumps({"status": "ok", "updated": 0}))

    out("unknown prompt " + prompt, True, 1)


if __name__ == "__main__":
    main()
