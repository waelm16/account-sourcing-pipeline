# Test plan

Run: `.venv\Scripts\python -m pytest tests -q`. No test touches Apollo, HubSpot, the real `claude` binary or the
network; live steps are in `LIVE_CHECKLIST.md`. Company names and page text in the tests are fictional, domains end
in `.example`, and dates are in 2030 so that nothing can be mistaken for a record of a real run.

| Area | File | What is checked |
|---|---|---|
| Apollo URL parser | `test_apollo_url.py` | each kind of Companies-tab param (list, number range, date range, text range, text, number, flat range) → search parameter; URL-encoded and double-encoded values; UI-only params ignored; unknown params and other tabs land in `unmapped`; the readiness check (example file refused, unacknowledged filters, required base filters, list mode); a new URL resets acknowledgements; `parse --write`, `ack`, `resolve` round trip in a temp folder |
| HubSpot writer | `test_hubspot_writer.py` | every token maps to a CRM option label and every label is in the portal's allowed set; `evidence_links` joined with ` \| `; fixed fields (`sourcing_account_stage`, `sourcing_sourced_from`, `sourcing_p1_date`); portal id and owner id come from the environment or `config/settings.json`, and the shipped values are placeholders; no downgrade of an existing Qualified record; a human verdict is never reversed; duplicate domains are refused; an unknown live option fails closed; search-by-domain → update vs create; dry-run makes no write calls |
| Verdict parsing | `test_verdict_parse.py` | JSON on final line; trailing whitespace / code fences; prose before JSON; malformed JSON; every key missing in turn; bad tokens; wrong types; usage-limit messages of the CLI are recognised, and the same words quoted from a website or a connector are not |
| Driver | `test_run_nightly.py` | stubbed `claude` + source pack: queue resume (only `pending` processed), per-company error isolation, usage-limit stop with `stopped_reason`, report `.md` + `.json` written, idempotent re-run, cap honoured, backlog first, lock, crash still writes a report, tiers held out of the CRM, page-fetch and stealth tools only when switched on, a blocked site named in the report, replay of a complete CLI result (`fixtures/claude_result_sample.json`) |
| Billing mode | `test_run_nightly.py` | `subscription`: API credentials never reach a child, an `apiKeyHelper` stops the run. `api_key`: credentials are passed through and nothing is refused. The CRM token never reaches a child in either mode. An unknown value stops the run |
| Contracts | `test_contracts.py` | prompt footer names every key and token the parser knows; the writer's mappings equal the contract; the shipped JSON Schema equals the generated one; skills' arguments match the driver's invocation; every settings key is read somewhere; the shipped search is the example and is refused; credit spend is worded as an advance authorisation with a ceiling from the settings |
| Tools per session | `test_contracts.py` | the tool list of each session type, derived from the project list and the driver's flags, equals the expected list written in the test file (the README table is kept in step by hand): `/qualify` has file read and web search only; the Apollo sessions have named Apollo tools and the repo's scripts, and no web, no page fetch and no crawler, even if the project list were to grant them |
| Crawl signals | `test_signals.py` | `config/signals.json` loads; a missing, malformed or incomplete file fails with a message that names the file and the reason; tokens match whole words and are taken literally; skip patterns are added to the crawler's technical list |
| Crawler functions | `test_source_pack_units.py` | with HTML pages and a job feed from `tests/fixtures/`: page ranking, seeds and the seed cap, page to index row, duplicates and error pages dropped, job-board links and guessed slugs, job listing order and cap; robots.txt rules per status and per user agent; request options and robots.txt agent in both identity modes; safe defaults for a missing or broken settings file |
| Crawler, offline | `test_source_pack_offline.py` | the real `source_pack.py` with all network calls replaced: robots.txt precedes every request; a site that refuses (401, 403, 407, 429, 503, bot challenge) is recorded as blocked and nothing more is requested; a refused or failing robots.txt allows nothing; the stealth browser is used only when switched on and still obeys robots.txt; every request carries the plain user agent unless browser identity is switched on; seeds, link rules and sitemap ranking come from the signals file; a bad signals file stops the run before any request |
| Sample report | `test_sample_report.py` | `docs/sample-morning-report.md` and its copy in the README equal the output of the report code for the fictional night in `tests/sample_report.py` |
| Source queue | `test_source_queue.py` | domain normalisation, prefilter, cap/resurfaced/invalid ids, queue-line keys, select→build→exists CLI in tmp root, result_value for every status the driver writes, name-collision dedupe |
| Keyword exclusions | `test_exclusions.py` | companies from the keyword-tag lookup are marked `skipped_prefilter` and do not count toward the cap |
| CSV import | `test_import_csv.py` | an Apollo accounts export becomes a queue batch with the same pre-filters; dry run writes nothing; a batch id is never reused |
| Queue priority | `test_prioritize.py` | only pending lines that match the second CSV move to the front |
| Enrichment | `test_enrich.py` | only pushed records with empty fields are enriched, each at most once; existing CRM values are never overwritten; one call never returns more domains than the credit ceiling |
| Resume loop | `test_run_until_done.py` | reset-time parsing, continue / wait / stop decisions, batch ids |
| Crawler, live | `test_source_pack_live.py` (opt-in: `--live-web` plus `SOURCING_LIVE_DOMAIN`) | `source_pack.py <public domain> --light` finishes < 120 s, writes `index.txt` and `jobs.json` |

## What is skipped and why

The only tests skipped in a default run are the 7 in `test_source_pack_live.py`. They fetch a real website, so they
run only with `--live-web` and with `SOURCING_LIVE_DOMAIN` set to a public site you are allowed to crawl.

## Test-only environment variables

These exist for the test suite. Nothing in a real run sets or needs them.

| Variable | Read by | Purpose |
|---|---|---|
| `SOURCING_STUB_SCENARIO` | `tests/stubs/fake_claude.py` | path of the scenario file that says what the fake `claude` answers for `/source` and for each domain |
| `SOURCING_STUB_LOG` | `tests/stubs/fake_claude.py` | path where the fake `claude` records its arguments and whether secrets were in its environment |
| `SOURCING_STUB_SP_FAIL` | `tests/stubs/fake_source_pack.py` | comma-separated domains for which the fake crawler fails |
| `SOURCING_STUB_SP_BLOCKED` | `tests/stubs/fake_source_pack.py` | comma-separated domains whose site "refused" the fake crawler |
| `SOURCING_CLAUDE_BIN` | `scripts/run_nightly.py` | test seam: the program started in place of `claude` |
| `SOURCING_SOURCE_PACK_CMD` | `scripts/run_nightly.py` | test seam: the command started in place of the crawler |
| `SOURCING_ROOT` | `scripts/run_nightly.py`, `scripts/enrich.py` | test seam: the folder that holds `config/`, `state/` and `runs/` (same as `--root`) |
| `SOURCE_PACK_FORCE_STEALTH` | `scripts/source_pack.py` | treats the homepage as refused. It does not switch the stealth option on, so with the shipped settings the site is recorded as blocked |
| `SOURCING_LIVE_DOMAIN` | `tests/test_source_pack_live.py` | the site the opt-in live test crawls |
