"""Crawl signals for scripts/source_pack.py, loaded from config/signals.json.

Everything that decides WHICH pages and job postings matter lives in that file:
the crawler has no targeting of its own. Replace the shipped sample values with
terms that fit your own qualification prompt.

Kinds of entries:
- *_terms and *_patterns: regular-expression fragments (case-insensitive).
- *_tokens, *_paths and *_subdomains: plain words, matched literally.

  python scripts/signals.py [--signals config/signals.json]    validate and print a summary

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SIGNALS_JSON = ROOT / "config" / "signals.json"

# key -> True when the list must not be empty
TOP_LISTS = {
    "topic_terms": True,
    "role_terms": True,
    "path_tokens": True,
    "priority_paths": True,
    "secondary_paths": True,
    "skip_patterns": False,
    "extra_allow_patterns": False,
    "extra_sitemap_patterns": False,
}
MODE_LISTS = {
    "probe_subdomains": False,
    "seed_subdomains": False,
    "seed_paths": True,
    "sitemap_subdomains": False,
    "skip_patterns": False,
    "stealth_paths": False,
}
MODES = ("light", "full")
REGEX_KEYS = ("topic_terms", "role_terms", "skip_patterns", "extra_allow_patterns", "extra_sitemap_patterns")


class SignalsError(ValueError):
    """config/signals.json is missing, unreadable or malformed."""


def _check_list(where: str, value, non_empty: bool, regex: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise SignalsError(f"{where} must be a list of strings")
    if non_empty and not value:
        raise SignalsError(f"{where} must not be empty")
    if any(not x.strip() for x in value if where.rsplit(".", 1)[-1] != "stealth_paths"):
        raise SignalsError(f"{where} contains a blank entry")
    if regex:
        for x in value:
            try:
                re.compile(x)
            except re.error as e:
                raise SignalsError(f"{where}: {x!r} is not a valid regular expression ({e})") from e
    return list(value)


def load_signals(path=None) -> dict:
    """Read and validate the signals file. Raises SignalsError with the file name and the reason."""
    p = Path(path) if path else SIGNALS_JSON
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise SignalsError(f"signals file not readable: {p} ({e.strerror or e})") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SignalsError(f"signals file is not valid JSON: {p} ({e})") from e
    if not isinstance(data, dict):
        raise SignalsError(f"signals file must hold a JSON object: {p}")
    out = {}
    for key, non_empty in TOP_LISTS.items():
        if key not in data:
            raise SignalsError(f"signals file {p}: missing key {key!r}")
        out[key] = _check_list(key, data[key], non_empty, key in REGEX_KEYS)
    for mode in MODES:
        block = data.get(mode)
        if not isinstance(block, dict):
            raise SignalsError(f"signals file {p}: missing block {mode!r}")
        out[mode] = {}
        for key, non_empty in MODE_LISTS.items():
            if key not in block:
                raise SignalsError(f"signals file {p}: missing key {mode}.{key}")
            out[mode][key] = _check_list(f"{mode}.{key}", block[key], non_empty, key in REGEX_KEYS)
    return out


def _words(words: list[str]) -> str:
    return "|".join(re.escape(w) for w in words)


def build(signals: dict, light: bool, base_deny: list[str]) -> SimpleNamespace:
    """Compile the patterns the crawler uses. `base_deny` is the crawler's own technical skip list."""
    mode = signals["light" if light else "full"]
    tok = _words(signals["path_tokens"])
    # A URL qualifies if any path segment contains one of the tokens as a whole
    # hyphen/underscore-separated word (so "store-locator" matches "locator", "relocators" does not).
    allow = [rf"/(?:[^/]*(?:^|[-_]))?(?:{tok})(?:(?:[-_])[^/]*)?(?:/|$)"] + list(signals["extra_allow_patterns"])
    deny = list(base_deny) + list(signals["skip_patterns"]) + list(mode["skip_patterns"])
    pick = "|".join([rf"(?:^|[-_/])(?:{tok})(?:[-_/]|$)"] + list(signals["extra_sitemap_patterns"]))
    return SimpleNamespace(
        allow=allow,
        deny=deny,
        deny_re=re.compile("|".join(deny)),
        sitemap_pick=re.compile(pick, re.I),
        topic_terms=re.compile(r"\b(" + "|".join(signals["topic_terms"]) + r")\b", re.I),
        role_terms=re.compile(r"\b(" + "|".join(signals["role_terms"]) + r")\b", re.I),
        priority=re.compile(rf"(?:^|[-_/])(?:{_words(signals['priority_paths'])})(?:[-_/]|$)", re.I),
        secondary=re.compile(rf"(?:^|[-_/])(?:{_words(signals['secondary_paths'])})(?:[-_/]|$)", re.I),
        probe_subdomains=tuple(mode["probe_subdomains"]),
        seed_subdomains=tuple(mode["seed_subdomains"]),
        seed_paths=tuple(mode["seed_paths"]),
        sitemap_subdomains=tuple(mode["sitemap_subdomains"]),
        stealth_paths=tuple(mode["stealth_paths"]),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--signals", default=str(SIGNALS_JSON))
    a = ap.parse_args(argv)
    try:
        s = load_signals(a.signals)
    except SignalsError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps({"ok": True, "counts": {k: len(v) for k, v in s.items() if isinstance(v, list)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
