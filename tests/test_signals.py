"""signals.py: the crawler's targeting lists come from config/signals.json. No network."""
import json
import re
import subprocess
import sys

import pytest

import signals as sg

BASE_DENY = [r"\?", r"/login"]


def minimal(**over):
    mode = {"probe_subdomains": ["www"], "seed_subdomains": [], "seed_paths": ["about"],
            "sitemap_subdomains": [], "skip_patterns": [], "stealth_paths": [""]}
    cfg = {"topic_terms": ["widgets?", "gear ratio"], "role_terms": ["engineer\\w*"],
           "path_tokens": ["about", "hub"], "priority_paths": ["hub"], "secondary_paths": ["about"],
           "skip_patterns": ["/archive/"], "extra_allow_patterns": [], "extra_sitemap_patterns": [],
           "light": dict(mode), "full": dict(mode, seed_paths=["about", "hub"], skip_patterns=["/old/"])}
    cfg.update(over)
    return cfg


def write(tmp_path, data):
    p = tmp_path / "signals.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------- loading
def test_shipped_file_loads(repo):
    s = sg.load_signals(repo / "config" / "signals.json")
    assert s["topic_terms"] and s["role_terms"] and s["path_tokens"]
    assert s["light"]["seed_paths"] and s["full"]["seed_paths"]
    assert sg.load_signals() == s                      # default path is the repo's config/signals.json


def test_values_come_from_the_file(tmp_path):
    s = sg.load_signals(write(tmp_path, minimal()))
    assert s["topic_terms"] == ["widgets?", "gear ratio"]
    assert s["full"]["seed_paths"] == ["about", "hub"]
    assert "_notes" not in s


def test_missing_file_fails_clearly(tmp_path):
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(tmp_path / "nope.json")
    assert "not readable" in str(e.value) and "nope.json" in str(e.value)


@pytest.mark.parametrize("content,needle", [
    ("", "not valid JSON"),
    ("{not json", "not valid JSON"),
    ("[1, 2]", "must hold a JSON object"),
    ('"text"', "must hold a JSON object"),
])
def test_malformed_file_fails_clearly(tmp_path, content, needle):
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, content))
    assert needle in str(e.value) and "signals.json" in str(e.value)


@pytest.mark.parametrize("key", sorted(sg.TOP_LISTS))
def test_missing_top_level_key(tmp_path, key):
    cfg = minimal()
    del cfg[key]
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, cfg))
    assert repr(key) in str(e.value)


@pytest.mark.parametrize("mode", sg.MODES)
@pytest.mark.parametrize("key", sorted(sg.MODE_LISTS))
def test_missing_mode_key(tmp_path, mode, key):
    cfg = minimal()
    del cfg[mode][key]
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, cfg))
    assert f"{mode}.{key}" in str(e.value)


def test_missing_mode_block(tmp_path):
    cfg = minimal()
    del cfg["light"]
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, cfg))
    assert "'light'" in str(e.value)


@pytest.mark.parametrize("bad", ["forecasting", {"a": 1}, [1, 2], ["ok", None], None])
def test_wrong_type(tmp_path, bad):
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, minimal(topic_terms=bad)))
    assert "topic_terms must be a list of strings" in str(e.value)


@pytest.mark.parametrize("key", [k for k, required in sg.TOP_LISTS.items() if required])
def test_required_list_must_not_be_empty(tmp_path, key):
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, minimal(**{key: []})))
    assert "must not be empty" in str(e.value)


def test_blank_entry_rejected(tmp_path):
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, minimal(path_tokens=["about", " "])))
    assert "blank" in str(e.value)


def test_invalid_regex_rejected(tmp_path):
    with pytest.raises(sg.SignalsError) as e:
        sg.load_signals(write(tmp_path, minimal(topic_terms=["fine", "broken("])))
    assert "broken(" in str(e.value) and "regular expression" in str(e.value)


def test_cli_reports_ok_and_error(tmp_path, repo):
    def cli(path):
        r = subprocess.run([sys.executable, str(repo / "scripts" / "signals.py"), "--signals", str(path)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        return r.returncode, json.loads(r.stdout)
    code, out = cli(write(tmp_path, minimal()))
    assert code == 0 and out["ok"] is True and out["counts"]["topic_terms"] == 2
    code, out = cli(tmp_path / "missing.json")
    assert code == 2 and out["ok"] is False and "not readable" in out["error"]


# ---------------------------------------------------------------- patterns built from the lists
@pytest.fixture
def built(tmp_path):
    return sg.build(sg.load_signals(write(tmp_path, minimal())), light=True, base_deny=BASE_DENY)


def follows(b, url):
    return any(re.search(a, url) for a in b.allow) and not b.deny_re.search(url)


def test_path_token_is_a_whole_word(built):
    assert follows(built, "https://x.example/about")
    assert follows(built, "https://x.example/about/")
    assert follows(built, "https://x.example/help-hub")
    assert follows(built, "https://x.example/hub_pages/one")
    assert follows(built, "https://x.example/en/about-us")
    assert not follows(built, "https://x.example/hubcap")          # token inside a longer word
    assert not follows(built, "https://x.example/roundabout")
    assert not follows(built, "https://x.example/contact")


def test_tokens_are_literal_not_regex(tmp_path):
    b = sg.build(sg.load_signals(write(tmp_path, minimal(path_tokens=["a.b"]))), True, BASE_DENY)
    assert follows(b, "https://x.example/a.b")
    assert not follows(b, "https://x.example/axb")


def test_deny_is_base_plus_configured_skips(tmp_path):
    s = sg.load_signals(write(tmp_path, minimal()))
    light, full = sg.build(s, True, BASE_DENY), sg.build(s, False, BASE_DENY)
    assert light.deny == BASE_DENY + ["/archive/"]
    assert full.deny == BASE_DENY + ["/archive/", "/old/"]
    assert not follows(light, "https://x.example/archive/about")
    assert follows(light, "https://x.example/old/about") and not follows(full, "https://x.example/old/about")
    assert not follows(light, "https://x.example/about?page=2")     # technical skip from the crawler itself
    assert BASE_DENY == [r"\?", r"/login"]                          # the caller's list is not modified


def test_extra_patterns_are_added(tmp_path):
    cfg = minimal(extra_allow_patterns=["/kb/articles/"], extra_sitemap_patterns=["/kb/"])
    b = sg.build(sg.load_signals(write(tmp_path, cfg)), True, BASE_DENY)
    assert follows(b, "https://x.example/kb/articles/42")
    assert b.sitemap_pick.search("/kb/42") and not b.sitemap_pick.search("/faq/42")


def test_sitemap_pick_and_ranking(built):
    assert built.sitemap_pick.search("/company/about-us")
    assert built.sitemap_pick.search("/HUB/")                       # case-insensitive
    assert not built.sitemap_pick.search("/pricing")
    assert built.priority.search("/hub/one") and not built.priority.search("/about")
    assert built.secondary.search("/about") and not built.secondary.search("/hub/one")


def test_topic_and_role_terms(built):
    found = {m.lower() for m in built.topic_terms.findall("Our Widgets have a fixed gear ratio. One widget each.")}
    assert found == {"widgets", "gear ratio", "widget"}
    assert not built.topic_terms.search("midgets and gearratios")    # whole words only
    assert built.role_terms.search("Senior Engineering Manager") and not built.role_terms.search("Cashier")


def test_mode_lists(tmp_path):
    s = sg.load_signals(write(tmp_path, minimal()))
    assert sg.build(s, True, BASE_DENY).seed_paths == ("about",)
    assert sg.build(s, False, BASE_DENY).seed_paths == ("about", "hub")
    assert sg.build(s, True, BASE_DENY).stealth_paths == ("",)


def test_shipped_sample_keeps_store_pages(repo):
    """The grocery sample must crawl store locator pages and skip shop and recipe pages."""
    b = sg.build(sg.load_signals(repo / "config" / "signals.json"), True, BASE_DENY)
    for url in ("https://g.example/stores", "https://g.example/locations/springfield", "https://g.example/store-locator"):
        assert follows(b, url), url
    for url in ("https://g.example/recipes/soup", "https://g.example/weekly-ad", "https://g.example/products/milk"):
        assert not follows(b, url), url
    assert b.priority.search("/stores/12") and b.topic_terms.search("We cut food waste and shrink")
    assert b.role_terms.search("Replenishment Analyst") and not b.role_terms.search("Pharmacist")

