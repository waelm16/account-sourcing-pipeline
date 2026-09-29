"""Keep the nightly run going until the queue is exhausted.

Wraps scripts/nightly.ps1 --resume in a loop:
  - usage limit hit  -> sleep until the reset time Claude reported (+ a small buffer),
                        or re-check every --poll-min minutes when no time is given, then resume
  - run deadline / cap -> resume straight away
  - API overloaded (api_errors) -> wait --poll-min and resume, at most 4 times in a row
  - anything else (HubSpot down, consecutive skill errors, crash) -> stop; a human should look
It never calls /source, so it spends no Apollo search credits: it only drains the
queue files that already exist. If a run is already active (state/nightly.lock held
by a live process) it waits for that run to finish first.

  python scripts/run_until_done.py --date 2030-01-17 [--cap 1000] [--max-hours 20] [--poll-min 30]

Log: runs/<date>-until-done.log. Each resumed run writes its own report,
runs/<date>-2.md, runs/<date>-3.md, ...
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_nightly as rn  # noqa: E402

RESET_BUFFER_S = 180
MAX_API_ERROR_RETRIES = 4
CONTINUE_REASONS = {None, "", "completed", "deadline", "cap_reached"}

_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_CLOCK_RE = re.compile(r"reset[s]?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def seconds_until_reset(text: str, now: dt.datetime) -> float | None:
    """Seconds from `now` (local, naive) until the reset time named in Claude's
    usage-limit message, or None when the message names no time."""
    text = text or ""
    m = _EPOCH_RE.search(text)
    if m:
        return max(0.0, int(m.group(1)) - now.timestamp())
    m = _CLOCK_RE.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def next_date(date: str, root: Path | None = None, today: str | None = None) -> str:
    """Next batch id: today's real date, or today + b, c, ... when today already has batches.
    Never a future date (the batch id ends up in Apollo list names and reports)."""
    import source_queue as sq
    return sq.next_batch(root or ROOT, max(today or dt.date.today().isoformat(), date[:10]))


def decide(report: dict | None, api_error_streak: int) -> str:
    """continue | wait_reset | wait_api | stop"""
    if report is None:
        return "stop"
    reason = report.get("stopped_reason")
    if reason == "usage_limit":
        return "wait_reset"
    if reason == "api_errors":
        return "wait_api" if api_error_streak < MAX_API_ERROR_RETRIES else "stop"
    if reason in CONTINUE_REASONS:
        # a run that got through nothing without hitting a limit would loop forever
        return "continue" if int((report.get("counts") or {}).get("processed", 0)) > 0 else "stop"
    return "stop"


def retryable_left(root: Path, date: str, max_attempts: int) -> int:
    """Rows the driver would still work on: all queue files up to and including `date`."""
    left = 0
    for qf in sorted(glob.glob(str(root / "state" / "queue" / "*.jsonl"))):
        qd = Path(qf).stem
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[b-z]?", qd) and qd <= date:
            left += sum(1 for r in rn.read_queue(Path(qf)) if rn.is_retryable(r, max_attempts))
    return left


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def lock_holder_alive(root: Path) -> bool:
    lock = root / "state" / "nightly.lock"
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        pid = None
    if pid and _pid_alive(pid):
        return True
    try:
        lock.unlink()  # holder is gone (killed or crashed): the next run would refuse to start
    except OSError:
        pass
    return False


STOP_FLAG = Path("state") / "stop.flag"  # run_nightly finishes its current company and stops when it exists
STOP_GRACE_S = 20 * 60                    # then a hard stop, if one company is still running after this


def stop_time(start: dt.datetime, hhmm: str) -> dt.datetime:
    """Next occurrence of HH:MM after `start` (a run started at 20:00 with 08:00 stops the next morning)."""
    h, m = (int(x) for x in hhmm.split(":"))
    t = start.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > start else t + dt.timedelta(days=1)


def latest_batch(root: Path, today: str) -> str:
    """Newest queue batch id (or today): with it as --date every earlier queue is drained as backlog."""
    ids = [Path(q).stem for q in glob.glob(str(root / "state" / "queue" / "*.jsonl"))]
    return max([i for i in ids if re.fullmatch(r"\d{4}-\d{2}-\d{2}[b-z]?", i)] + [today])


def report_files(root: Path, date: str) -> set[str]:
    return {p for p in glob.glob(str(root / "runs" / f"{date}*.json"))
            if re.fullmatch(rf"{re.escape(date)}(-\d+)?\.json", Path(p).name)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Resume the nightly run until the queue is exhausted")
    ap.add_argument("--date", help="queue batch to drain, with every earlier one (default: the newest batch)")
    ap.add_argument("--cap", type=int, default=1000, help="per-run cap passed to nightly.ps1")
    ap.add_argument("--max-hours", type=float, default=20, help="give up after this long")
    ap.add_argument("--poll-min", type=float, default=30, help="wait when Claude names no reset time")
    ap.add_argument("--stop-at", help="HH:MM local: finish the current company and stop at this time "
                                      "(e.g. 08:00, to keep the run inside the night)")
    ap.add_argument("--refill", action="store_true",
                    help="when the queue is empty, pull the next batch (/source under the next date) "
                         "until the Apollo search has no new companies; costs search credits")
    a = ap.parse_args(argv)
    a.date = a.date or latest_batch(ROOT, dt.date.today().isoformat())

    settings = rn.load_settings(ROOT)
    max_attempts = int(settings["max_attempts"])
    logf = ROOT / "runs" / f"{a.date}-until-done.log"
    logf.parent.mkdir(parents=True, exist_ok=True)

    def say(msg: str) -> None:
        line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line, flush=True)
        with open(logf, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def sleep_until(t_end: float) -> None:
        while time.time() < t_end:
            time.sleep(min(60.0, max(0.0, t_end - time.time())))

    give_up_at = time.time() + a.max_hours * 3600
    stop_at = stop_time(dt.datetime.now(), a.stop_at).timestamp() if a.stop_at else None
    if stop_at:
        give_up_at = min(give_up_at, stop_at)
    flag = ROOT / STOP_FLAG
    flag.unlink(missing_ok=True)  # a stale flag from an earlier stop must not end this run
    api_error_streak = 0
    date = a.date
    say(f"start: draining queue {date} and earlier (cap {a.cap}/run, max {a.max_hours}h"
        + (f", stop at {dt.datetime.fromtimestamp(stop_at).strftime('%a %H:%M')}" if stop_at else "")
        + (", refill: pull the next batch when the queue is empty)" if a.refill else ")"))
    while True:
        while lock_holder_alive(ROOT):
            if time.time() > give_up_at:
                say("stop: max hours reached while another run was active")
                return 0
            time.sleep(60)
        left = retryable_left(ROOT, date, max_attempts)
        queue_exists = (ROOT / "state" / "queue" / f"{date}.jsonl").exists()
        if left == 0 and queue_exists:
            if not a.refill:
                say("done: no pending or retryable companies left")
                return 0
            date = next_date(date, ROOT)
            queue_exists = False
            say(f"refill: queue empty, pulling the next batch from Apollo as {date}")
        if time.time() > give_up_at:
            say(f"stop: {'stop time' if stop_at and time.time() >= stop_at else 'max hours'} reached "
                f"with {left} companies left")
            return 0

        pull = a.refill and not queue_exists  # this run calls /source for `date`
        before = report_files(ROOT, date)
        say(f"run: {left} companies left" + (f", plus a new pull for {date}" if pull else ""))
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(ROOT / "scripts" / "nightly.ps1"), "-Date", date, "-Cap", str(a.cap)]
        if not a.refill:
            cmd.append("-Resume")
        proc = subprocess.Popen(cmd, cwd=str(ROOT))
        asked_at = None
        while proc.poll() is None:
            if stop_at and time.time() >= stop_at and asked_at is None:
                flag.touch()
                asked_at = time.time()
                say("stop time: the run finishes its current company, then stops")
            if asked_at and time.time() - asked_at > STOP_GRACE_S:
                say("stop time: the run did not stop in time, ending it (its company is redone next time)")
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
                break
            time.sleep(15)
        rc = proc.wait()
        new = sorted(report_files(ROOT, date) - before, key=lambda p: Path(p).stat().st_mtime)
        report = json.loads(Path(new[-1]).read_text(encoding="utf-8")) if new else None
        reason = (report or {}).get("stopped_reason")
        processed = ((report or {}).get("counts") or {}).get("processed", 0)
        say(f"run ended: rc={rc} stopped={reason or 'completed'} processed={processed}")

        if pull:
            src = (report or {}).get("source") or {}
            status = (src.get("summary") or {}).get("status")
            if status == "empty":
                say("done: the Apollo search has no new companies left")
                return 0
            if not (ROOT / "state" / "queue" / f"{date}.jsonl").exists() \
                    and reason not in ("usage_limit", "api_errors"):
                say(f"stop: the pull for {date} failed ({src.get('kind')}, status={status}); needs a human")
                return 1

        if asked_at or (stop_at and time.time() >= stop_at):
            flag.unlink(missing_ok=True)
            say(f"stop: stop time reached, {retryable_left(ROOT, date, max_attempts)} companies left for next time")
            return 0

        action = decide(report, api_error_streak)
        api_error_streak = api_error_streak + 1 if action == "wait_api" else 0
        if action == "stop":
            say(f"stop: {reason or 'no report written'} needs a human "
                f"({(report or {}).get('stopped_detail', '')[:200]})")
            return 1
        if action == "wait_reset":
            wait = seconds_until_reset((report or {}).get("stopped_detail", ""), dt.datetime.now())
            wait = a.poll_min * 60 if wait is None else wait + RESET_BUFFER_S
            say(f"usage limit: sleeping {wait / 60:.0f} min "
                f"(until {(dt.datetime.now() + dt.timedelta(seconds=wait)).strftime('%H:%M')})")
            sleep_until(min(time.time() + wait, give_up_at))
        elif action == "wait_api":
            say(f"API errors: sleeping {a.poll_min:.0f} min before retry {api_error_streak}/{MAX_API_ERROR_RETRIES}")
            sleep_until(min(time.time() + a.poll_min * 60, give_up_at))


if __name__ == "__main__":
    sys.exit(main())
