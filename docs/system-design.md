# System design

This is the longer version of the design section in the README. It describes the architecture, the contracts between the parts, and the rules each part follows. The code holds no targeting: the qualification criteria live in `prompts/prompt1.md`, the search in `config/search.json` and the crawl signals in `config/signals.json`. All three ship as samples for a fictional company.

## 1. Goal and hard rules

Every night, unattended: pull net-new companies from a saved Apollo search, register each pulled company so it never resurfaces, qualify each company for outbound sales one at a time, write only the qualified ones to the CRM, and leave a report.

The rules the design is built around:

1. **Unattended.** No step may wait for a person. Failures are logged and skipped, never retried in a loop that burns usage.
2. **Idempotent.** Running the same night twice must not pull, qualify or write anything twice.
3. **One company per LLM call.** Never batch.
4. **Predictable cost.** Every spend has a ceiling set in advance by the account owner: Apollo credits per run, companies per run, minutes per run. How the LLM is billed is a setting (section 10).
5. **Stop cleanly.** On a usage limit, a deadline or a run of errors, stop and record where, so the next run resumes.
6. **Apollo lists are the dedupe register. The CRM holds only qualified accounts.**
7. **The prompt is a file.** `prompts/prompt1.md` can be edited without touching code, as long as the output contract holds.
8. **A refusal is an answer.** A site that refuses the crawler, or whose robots.txt does not allow it, is recorded and left alone.

## 2. Components

| Component | Kind | Responsibility |
|---|---|---|
| `scripts/nightly.ps1` | PowerShell | Task Scheduler entry point. Cleans the environment, sets UTF-8, appends all output to `runs/<date>.log` |
| `scripts/run_nightly.py` | Python | The driver. Control flow, subprocesses, timeouts, verdict parsing, stop conditions, report |
| `scripts/source_queue.py` | Python | Deterministic half of the Apollo steps: row format check, selection, dedupe, pre-filter, queue file, result mapping |
| `scripts/apollo_url.py` | Python, stdlib only | Apollo web URL to search parameters, acknowledgement of untranslatable filters, readiness check |
| `scripts/source_pack.py` | Python + Scrapling | Crawl of one company's public site into markdown. No LLM |
| `scripts/signals.py` | Python, stdlib only | Loads and validates `config/signals.json`, builds the crawler's patterns |
| `scripts/hubspot_writer.py` | Python + HubSpot client | The only CRM writer. Validation, mapping, lookup, upsert rules |
| `scripts/verdict_schema.py` | Python | Generates the JSON Schema of the verdict from the contract |
| `scripts/enrich.py` | Python | Decides which pushed records still need employee count and industry, and writes them |
| `scripts/run_until_done.py` | Python | Optional loop that resumes the driver until the queue is empty |
| `scripts/import_csv.py`, `scripts/prioritize.py` | Python | Queue a CSV export as a batch; move matching companies to the front |
| `.claude/skills/source` | Claude Code skill | Apollo search, transcription of rows, registration |
| `.claude/skills/qualify` | Claude Code skill | Prompt 1 for one company. Read-only |
| `.claude/skills/enrich`, `register-results` | Claude Code skills | Optional Apollo steps after the loop |
| `.claude/skills/search-config`, `morning` | Claude Code skills | Interactive: set up the search; read last night's report |

The skills that run headless end their answer with a single line of JSON. The driver parses the last line of the result that starts with `{`.

## 3. The search definition

The search is built and sized in Apollo's web UI. `apollo_url.py parse` translates the URL's query string into the parameter names of the connector's company search tool.

- Known parameters map to a filter (`PARAM_MAP`). Repeated `[]` parameters become lists, `[min]` and `[max]` become ranges, and the UI's double URL-encoding is decoded.
- UI state such as paging and sorting is ignored.
- Anything that cannot be translated is listed under `unmapped` with a reason and a fallback. Nothing is dropped silently.
- The Net New flag is recorded as handled: `/source` keeps only the net-new part of the results.

`apollo_url.py resolve` decides whether the unattended search may run (`validate_search`). It refuses when:

- `source_url` starts with `EXAMPLE` (the shipped sample);
- an `unmapped` filter has not been acknowledged by name. Otherwise the search would silently run broader than the one that was built;
- a required base filter is missing (`apollo.required_filters` in settings: an industry code, headcount ranges, locations);
- list mode is set without a list id.

A new URL resets every acknowledgement. Relative dates such as `30_days_ago` are resolved against the run date.

There are two modes. `net_new_search` runs the search and keeps the net-new part. `list` reads a saved accounts list; in that mode there is no Net New and dedupe comes from the local queue history only.

## 4. Pull and register (`/source`)

The skill runs in its own headless session with Apollo tools and the repo's own scripts, and nothing else (section 10). Its steps:

1. If today's queue exists, report `exists` and make no Apollo call.
2. If `candidates.json` exists, skip straight to registration. Never search twice for the same date.
3. Validate the search with `apollo_url.py resolve`. On failure, stop with an error line and make no Apollo call.
4. Optionally list the companies that carry an excluded keyword tag (a free lookup) into `exclude.jsonl`.
5. Search page by page. After each page, write the rows to `page-<n>.jsonl` and run `source_queue.py select`. Stop paging when enough companies are pending, when the page ceiling is reached, or when the results run out.
6. Register every candidate in Apollo (bulk create with dedupe), add them to the dated list, and write `registered.json`.
7. Run `source_queue.py build`, which writes the queue file.

### Credit spend is authorised in advance

Apollo's tools ask for the account owner's confirmation before a call that spends credits. Nobody is present at night, so the owner gives that confirmation in advance and in writing, as a ceiling in `config/settings.json`:

- `apollo.max_search_pages`: search credits per run date. Counted from the page files on disk, so a resumed run cannot exceed it.
- `apollo.max_enrich_per_call`: enrichment credits per `/enrich` call. `enrich.py todo` returns at most that many domains.

The skills read the ceiling from the settings and treat it as the confirmation the tool asks for. A missing or zero ceiling means no authorisation and no call. No other credit-spending tool is authorised or allowed.

### What Python does with the rows (`source_queue.py`)

The LLM transcribes. Python decides.

- **Row format check** (`row_problem`): an Apollo id that is not 24 hex characters, a domain that does not normalise, or a row with neither name nor domain is rejected and reported back to the skill, which may rewrite that page file once. The check is about format. A well-formed but wrong domain passes it.
- **Domain normalisation:** scheme, `www.`, port, path and credentials are stripped.
- **Selection** (`select`): rows are walked in search order until the cap is reached. Duplicates within the run are dropped. Domains seen in any earlier queue file are reported as `resurfaced` and not queued.
- **Pre-filter:** a positive match on the name or industry regex, a keyword-tag exclusion, a missing domain, or (for CSV imports) a parent company marks the row `skipped_prefilter`. Pre-filtered rows do not count toward the cap, and they are still registered.
- **Scope warnings:** a country or headcount outside the configured range produces a warning on the row. It never excludes.
- **Registration matching** (`_match_registered`): account ids are matched by organisation id, then by domain. A match by name alone is accepted only when one side has no domain, because the connector's dedupe also matches by name and can return a different company. Such a case is reported as `dedupe_name_collision`.
- **Queue build** (`cmd_build`): written atomically. If the file already exists the command reports `exists` and changes nothing, because the driver may already have updated statuses.

## 5. The source pack (`source_pack.py --light`)

A small crawl that gives the qualification session the company's own pages, so it spends its budget on judgement and not on fetching. The script is a set of functions with a `main()`; the functions that rank pages, build the index and write the job listing are tested with local files.

- **Size.** At most 15 pages, the seeds listed in the signals file, 4 requests at a time with a delay, and a wall-clock cap of 90 seconds.
- **Signals come from a file.** `config/signals.json` lists the topic terms counted per page, the role terms that put a job posting first, the path tokens a link must contain to be followed, the paths ranked first, the seeds, and the skip patterns. `scripts/signals.py` validates it. A missing or malformed file stops the crawler with exit code 2 before its first request. The crawler's own skip list is technical only: query strings, archives, files, account pages.
- **No crawl under a placeholder name.** While `crawler.user_agent` still holds the shipped placeholder, and impersonation is off, the crawler exits with code 2 before its first request. The driver checks the same thing once per run, crawls no site, and says so in the report.
- **Client identity.** With `crawler.impersonate_browser` false (the shipped value), every request is made with `impersonate=None`, `stealthy_headers=False` and a `User-Agent` header holding `crawler.user_agent`. The same options go to the spider's session. With it true, no option is passed and the fetch library's defaults apply, which present the client as a browser.
- **robots.txt.** One `Robots` object reads robots.txt once per host, with the same fetch function and options as every other request, and is consulted before the homepage probe, each sitemap file and nested sitemap, each seed, each job-board feed and each stealth fetch. The spider consults robots.txt for every page it follows; the script replaces the library's robots manager with a subclass so that the spider uses the same user agent and the same rules. The agent matched is `crawler.user_agent`, or `*` when posing as a browser.
  - 200: the file applies.
  - 401, 403, a server error, or no answer: nothing is allowed.
  - any other status, for example 404: there is no robots.txt, everything is allowed.
- **A refused site is recorded and skipped.** A homepage that answers 401, 403, 407, 429 or 503, or whose body carries a bot-challenge marker, is a refusal. The crawler writes `status.json` with `blocked` and the reason, writes empty pack files, and makes no further request for that company. `disallowed` is written when robots.txt does not allow the homepage, `unreachable` when the homepage gives no answer, `no_host` when no host of the domain resolves. The driver copies anything other than `ok` into the report row.
- **Stealth fallback, off by default.** Only when `crawler.stealth_fallback` is `true` is a refused homepage fetched again with a stealth browser built to pass bot challenges, for the paths in `stealth_paths`, each after a robots.txt check and within a fixed time budget. If that returns no page, the site is recorded as blocked.
- **Sitemaps** are read with a short time budget, and candidate URLs are ranked: `priority_paths` first, then `secondary_paths`, then the rest, shorter paths first.
- **Job feeds.** Lever, Greenhouse and Ashby. Slugs found as links on the site are trusted. A slug guessed from the domain is labelled `guessed`, and the skill is told to use it only if the postings clearly describe this company. No slug is guessed when the site shows another job system.
- `index.txt` lists only usable pages, one per line: `url | title | topic-terms | words`, pages with topic terms first. Duplicates, error pages, soft 404s and pages under 60 words are dropped and their files deleted.
- `jobs.txt` is a compact listing: roles matching `role_terms` first, then postings with topic terms. The raw feed `jobs.json` is kept on disk but the session is told never to read it because of its size.
- Exit code 0 even when nothing was fetched. The caller decides what an empty pack means. The driver treats a failed or timed-out crawl as non-fatal: qualification continues with web search only.

## 6. Qualification (`/qualify`)

Inputs, in order: the prompt file, the company's queue row (`apollo.json`), the source pack index, the job listing, and the crawl status.

Budget, stated in the skill: at most 6 web searches, 8 remote page reads and 25 tool calls in total. When a limit is hit, the session writes its verdict with what it has and marks the gaps.

The session is read-only. The driver passes these flags:

```
claude -p '/qualify <domain> "<name>"' --model <model> --output-format json
       --permission-mode dontAsk
       --strict-mcp-config --mcp-config state/mcp-qualify.json
       --tools Read,Glob,Grep,WebSearch
       --allowedTools Read,Glob,Grep,WebSearch
       --disallowedTools <Apollo, every other connector, the page fetcher>
       --max-turns <n>
```

`state/mcp-qualify.json` is generated before each call. By default it lists no server and the session has no page-fetch tool: it works from the source pack and web search. With `crawler.session_page_fetch` set to `true` the file lists the page fetcher and four fetch tools are added to `--allowedTools`. The stealth fetch tool is added only when `crawler.stealth_fallback` is `true` as well, and is removed otherwise, even if another setting names it. The page-fetch tools belong to the fetch library's own server: they fetch any URL the session asks for and do not consult robots.txt.

### The verdict contract

The last line of the answer is one minified JSON object. Keys and tokens are fixed:

| Key | Allowed values |
|---|---|
| `status` | `ok`, `error` |
| `qualification` | `QUALIFIED`, `NOT_QUALIFIED` (`MAYBE` is accepted and downgraded) |
| `buyer_or_vendor` | `BUYER`, `VENDOR`, `UNCLEAR` |
| `account_tier` | `TIER_A`, `TIER_B`, `TIER_C` |
| `need_strength` | `STRONG`, `MODERATE`, `WEAK`, `NOT_FOUND` |
| `current_approach` | `MANUAL`, `BASIC_TOOLS`, `ADVANCED_SYSTEM`, `UNKNOWN` |
| `timing` | `ACTIVE_TRIGGER`, `NO_TRIGGER`, `UNKNOWN` |
| `evidence_confidence` | `HIGH`, `MEDIUM`, `LOW` |
| `domain`, `name`, `buyer_vendor_reason`, `qualification_reason`, `strongest_signal`, `main_uncertainty`, `roles_to_contact` | text |
| `evidence_links` | list of URLs |

These are the sample's fields. `buyer_or_vendor` is validated and used by the backstop, but not written to the CRM. Every other enumerated field maps to one CRM property.

An error-shaped object (`status: error` plus a reason) is allowed when the session could not research at all.

`tests/contract_spec.py` holds the canonical copy. `tests/test_contracts.py` checks that the prompt footer names every key and token, that the writer's mappings cover exactly those tokens, that the skill's arguments match the driver's invocation, and that `prompts/p1_verdict.schema.json` is exactly what `scripts/verdict_schema.py` generates from the contract.

The schema describes what the model is asked for, so it offers a binary verdict and has no place for the error shape. The driver passes it to the CLI only when `qualify_json_schema` is set in the settings. By default it is not set, and the prose report plus the final JSON line is used.

### How the driver reads the answer

1. **Classify the process result** (`classify_claude`): `ok`, `usage_limit`, `transient`, `timeout` or `error`. A usage limit is recognised only when the call failed, and then from an HTTP 429 status or from a message of the CLI itself: one of a short list of phrases has to start a line. The same words inside a sentence do not count, so a session that fails while quoting a rate-limit error of a website or a connector is an error for that company and not a stop. A successful answer whose prose mentions a rate limit is not a stop either.
2. **Empty result with zero turns** is an error: the skill was not found or was blocked.
3. **Parse** the last JSON line (`parse_verdict`). Missing keys, unknown tokens, values of the wrong type and a non-object all raise an error for this company.
4. **Check the domain.** A verdict about a different domain than the queue row is rejected.
5. **Apply the backstop** (`apply_backstop`): `MAYBE` becomes `NOT_QUALIFIED`; `QUALIFIED` without `BUYER` becomes `NOT_QUALIFIED`; a `NOT_QUALIFIED` verdict is stored as `TIER_C` whatever tier came with it. Each change is noted in the report. `QUALIFIED` with `TIER_C` contradicts itself: it is left as returned and noted, and `hubspot_tiers` decides whether it is written (the shipped setting holds it out of the CRM).
6. **Save** `p1.json` (verdict) and `p1.md` (the prose, with the JSON line removed).

## 7. The CRM write (`hubspot_writer.py`)

`build_properties` is a pure function from verdict and queue row to CRM properties. It raises an error for anything that is not a well-formed qualified buyer verdict. The queue row's domain is authoritative.

The portal id (used for record links in the report) and the owner id (set on new records) are read once at import, from the environment variables `HUBSPOT_PORTAL_ID` and `HUBSPOT_OWNER_ID` or from the `hubspot` block of `config/settings.json`. The shipped values are placeholders. With an empty owner id no owner is set.

`upsert` returns one of these actions:

| Action | Meaning | Writes? |
|---|---|---|
| `created` | no record matched the domain | yes |
| `updated` | one record matched and had no verdict yet | yes, only changed fields |
| `unchanged` | one record matched and nothing differs | no |
| `exists` | the record is already Qualified | no |
| `human_verdict_exists` | the record carries a Needs Review or Not Qualified verdict | no, needs review |
| `conflict` | more than one record shares the domain | no, needs review |
| `invalid` | a value is not in the portal's live option list, or the property has no options | no, needs review |
| `dry_run` | dry run; the lookup still happens, read-only | no |

`plan_update` decides which fields of an existing record may change: the name, domain and website never; the stage only from empty or "Sourced" to "Qualified"; source, employee count and owner only when empty.

`check_enums` fails closed: a value that is not among the live options of its property is invalid, and so is any value of a property whose option list is empty or missing.

The driver can also hold qualified companies out of the CRM by tier (`hubspot_tiers`). Held companies are appended to `runs/held_qualified.csv`.

If the write raises, the queue line becomes `hubspot_error`. The next run retries the write from the saved verdict without calling the LLM, up to `max_attempts`.

## 8. Queue states

```
                      ┌────────────── usage limit: stays pending ──────────────┐
                      ▼                                                         │
 select ──► pending ──┴─► qualification ──► done                                │
    │                        │                ├─ not qualified, or held by tier
    │                        │                └─ qualified and written to the CRM
    │                        ├──► error            retried until max_attempts, then left for a person
    │                        ├──► hubspot_error    write retried without the LLM
    │                        └──► needs_review     terminal: conflict, invalid, human verdict
    └──► skipped_prefilter                         terminal: registered, never qualified
```

Each run works through, in this order: retryable lines from earlier queue files, oldest first, then today's queue. If the backlog alone fills the cap, `/source` is not called that night.

## 9. After the loop

- **`/enrich`** (when `apollo.enrich_pushed` is on): for records this pipeline pushed that lack employee count or industry. One Apollo credit per matched company, at most `apollo.max_enrich_per_call` per call. A domain is enriched at most once, and "not found" is final too, so a credit is never spent twice on the same company. Existing CRM values are never overwritten. Industry is written only when it maps exactly to one of the CRM's options.
- **`/register-results`** (when `register_results` is on): writes each verdict back to Apollo, as a custom account field if it exists, otherwise as dated result lists. It never creates the field. Written items are recorded in `results_registered.jsonl`, so a rerun writes nothing twice.
- **Report.** `runs/<date>.md` and `.json`: counts, how the run ended, a "Needs review" section, and one row per company. A same-day rerun writes `<date>-2`. A dry run writes `<date>-dry-run`. `docs/sample-morning-report.md` is a generated example.

Driver exit codes: 0 for a completed or cleanly stopped run, 1 for a failed pull with no backlog, an unavailable CRM or a crash, 2 for a setting with an unknown value, 3 when the billing guard refuses, 4 when another run holds the lock.

## 10. Security and billing model

### Tools per session

| Session | Reads untrusted content | Tools | Secrets in environment |
|---|---|---|---|
| `/qualify` | yes, search results and the source pack | file read (not limited to the repository), web search. Page fetch only with `crawler.session_page_fetch` | none of the CRM's. API credentials only in `api_key` mode |
| `/source`, `/register-results` | Apollo rows | named Apollo tools, `apollo_url.py`, `source_queue.py`, writes under `state/` | the same |
| `/enrich` | Apollo rows | the same, plus the Apollo enrichment tool and `enrich.py` | the same. `enrich.py`, started by the session, reads the HubSpot token from the keyring |
| Python driver | verdict JSON, validated | HubSpot API | the HubSpot token |

- The session that reads the open web cannot write files, run commands, or reach Apollo or the CRM. Apollo and the other connectors are kept out twice: by the strict MCP configuration, and by name in `--disallowedTools`. Its file reads are not limited to the repository, which together with web search is a possible leak path after a successful prompt injection.
- CRM writes come from two places: the driver (create and update of qualified accounts) and `enrich.py apply`, which the `/enrich` session starts. The second writes `numberofemployees` and `industry` only, only when empty, only on records the pipeline pushed, and `industry` only on an exact match with one of the portal's options.
- The sessions that can write to Apollo cannot search the web, fetch pages or run the crawler. The driver passes `--disallowedTools` with web search, web fetch, the page-fetch server, the crawler command and every other connector. The project list grants none of them either.
- The CRM token is never passed to a session. It is removed from the environment of every child, in every billing mode. `enrich.py` reads it from the keyring, which any process of the same Windows user can do.
- The project allowlist names each Apollo tool one by one and enables no MCP server for every session.
- Credit-spending calls have explicit ceilings, set by the account owner in the settings.

`tests/test_contracts.py` computes the tool list of each session type from the project list and the driver's flags and compares it with expected lists written in the test file. It also shows that adding web or connector tools to the project list would not reach an Apollo session, because the driver's own flags refuse them. These tests check configuration. The behaviour of the real CLI is checked by hand (section E of `tests/LIVE_CHECKLIST.md`).

### Billing mode

`claude_auth` in `config/settings.json`:

| Value | Environment of child processes | Start |
|---|---|---|
| `subscription` (default) | `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` removed | refused when an `apiKeyHelper` is configured in a Claude settings file (exit code 3) |
| `api_key` | both passed through | never refused for billing reasons |
| anything else | | refused with `bad_settings` (exit code 2) |

`scripts/nightly.ps1` reads the same setting and removes the API credentials from its own environment only in subscription mode. Subscription mode suits one person running the tool for themselves. A company would use `api_key` or a team plan. Following the terms of each connected service is the operator's responsibility.

## 11. Testing

The suite runs offline.

- **Stub CLI.** `SOURCING_CLAUDE_BIN` points the driver at `tests/stubs/fake_claude.py`, which plays back a scenario: a good verdict, a usage limit, a crash, garbage, a wrong domain, a timeout, an empty result. It also records its arguments and which credentials were present in its environment.
- **Stub crawler.** `SOURCING_SOURCE_PACK_CMD` replaces the crawl in the driver tests.
- **Crawler functions.** `tests/test_source_pack_units.py` tests page ranking, the index builder, the job listing, the robots.txt rules and the client options, with HTML pages and a job feed stored in `tests/fixtures/`.
- **Offline crawler harness.** `tests/stubs/run_source_pack_offline.py` runs the real `source_pack.py` with name resolution, the fetchers and the spider's start replaced by a scenario file. It logs every request with its options, which is how the tests show that robots.txt precedes every request and which identity each request carries.
- **Fake CRM.** `tests/fakes.py` is a duck-typed client that records calls.
- **Temp root.** `SOURCING_ROOT` or `--root` moves all state into a temporary folder.
- **Replay.** `tests/fixtures/claude_result_sample.json` is a complete CLI result object for a fictional company and is run through the whole driver.
- **Generated sample report.** `tests/sample_report.py` runs the driver on a fictional night. A test fails when `docs/sample-morning-report.md` or its copy in the README differs from the output.
- **Contract tests** compare the prompt, the skills, the settings, the tool lists, the parser, the writer and the schema against each other.

What the suite does not cover is listed in `tests/LIVE_CHECKLIST.md`: the connector calls, the scheduled task, real CRM writes, what the real CLI does with the tool flags, and the crawler against real sites.
