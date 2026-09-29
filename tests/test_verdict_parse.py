"""parse_verdict(result_text): final-line JSON of /qualify. Anything malformed must fail closed."""
import json

import pytest

import run_nightly as rn
from contract_spec import VERDICT_ENUMS, VERDICT_KEYS, good_verdict


def line(v=None, **over):
    return json.dumps(v if v is not None else good_verdict(**over), ensure_ascii=False)


def assert_valid(v):
    assert VERDICT_KEYS <= set(v), VERDICT_KEYS - set(v)
    for k, allowed in VERDICT_ENUMS.items():
        assert v[k] in allowed, (k, v[k])


def fails_closed(text):
    """Must raise VerdictError, or return something that is not a writable QUALIFIED verdict."""
    try:
        v = rn.parse_verdict(text)
    except rn.VerdictError:
        return True
    if v.get("status") == "error":
        return True
    assert_valid(v)  # if accepted it must at least be a valid contract object
    return False


def test_plain_final_line():
    v = rn.parse_verdict("I wrote p1.md.\n\nSummary...\n" + line())
    assert_valid(v)
    assert v["qualification"] == "QUALIFIED"
    assert v["evidence_links"] == ["https://acme.example/careers/1", "https://acme.example/news/ordering"]


def test_trailing_whitespace():
    assert_valid(rn.parse_verdict("prose\n" + line() + "\n\n  \n"))


def test_fenced_json():
    v = rn.parse_verdict("prose\n```json\n" + line() + "\n```\n")
    assert_valid(v)


def test_unicode():
    v = rn.parse_verdict(line(name="Mercado Fictício — Brasil"))
    assert v["name"] == "Mercado Fictício — Brasil"


def test_error_shape():
    v = None
    try:
        v = rn.parse_verdict(json.dumps({"domain": "acme.example", "name": "Acme", "status": "error", "error": "site down"}))
    except rn.VerdictError:
        return
    assert v["status"] == "error"


@pytest.mark.parametrize("text", [
    "",
    "   \n",
    "no json at all",
    "prose\n{\"domain\": \"acme.example\", \"qualification\": ",          # truncated
    "prose\n{'domain': 'acme.example'}",                                     # python repr, not JSON
    "prose\n[1, 2, 3]",                                                  # not an object
    "prose\nnull",
])
def test_malformed_raises(text):
    with pytest.raises(rn.VerdictError):
        rn.parse_verdict(text)


def test_json_followed_by_prose_only_if_fully_valid():
    # Accepting a fully valid verdict followed by chatter is OK; an incomplete one must not be accepted.
    v = good_verdict()
    del v["account_tier"]
    assert fails_closed(json.dumps(v) + "\nHope this helps!")


@pytest.mark.parametrize("key", sorted(VERDICT_KEYS))
def test_missing_key(key):
    v = good_verdict()
    del v[key]
    with pytest.raises(rn.VerdictError):
        rn.parse_verdict(json.dumps(v))


@pytest.mark.parametrize("key,bad", [
    ("qualification", "MAYBE — LEANING YES"),
    ("qualification", "Qualified"),
    ("qualification", "YES"),
    ("account_tier", "Tier A"),
    ("account_tier", "TIER_D"),
    ("need_strength", "VERY_STRONG"),
    ("current_approach", ""),
    ("timing", "SOON"),
    ("evidence_confidence", "high"),
    ("buyer_or_vendor", "SELLER"),
])
def test_bad_enum(key, bad):
    with pytest.raises(rn.VerdictError):
        rn.parse_verdict(line(**{key: bad}))


def test_lowercase_enum_fails_closed_or_normalises():
    text = line(qualification="qualified", need_strength="strong")
    try:
        v = rn.parse_verdict(text)
    except rn.VerdictError:
        return
    assert_valid(v)


def test_evidence_links_must_be_list():
    try:
        v = rn.parse_verdict(line(evidence_links="https://a.example | https://b.example"))
    except rn.VerdictError:
        return
    assert isinstance(v["evidence_links"], list)


def test_qualified_vendor_downgraded():
    """Invariant: QUALIFIED requires BUYER. Either parse downgrades, or the driver does (checked in driver tests)."""
    try:
        v = rn.parse_verdict(line(buyer_or_vendor="VENDOR"))
    except rn.VerdictError:
        return
    assert v["qualification"] in {"QUALIFIED", "MAYBE"}  # driver-level test enforces the no-write


# ---------------------------------------------------------------- backstop
def backstop(**over):
    return rn.apply_backstop(good_verdict(**over))


def test_backstop_leaves_a_consistent_verdict_alone():
    for tier in ("TIER_A", "TIER_B"):
        v, note = backstop(account_tier=tier)
        assert v == good_verdict(account_tier=tier) and note is None
    v, note = backstop(qualification="NOT_QUALIFIED", account_tier="TIER_C")
    assert v["qualification"] == "NOT_QUALIFIED" and v["account_tier"] == "TIER_C" and note is None


@pytest.mark.parametrize("tier", ["TIER_A", "TIER_B"])
def test_not_qualified_is_always_the_lowest_tier(tier):
    v, note = backstop(qualification="NOT_QUALIFIED", account_tier=tier)
    assert v["qualification"] == "NOT_QUALIFIED" and v["account_tier"] == "TIER_C"
    assert note == f"tier {tier}->TIER_C (not qualified)"


@pytest.mark.parametrize("over,first_note", [
    ({"qualification": "MAYBE"}, "MAYBE->NOT_QUALIFIED (verdicts are binary)"),
    ({"buyer_or_vendor": "VENDOR"}, "downgraded QUALIFIED->NOT_QUALIFIED (buyer_or_vendor=VENDOR)"),
    ({"buyer_or_vendor": "UNCLEAR"}, "downgraded QUALIFIED->NOT_QUALIFIED (buyer_or_vendor=UNCLEAR)"),
])
def test_a_downgrade_also_lowers_the_tier(over, first_note):
    v, note = backstop(account_tier="TIER_A", **over)
    assert v["qualification"] == "NOT_QUALIFIED" and v["account_tier"] == "TIER_C"
    assert note == first_note + "; tier TIER_A->TIER_C (not qualified)"
    v, note = backstop(account_tier="TIER_C", **over)             # already the lowest tier: one note only
    assert v["account_tier"] == "TIER_C" and note == first_note


def test_qualified_with_the_lowest_tier_is_kept_and_noted():
    v, note = backstop(account_tier="TIER_C")
    assert v["qualification"] == "QUALIFIED" and v["account_tier"] == "TIER_C"
    assert note == "QUALIFIED with TIER_C: the verdict contradicts its tier, kept as returned"


def test_backstop_does_not_change_its_input_or_an_error_verdict():
    original = good_verdict(buyer_or_vendor="VENDOR", account_tier="TIER_A")
    rn.apply_backstop(original)
    assert original["qualification"] == "QUALIFIED" and original["account_tier"] == "TIER_A"
    err = {"domain": "a.example", "name": "A", "status": "error", "error": "x"}
    assert rn.apply_backstop(err) == (err, None)


def test_is_usage_limit_matches():
    for msg in ["Claude AI usage limit reached|1900000000", "API Error: rate limit exceeded",
                "You're out of extra usage"]:
        out = json.dumps({"result": msg, "is_error": True})
        assert rn.is_usage_limit(1, out), msg
    assert rn.is_usage_limit(1, "Claude AI usage limit reached")  # non-JSON stdout too


@pytest.mark.parametrize("msg", [
    "Claude AI usage limit reached|1900000000",
    "Claude usage limit reached. Your limit will reset at 1:30am",
    "5-hour limit reached \u2219 resets 6am",
    "You've hit your limit \u00b7 resets 3pm",
    "You\u2019re out of extra usage",
    "Your limit will reset at 1:30am (America/Chicago)",
    "API Error: 429 too many requests",
    "API Error: rate limit exceeded",
    "Error: Claude AI usage limit reached",
    "some log line\nWeekly limit reached\nmore",          # the phrase starts a line further down
])
def test_cli_limit_messages_are_recognised(msg):
    assert rn.is_usage_limit(1, msg)
    r = {"rc": 1, "stdout": "", "stderr": "", "timed_out": False, "json": {"result": msg, "is_error": True}}
    assert rn.classify_claude(r) == "usage_limit"


@pytest.mark.parametrize("msg", [
    "The Apollo connector returned an error: rate limit exceeded, try again later",
    "I could not read the site. It answered 429 with the text 'rate limit reached'.",
    "Search failed: the tool said \"usage limit reached for this key\".",
    "The page says the daily limit reached by the API will reset at 5pm.",
    "HubSpot: you have hit your limit of 100 requests",
    "The store resets at 9 every morning",
    "Tool error (apollo_mixed_companies_search): Rate limit exceeded",
])
def test_quoted_third_party_limit_is_not_a_claude_usage_limit(msg):
    """A failed session whose text quotes someone else's limit is an error for that company, not a stop."""
    assert not rn.is_usage_limit(1, msg)
    r = {"rc": 1, "stdout": "", "stderr": "", "timed_out": False, "json": {"result": msg, "is_error": True}}
    assert rn.classify_claude(r) == "error"
    assert not rn.is_usage_limit(1, json.dumps({"result": msg, "is_error": True}))


def test_http_429_status_is_a_usage_limit_whatever_the_text():
    r = {"rc": 1, "stdout": "", "stderr": "", "timed_out": False,
         "json": {"result": "something went wrong", "is_error": True, "api_error_status": 429}}
    assert rn.classify_claude(r) == "usage_limit"


def test_is_usage_limit_negative():
    assert not rn.is_usage_limit(0, json.dumps({"result": "ok " + line(), "is_error": False}))
    assert not rn.is_usage_limit(1, json.dumps({"result": "Error: tool failed", "is_error": True}))


def test_usage_words_in_prose_on_success_not_a_stop():
    """A successful P1 whose prose mentions 'rate limit' (e.g. about the company's API) must not stop the run."""
    res = "The company's API docs mention a rate limit of 100 rps.\n" + line()
    r = {"rc": 0, "stdout": "", "stderr": "", "timed_out": False, "json": {"result": res, "is_error": False}}
    assert rn.classify_claude(r) == "ok"


def test_classify_usage_limit():
    r = {"rc": 1, "stdout": "", "stderr": "", "timed_out": False,
         "json": {"result": "Claude AI usage limit reached|1900000000", "is_error": True}}
    assert rn.classify_claude(r) == "usage_limit"
    r = {"rc": 1, "stdout": "", "stderr": "", "timed_out": False, "json": {"is_error": True, "api_error_status": 429}}
    assert rn.classify_claude(r) == "usage_limit"


def test_classify_max_turns_is_error():
    r = {"rc": 1, "stdout": "", "stderr": "", "timed_out": False,
         "json": {"subtype": "error_max_turns", "is_error": True, "result": ""}}
    assert rn.classify_claude(r) == "error"


def test_classify_is_error_with_rc0():
    r = {"rc": 0, "stdout": "", "stderr": "", "timed_out": False, "json": {"is_error": True, "result": "boom"}}
    assert rn.classify_claude(r) != "ok"


def test_classify_non_json_stdout():
    r = {"rc": 0, "stdout": "garbage", "stderr": "", "timed_out": False, "json": None}
    assert rn.classify_claude(r) == "error"


@pytest.mark.parametrize("bad", [["QUALIFIED"], {"x": 1}, 3, None])
def test_unhashable_or_wrong_type_enum(bad):
    with pytest.raises(rn.VerdictError):
        rn.parse_verdict(line(qualification=bad))


@pytest.mark.parametrize("bad", [None, 5, ["a.example"]])
def test_non_string_domain(bad):
    with pytest.raises(rn.VerdictError):
        rn.parse_verdict(line(domain=bad))
