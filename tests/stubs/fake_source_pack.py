"""Stand-in for scripts/source_pack.py --light. Writes a tiny pack under cwd/state/work/<domain>/sources.
Env SOURCING_STUB_SP_FAIL: comma-separated domains that exit 1 without writing anything.
Env SOURCING_STUB_SP_BLOCKED: comma-separated domains whose site "refused the crawler" (status.json says blocked)."""
import os
import sys
from pathlib import Path

domain = sys.argv[1]
if domain in (os.environ.get("SOURCING_STUB_SP_FAIL") or "").split(","):
    sys.stderr.write("stub source pack failure\n")
    sys.exit(1)
out = Path.cwd() / "state" / "work" / domain / "sources"
out.mkdir(parents=True, exist_ok=True)
if domain in (os.environ.get("SOURCING_STUB_SP_BLOCKED") or "").split(","):
    (out / "index.txt").write_text("", encoding="utf-8")
    (out / "jobs.json").write_text('{"jobs": [], "source": null}', encoding="utf-8")
    (out / "status.json").write_text('{"status": "blocked", "reason": "http 403", "pages": 0}', encoding="utf-8")
    print(f"0 usable pages -> {out}")
    sys.exit(0)
(out / "index.txt").write_text(f"https://{domain}/ | Home | replenishment | 100\n", encoding="utf-8")
(out / "jobs.json").write_text('{"jobs": [], "source": null}', encoding="utf-8")
print(f"1 usable pages -> {out}")
