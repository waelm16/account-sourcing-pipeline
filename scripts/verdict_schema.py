"""JSON Schema of the /qualify verdict, generated from the mappings in hubspot_writer.py.

  python scripts/verdict_schema.py            print the schema
  python scripts/verdict_schema.py --write    write prompts/p1_verdict.schema.json

The schema describes what the model is ASKED to return, so the verdict is binary
(the parser still accepts a stray MAYBE in order to downgrade it). The driver uses
the file only when `qualify_json_schema` is set in config/settings.json; by default
the prose report plus the final JSON line is used. tests/test_contracts.py checks
the shipped file against the contract and against this generator.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hubspot_writer as hw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_JSON = ROOT / "prompts" / "p1_verdict.schema.json"
ASKED_QUALIFICATIONS = ["QUALIFIED", "NOT_QUALIFIED"]


def build_schema() -> dict:
    props: dict = {
        "domain": {"type": "string"},
        "name": {"type": "string"},
        "status": {"type": "string", "enum": ["ok", "error"]},
        "qualification": {"type": "string", "enum": ASKED_QUALIFICATIONS},
        "buyer_or_vendor": {"type": "string", "enum": sorted(hw.BUYER_OR_VENDOR)},
        "buyer_vendor_reason": {"type": "string"},
    }
    for key, (_, mapping) in hw.ENUM_FIELDS.items():
        props[key] = {"type": "string", "enum": list(mapping)}
    for key in hw.TEXT_FIELDS:
        props[key] = {"type": "string"}
    props["evidence_links"] = {"type": "array", "items": {"type": "string"}, "maxItems": 8}
    missing = set(hw.REQUIRED_KEYS) - set(props)
    if missing:
        raise RuntimeError(f"schema generator does not cover: {sorted(missing)}")
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Prompt 1 verdict",
        "type": "object",
        "additionalProperties": False,
        "required": list(hw.REQUIRED_KEYS),
        "properties": props,
    }


def render() -> str:
    return json.dumps(build_schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="write prompts/p1_verdict.schema.json")
    a = ap.parse_args(argv)
    if a.write:
        SCHEMA_JSON.write_bytes(render().encode("utf-8"))
        print(f"wrote {SCHEMA_JSON.relative_to(ROOT).as_posix()}")
    else:
        sys.stdout.buffer.write(render().encode("utf-8"))   # bytes: same LF text on every platform
    return 0


if __name__ == "__main__":
    sys.exit(main())
