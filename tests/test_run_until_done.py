import datetime as dt
import json

import run_until_done as rud

NOW = dt.datetime(2030, 1, 16, 3, 10)


def test_epoch_reset():
    target = NOW + dt.timedelta(hours=2)
    msg = f"Claude AI usage limit reached|{int(target.timestamp())}"
    assert abs(rud.seconds_until_reset(msg, NOW) - 7200) < 2


def test_clock_reset_later_today():
    assert rud.seconds_until_reset("5-hour limit reached ∙ resets 6am", NOW) == (2 * 60 + 50) * 60


def test_clock_reset_tomorrow_and_minutes():
    s = rud.seconds_until_reset("Your limit will reset at 1:30am (America/Chicago)", NOW)
    assert s == (22 * 60 + 20) * 60


def test_clock_reset_pm_and_noon():
    assert rud.seconds_until_reset("resets 3pm", NOW) == (11 * 60 + 50) * 60
    assert rud.seconds_until_reset("resets 12pm", NOW) == (8 * 60 + 50) * 60


def test_no_reset_time():
    assert rud.seconds_until_reset("rate limit exceeded", NOW) is None
    assert rud.seconds_until_reset("", NOW) is None


def rep(reason, processed=3):
    return {"stopped_reason": reason, "counts": {"processed": processed}}


def test_decide():
    assert rud.decide(rep("usage_limit", 0), 0) == "wait_reset"
    assert rud.decide(rep("deadline"), 0) == "continue"
    assert rud.decide(rep("cap_reached"), 0) == "continue"
    assert rud.decide(rep(None), 0) == "continue"
    assert rud.decide(rep(None, 0), 0) == "stop"          # no progress, no limit: never spin
    assert rud.decide(rep("api_errors"), 0) == "wait_api"
    assert rud.decide(rep("api_errors"), rud.MAX_API_ERROR_RETRIES) == "stop"
    for bad in ("hubspot_unavailable", "consecutive_errors", "driver_crash", "locked", "api_key_guard"):
        assert rud.decide(rep(bad), 0) == "stop"
    assert rud.decide(None, 0) == "stop"


def test_retryable_left(tmp_path):
    q = tmp_path / "state" / "queue"
    q.mkdir(parents=True)
    rows = [{"domain": "a.example", "status": "pending"}, {"domain": "b.example", "status": "done"},
            {"domain": "c.example", "status": "error", "attempts": 1},
            {"domain": "d.example", "status": "error", "attempts": 2},
            {"domain": "e.example", "status": "skipped_prefilter"}]
    (q / "2030-01-17.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    (q / "2030-01-18.jsonl").write_text(json.dumps({"domain": "f.example", "status": "pending"}), encoding="utf-8")
    assert rud.retryable_left(tmp_path, "2030-01-17", 2) == 2  # later dates are not this run's


def test_next_date_is_real_date_plus_letter(tmp_path):
    (tmp_path / "state" / "queue").mkdir(parents=True)
    (tmp_path / "state" / "queue" / "2030-01-16.jsonl").write_text("", encoding="utf-8")
    # same day: a letter, never tomorrow's date
    assert rud.next_date("2030-01-16", tmp_path, today="2030-01-16") == "2030-01-16b"
    (tmp_path / "state" / "source" / "2030-01-16b").mkdir(parents=True)
    assert rud.next_date("2030-01-16b", tmp_path, today="2030-01-16") == "2030-01-16c"
    # after midnight the new real date starts again without a letter
    assert rud.next_date("2030-01-16c", tmp_path, today="2030-01-17") == "2030-01-17"


def test_stop_time_is_next_occurrence():
    start = dt.datetime(2030, 1, 16, 20, 0)
    assert rud.stop_time(start, "08:00") == dt.datetime(2030, 1, 17, 8, 0)
    assert rud.stop_time(dt.datetime(2030, 1, 17, 2, 30), "08:00") == dt.datetime(2030, 1, 17, 8, 0)


def test_latest_batch(tmp_path):
    q = tmp_path / "state" / "queue"
    q.mkdir(parents=True)
    for b in ("2030-01-16", "2030-01-16e", "2030-01-16b", "notes"):
        (q / f"{b}.jsonl").write_text("", encoding="utf-8")
    assert rud.latest_batch(tmp_path, "2030-01-16") == "2030-01-16e"
    assert rud.latest_batch(tmp_path, "2030-01-17") == "2030-01-17"
