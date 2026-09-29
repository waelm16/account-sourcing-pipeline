# Account Sourcing Pipeline

A nightly job that qualifies accounts for outbound sales. It pulls companies the sales team has not seen from a saved Apollo search, decides for each one whether it is worth a salesperson's time, and puts only the qualified ones in the CRM, with the reason, the strongest signal and the roles to contact. Everything it rejects is recorded so it is never pulled again. In the morning there is a report to read.

**Origin.** I built this to run nightly account sourcing at a startup I co-founded. This public version ships with a fictional sample company, a sample qualification prompt, and fake fixtures.

## What it guarantees about the CRM

A sourcing job that writes to the CRM every night can do damage every night. These are the rules the pipeline is built around. Each one is enforced in code, not in a prompt.

| Rule | What it means in practice | Where |
|---|---|---|
| **It looks up by domain before it creates** | No duplicate company records. It matches on the domain, on `www.` plus the domain, and on the website field | `upsert` in `scripts/hubspot_writer.py` |
| **It never reverses a person's verdict** | A record someone marked Not Qualified or Needs Review is left as it is and listed for review. A record that is already Qualified is not touched at all | `upsert`, `plan_update` |
| **It refuses when two records share a domain** | It writes nothing and lists the account for review, because it cannot know which record is the right one | `upsert` |
| **It never pushes a rejected account** | Only a Qualified verdict for a company that can be a buyer reaches the CRM. A qualified vendor is downgraded in code and refused again by the writer | `apply_backstop` in `scripts/run_nightly.py`, `build_properties` |
| **It registers rejects so they are not pulled again** | Every pulled company, qualified or not, is saved to a dated Apollo list, and any domain seen in an earlier night is dropped before qualification | `/source` skill, `select` in `scripts/source_queue.py` |
| **It stops cleanly and resumes** | On a usage limit, a deadline or a run of errors it stops, leaves the unfinished companies pending, and does them first the next night. A second run of the same night writes nothing twice | `scripts/run_nightly.py` |

All but one of these are covered by offline tests against a fake CRM (`tests/test_hubspot_writer.py`, `tests/test_run_nightly.py`, `tests/test_source_queue.py`). The exception is the Apollo half of the fifth rule: saving to the Apollo list happens in a connector session and is checked by hand with `tests/LIVE_CHECKLIST.md`.

The sample company is Tidewater Labs, a fictional startup that sells inventory forecasting software to regional grocery chains. The qualification criteria, the verdict fields, the CRM labels, the search filters and the crawl signals in this repo all belong to that sample. Every company name and page text in `prompts/`, `config/`, `docs/` and `tests/` is made up, and company domains use the reserved `.example` ending. The only real host names in the repo are those of services the code has to recognise or call: Apollo, HubSpot, and the job boards and applicant systems listed in `scripts/source_pack.py`.

## The nightly flow

```
Task Scheduler, 01:00 daily
  └─ scripts/nightly.ps1                        cleans the environment, logs to runs/<date>.log
       └─ python scripts/run_nightly.py
            ├─ 0. guard and lock                 settings check, billing guard, one run at a time
            ├─ 1. leftovers first                pending, failed and half-finished companies from earlier nights
            ├─ 2. claude -p "/source <date> <n>" Apollo search → net-new only → register in Apollo
            │                                    → state/queue/<date>.jsonl
            ├─ 3. CRM preflight                  read-only check that HubSpot is reachable
            ├─ 4. for each company, up to the cap:
            │     a. scripts/source_pack.py <domain> --light   crawl the public site, no LLM
            │     b. claude -p "/qualify <domain> <name>"      one fresh session → report + verdict JSON
            │     c. Python validates the verdict, applies the backstop rules
            │     d. scripts/hubspot_writer.py                 qualified only → lookup by domain → create or update
            │     e. queue file rewritten                      the company's new status is on disk before the next one starts
            ├─ 5. optional Apollo steps          /enrich (firmographics), /register-results (verdicts back to Apollo)
            └─ 6. runs/<date>.md + runs/<date>.json            the morning report
```

This public version ships with stealth retry, browser impersonation and session page fetching turned off, and it refuses to crawl until the crawler has a name of its own. Out of the box it therefore crawls less than this description of the full flow. The section "How the crawler behaves" explains each switch.

## Sample morning report

This is what the sales team reads. The text below is the output of the real report code, not an illustration: `tests/sample_report.py` runs the driver in a temporary folder on eight fictional companies, with a fake `claude`, a fake crawler and a fake CRM, and with the clock fixed to a made-up date. A test fails if this section or `docs/sample-morning-report.md` differs from what the code produces. Headings are moved down two levels to fit this page.

The night shows one account created, one held back because only Tier A goes to the CRM, one vendor caught by the backstop, one account that a person had already rejected, one site that refused the crawler, one error, and one company removed by the pre-filter.

<!-- sample-report:start -->
### Nightly run 2030-01-15

- Started 2030-01-15T01:00:00+00:00, finished 2030-01-15T01:04:00+00:00
- Model `opus`, cap 10
- HubSpot records given employee count/industry: 0
- Qualified but kept out of HubSpot (tier not in hubspot_tiers): 1, listed in `runs/held_qualified.csv`
- Stopped: **completed**

| pulled | prefiltered | backlog | processed | qualified | not qualified | errors | HubSpot created | updated | exists/unchanged | needs review | still pending |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7 | 1 | 0 | 7 | 3 | 3 | 1 | 1 | 0 | 0 | 1 | 0 |

#### Needs review (Qualified by P1, NOT written to HubSpot)

- **Pinecrest Markets** (pinecrest.example): HubSpot human_verdict_exists: HubSpot already says 'Not Qualified'; P1 now says Qualified. Owner to decide. https://app-na2.hubspot.com/contacts/YOUR_PORTAL_ID/record/0-2/301

#### Companies

| # | company | queue | verdict | buyer/vendor | tier | HubSpot | reason / error |
|---|---|---|---|---|---|---|---|
| 1 | Harrow & Finch Grocers (harrow-and-finch.example) | 2030-01-15 | QUALIFIED | BUYER | TIER_A | [created](https://app-na2.hubspot.com/contacts/YOUR_PORTAL_ID/record/0-2/new-1) | 62 stores with a large fresh offer and a published food waste target. A second distribution centre opens this year. |
| 2 | Lakeshore Grocers (lakeshore-grocers.example) | 2030-01-15 | QUALIFIED | BUYER | TIER_B | held | 31 stores, fresh categories are a clear part of the offer. No recent change found. |
| 3 | Tri-County Supply (tricounty.example) | 2030-01-15 | NOT_QUALIFIED | VENDOR | TIER_C |  | downgraded QUALIFIED->NOT_QUALIFIED (buyer_or_vendor=VENDOR); tier TIER_B->TIER_C (not qualified). Supplies 200 independent stores and runs none of its own. |
| 4 | Northfield Grocers (northfield.example) | 2030-01-15 | NOT_QUALIFIED | BUYER | TIER_C |  | [scope: headcount=14500] A national chain with more than 300 stores, outside the size we sell to. |
| 5 | Pinecrest Markets (pinecrest.example) | 2030-01-15 | QUALIFIED | BUYER | TIER_A | [human_verdict_exists](https://app-na2.hubspot.com/contacts/YOUR_PORTAL_ID/record/0-2/301) | HubSpot human_verdict_exists: HubSpot already says 'Not Qualified'; P1 now says Qualified. Owner to decide. |
| 6 | Quillbrook Markets (quillbrook.example) | 2030-01-15 | NOT_QUALIFIED | BUYER | TIER_C |  | [crawl: blocked (http 403)] The site refused the crawler and web search found too little to judge the offer. |
| 7 | Marlow Fresh (marlow-fresh.example) | 2030-01-15 | error |  |  |  | unparseable verdict: no JSON object line in result |
<!-- sample-report:end -->

## What this does not measure

There is no measurement of how good the qualifier is. The tests prove that a verdict is parsed, checked and written correctly. They do not prove that the verdict is right. There is no accuracy figure against accounts judged by hand, and no false negative rate: nobody has counted how many of the rejected companies a salesperson would have wanted. Rejected companies are registered and do not come back, so a false negative is a lost account. Section B of `tests/LIVE_CHECKLIST.md` describes how to measure both on a sample before trusting the pipeline with a real search. This repo also states no results: no counts per night, no time saved, no reply rates.

## System design

### An engine with the targeting in configuration

The code does not know what a good account is. Three files do, and they are the only ones to replace for another company:

| File | What it decides |
|---|---|
| `prompts/prompt1.md` | the qualification criteria and the wording of the verdict |
| `config/search.json` | which companies the Apollo search returns, and the pre-filter |
| `config/signals.json` | which pages the crawler visits first and which words and job titles it flags |

The output contract between the prompt and the code is a set of field names and tokens. It lives in `scripts/hubspot_writer.py`, is mirrored in `tests/contract_spec.py`, and is published as a JSON Schema in `prompts/p1_verdict.schema.json`.

### Division of labour: Python is exact, the LLM judges

Python owns everything that has to be exact and repeatable: the queue, deduplication, the cap, retries, timeouts, CRM writes, stop conditions and the report. None of that costs LLM usage, and all of it is covered by offline tests.

The LLM does the one thing code cannot: read what a company says about itself in public and decide whether the company fits. Its answer is free text followed by one line of JSON. Python treats that line as untrusted input. `scripts/hubspot_writer.py` checks that every required key is present, that every enumerated value is one of the allowed tokens, and that the domain in the verdict is the domain the queue asked about. Anything else is recorded as an error for that company and the run moves on.

Three business rules are enforced in code as a backstop, because a prompt alone is not a guarantee (`apply_backstop` in `scripts/run_nightly.py`):

- Verdicts are binary. A stray `MAYBE` becomes `NOT_QUALIFIED`.
- `QUALIFIED` requires the company to be classified as a `BUYER`. A qualified vendor is downgraded, and the writer refuses it a second time.
- The tier follows the final verdict. A `NOT_QUALIFIED` account is always stored as `TIER_C`, whatever tier the model gave it and whether or not the verdict was downgraded.

One contradiction is left as the model returned it: `QUALIFIED` with `TIER_C`. The driver does not guess which half is wrong. It notes the contradiction in the report row, and `hubspot_tiers` decides whether the account reaches the CRM. With the shipped setting (`["TIER_A"]`) it does not: it is held and listed in `runs/held_qualified.csv`. If you remove `hubspot_tiers`, such an account is written with the label Tier C.

### One fresh LLM session per company

Each company is qualified in its own `claude -p` process with its own context. The reasons:

- **Stable cost and quality.** The last company of the night gets the same empty context as the first. Nothing accumulates, so nothing degrades over a long run.
- **Isolation.** A timeout, a crash or a malformed answer affects one company. The driver records the error on that queue line and continues.
- **No cross-contamination.** One company's web pages cannot influence the verdict on the next.
- **Resumability.** Because no state lives in a conversation, the run can stop after any company and continue later from the queue file.

### The queue and state files

All state is plain files under `state/` and `runs/`, both git-ignored.

| File | Written by | Purpose |
|---|---|---|
| `state/source/<date>/page-<n>.jsonl` | `/source` | the Apollo rows, transcribed one per line |
| `state/source/<date>/candidates.json` | `source_queue.py select` | who was selected, written **before** anything is written to Apollo |
| `state/source/<date>/registered.json` | `/source` | the Apollo account ids after registration |
| `state/queue/<date>.jsonl` | `source_queue.py build`, then the driver | one line per company with a status. This is the resume point |
| `state/work/<domain>/` | the driver and the crawler | `apollo.json` (input row), `sources/` (crawled pages and `status.json`), `p1.md` (report), `p1.json` (verdict) |
| `state/nightly.lock` | the driver | stops two runs at once |
| `runs/<date>.md`, `.json`, `.log` | the driver, `nightly.ps1` | the morning report, its data, and the raw log |

Queue statuses: `pending`, `done`, `error`, `skipped_prefilter`, `hubspot_error`, `needs_review`. The queue file is replaced atomically (write a temp file, then rename) after every company. Once a queue file exists, `source_queue.py build` never overwrites it.

A batch id is the run date in the form `YYYY-MM-DD`, plus a letter for extra batches on the same day (`YYYY-MM-DDb`). String order is run order.

### Deduplication

A company must never be pulled or qualified twice. Three layers:

1. **Apollo Net New.** `/source` takes only the net-new part of the search result, then saves every pulled company as an Apollo account in a dated list (`sourced-<date>`). That includes companies the pre-filter rejected, so they never come back either.
2. **Local queue history.** `source_queue.py select` drops any domain that appears in an earlier queue file and reports it as `resurfaced`.
3. **CRM lookup.** The writer searches HubSpot by domain before it creates anything.

### Idempotent CRM writes

`scripts/hubspot_writer.py` is the only code that writes to HubSpot. Beyond the rules in the table at the top:

- **Never move backwards.** On existing records without a verdict, the account stage only moves forward, the name and domain are never renamed, and an existing owner or employee count is kept.
- **Fail closed on unknown values.** Before the first write it reads the live option lists of the enumeration properties. A value the portal does not list is reported as `invalid` and not written. A property whose option list is empty or missing has no valid value, so that is refused too.
- **A failed write does not repeat the LLM call.** The queue line becomes `hubspot_error`; the next run retries the write from the saved `p1.json`.

Running the same night twice makes no second Apollo pull, no second qualification and no second CRM write. The rerun writes its report to `runs/<date>-2.md` so the first report is kept.

### Crash recovery and stop conditions

| Situation | What the driver does |
|---|---|
| Usage limit reached | Stops. The current company stays `pending` and is done first next time. `stopped_reason: usage_limit` |
| Cap reached | Stops with `cap_reached`; the rest stay `pending` |
| Deadline (`run_deadline_min`) | Stops with `deadline` |
| 5 errors in a row (`max_consecutive_errors`) | Stops with `consecutive_errors` |
| 3 transient API errors in a row | Stops with `api_errors` |
| One company fails (timeout, no JSON, wrong domain, bad value) | Recorded on its queue line; retried on a later run up to `max_attempts` (2), then left for a person |
| HubSpot unreachable or no token | Stops before the first qualification call with `hubspot_unavailable` |
| `/source` died after selecting candidates | The next run finds `candidates.json` without a queue file and re-runs `/source` for that date, which resumes at registration without searching again |
| The driver itself crashes | The exception is caught at the top level and a report is still written, with `driver_crash` |
| The driver is killed, or the machine goes down | No report is written. The queue holds every company finished before that moment. The lock file stays, and a new run is refused until the lock is older than the run deadline plus 30 minutes (6 hours with the shipped settings). `run_until_done.py` removes a lock whose process no longer exists |
| A second run starts while one is active | Refused with `locked` |
| A child process hangs | Killed with its whole process tree after the timeout |
| A setting has a value the driver does not know (`claude_auth`) | Refused with `bad_settings` before anything runs |

A usage limit is recognised from an HTTP 429 status, or from a message of the CLI itself: the phrase has to start a line. The same words inside a sentence, for example a session quoting a rate-limit error of a website or a connector, count as an error for that one company and do not stop the run.

`scripts/run_until_done.py` is an optional wrapper that keeps resuming until the queue is empty. It sleeps until the reset time named in the usage-limit message, waits and retries on API errors (at most 4 times in a row), and stops for a person on anything else.

### What each session is allowed to do

The qualification session reads text written by strangers, so it is the one most exposed to prompt injection. The sessions that talk to Apollo can spend credits and write to an account. They get opposite sets of tools, and neither gets more than its job needs.

| Session | Can | Cannot |
|---|---|---|
| `/qualify` | read files, search the web | run a shell, write or edit files, reach Apollo, reach the CRM, fetch pages (unless switched on, see below) |
| `/source`, `/register-results` | call 7 named Apollo tools, run `apollo_url.py` and `source_queue.py`, write under `state/` | search the web, fetch pages, run the crawler, reach mail, calendar, drive, notes or the CRM |
| `/enrich` | the same, plus the Apollo enrichment tool and `enrich.py`, which fills two CRM fields (see below) | search the web, fetch pages, run the crawler, reach mail, calendar, drive or notes, reach the CRM in any other way |
| the Python driver | every other CRM write: create and update of qualified accounts | |

Two consequences that the table does not show:

- **`/qualify` can read any file the user can read.** Its file reads are not limited to the repository. It cannot write, run commands or fetch pages, but it can search the web, so a successful prompt injection from a crawled page or a search result could place the content of a local file in a search query. I have not limited `Read` to the repository, because I could not verify offline how the CLI treats a path rule for this session. It is listed under Known limitations, and step E7 of the checklist tests it.
- **`/enrich` causes CRM writes, and its script holds the CRM token.** The session runs `scripts/enrich.py apply`, a Python process that reads the token from the keyring and writes to HubSpot. So an LLM session does trigger CRM writes, and the driver is not the only process that holds the token. The write is narrow: two fields, `numberofemployees` and `industry`, on records this pipeline itself pushed; each only when the field is empty; `industry` only when Apollo's text matches one of the portal's own industry options exactly (compared without case and punctuation, with "&" read as "and"); each company at most once. The values come from a file the session wrote from Apollo's answer, so a wrong value in that file reaches the CRM. `/enrich` runs only when `apollo.enrich_pushed` is `true`.

How that is set up:

- `/qualify` is started with `--tools Read,Glob,Grep,WebSearch`, which removes every other built-in tool, with `--strict-mcp-config` and an MCP configuration that is empty by default, and with `--permission-mode dontAsk` and an explicit `--allowedTools` list (`qualify_args` in `scripts/run_nightly.py`).
- The Apollo sessions are started with `--disallowedTools` naming web search, web fetch, the page-fetch server, the crawler command and every other connector (`DEFAULT_SOURCE_DISALLOWED`).
- The project list in `.claude/settings.json` names each permitted Apollo tool one by one. It grants no web tool, no page-fetch tool and no crawler command, enables no MCP server for every session, and denies the Apollo tools that send email, create contacts or spend enrichment credits. The enrichment tool is granted only to the `/enrich` call.
- `/qualify` is also started with `--disallowedTools` naming Apollo as a whole and every other connector. That is a second layer: the strict MCP configuration already keeps them out.
- The CRM token is never passed to a session: `HUBSPOT_TOKEN` is removed from the environment of every child process in every mode. The token in the keyring can be read by any process of the same Windows user, which is how `enrich.py` gets it.

`tests/test_contracts.py` derives the tool list of each session type from these files and flags and compares it with expected lists written in the test file itself. It does not read this README, so the table above and the test have to be kept in step by hand. It is a test of configuration. What the real CLI does with the flags is verified by hand, with section E of `tests/LIVE_CHECKLIST.md`.

The session writes no files. The driver saves its answer. The skill text also tells the model that page content is data and not instructions, but the tool restrictions are what the design relies on.

### How the crawler behaves

Stated plainly, because a crawler touches other people's servers. Three behaviours are **off by default in this public version**: posing as a browser, getting past a refusal, and page fetching by the LLM session.

- **Small and slow.** The nightly mode reads at most 15 pages per company, 4 requests at a time with a delay between requests, inside a 90 second wall-clock cap.
- **It says who it is, or it does not run.** Every request sends the user agent in `crawler.user_agent`, without browser-like headers and without a browser's TLS fingerprint. The shipped value is a placeholder, `account-sourcing-pipeline/0.1 (+contact: set this to your own address)`. While the placeholder is in place the crawler refuses to run (exit code 2, no request made), the same way the pipeline refuses the example search. The driver then crawls no site, says so once in the report, and qualifies from web search only.
- **robots.txt before every request.** robots.txt is read once per host and consulted before the first request to the homepage, before each sitemap file, before every page of the crawl, before each job-board feed and before a stealth fetch. It is matched against the crawler's own user agent. A robots.txt that answers 401 or 403, fails with a server error or cannot be reached counts as "nothing is allowed". One that does not exist (404) allows everything.
- **A site that refuses is recorded and skipped.** If the homepage answers 401, 403, 407, 429 or 503, or shows a bot challenge, the crawler writes `blocked` and the reason to `status.json` and makes no further request to that company: no sitemap, no crawl, no job feed. The morning report shows it in the company's row. The qualification still runs, from web search, and is told not to fetch pages from that site.
- **Job boards.** Feeds are read from Lever, Greenhouse and Ashby, each after its own robots.txt. When the site shows no job-board link, the slug is guessed from the domain and the result is labelled `guessed`, because a guessed board can belong to another company.
- **LinkedIn.** The crawler only visits the company's own domain and the three job boards, and the qualification session is told never to open LinkedIn pages.

The three switches, all under `crawler` in `config/settings.json`, all shipped as `false`:

| Setting | When `true` |
|---|---|
| `impersonate_browser` | Requests use the fetch library's defaults, which present the client as a Chrome browser. robots.txt is then matched as `*`, because the client no longer has a name of its own |
| `stealth_fallback` | A page that refused the first request is fetched again with a stealth browser built to pass bot challenges. robots.txt still applies |
| `session_page_fetch` | The `/qualify` session gets page-fetch tools. These fetch any URL the session asks for, do not consult robots.txt, and use the fetch library's browser-like defaults. With `stealth_fallback` on as well, the session also gets the stealth fetch tool |

Only a JSON `true` turns a switch on. Anything else, including a missing or broken settings file, leaves it off.

### Credentials and billing

- **HubSpot:** a private-app token, read from the `HUBSPOT_TOKEN` environment variable or from Windows Credential Manager through `keyring` (service `account-sourcing`, name `HUBSPOT_TOKEN`). It lives only in the Python process. The portal id and the owner id are configuration, shipped as placeholders.
- **Apollo:** reached through the Apollo connector authorised on the claude.ai account (OAuth). There is no Apollo API key in the repo or in the environment. Credit spend is authorised in advance by the account owner, in writing, as a ceiling in the settings: `apollo.max_search_pages` search credits per run date, and `apollo.max_enrich_per_call` enrichment credits per call. A missing ceiling means no spend.
- **Claude:** `claude_auth` in `config/settings.json` says how the CLI is authenticated.

| `claude_auth` | What the driver does |
|---|---|
| `"subscription"` (default) | The LLM steps run under the CLI login of the person running the tool. API credentials are removed from every child process, and the run refuses to start when an `apiKeyHelper` is configured, so a run cannot create API charges nobody planned for |
| `"api_key"` | Metered API billing. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are passed through to the CLI and nothing is refused |

The subscription mode suits a single person running this for themselves, which is how I ran it. A company would run it on API keys or a team plan: set `claude_auth` to `"api_key"`. Following the terms of each connected service (Claude, Apollo, HubSpot) and of the websites you crawl is the operator's responsibility. Read them before you schedule this.

- `.gitignore` excludes `.env`, `.claude/settings.local.json`, `state/`, `runs/`, logs and CSV exports.

### Design decisions and tradeoffs

**A Python driver instead of one long LLM session.** A session that loops over a whole night of companies has to remember where it is, and its context grows all night. A driver with a queue file is boring and restartable. The cost is more moving parts: a process per company and a file contract between the driver and each skill.

**HubSpot through the Python API, not through an MCP connector.** A CRM write is a deterministic operation, so it is code. That makes the safety rules testable with a fake client, keeps CRM access away from the session that reads web pages, and avoids an LLM turn per write. The cost is that the property names and option labels are mapped in code and must be kept in sync with the portal.

**Apollo through the hosted connector, inside an LLM session.** This avoided managing an Apollo API key. The cost is real: an LLM copies search rows into files. To contain that, the skill is told to copy values exactly, and `source_queue.py` checks the format of every row and does all selection, deduplication and queue building itself. The model never writes the queue file. The check has a limit: it verifies that an id has the right shape, that a domain is well formed, and that a row has a name or a domain. It cannot tell whether a well-formed domain is the right one. A row copied with a valid but wrong domain passes, and the company is then qualified under that domain. Catching that needs a spot check against the Apollo list, or the Apollo REST API in place of the session.

**Register before qualifying, and save before registering.** Candidates are written to disk before any Apollo write. If the run dies between the two, the next run knows exactly which companies were selected. Registering rejected companies too costs list space in Apollo but guarantees they do not resurface.

**Conservative pre-filter.** A pre-filtered company is registered and gone for good, so the name and industry filters only exclude on a positive match. Unknown data never excludes. Country and headcount mismatches produce a warning that the qualification prompt sees, not an exclusion.

**Stop on a usage limit instead of retrying.** Retrying against an exhausted limit would mark every remaining company as failed. Stopping leaves them `pending`.

**Files instead of a database.** The state is small, append-mostly, and read by both Python and headless LLM sessions. JSONL files can be inspected and repaired by hand. The cost is no concurrency and no queries, which a single nightly run does not need.

**Binary verdicts.** The parser accepts a `MAYBE` token only so that it can downgrade it. A middle verdict hands the decision back to a person, and the CRM is meant to hold only accounts that are ready to work. The prompt has to lean one way and state its doubt in a `main_uncertainty` field.

**Crawl signals in a file, not in code.** The words that matter to one company's sales team mean nothing to another's. `config/signals.json` holds them, `scripts/signals.py` validates the file, and a missing or malformed file stops the crawler before its first request.

**A blocked site stays blocked.** A refusal is an answer. The pipeline loses the company's own pages for that account and qualifies on less. That is a worse verdict for a few accounts, accepted on purpose.

### Known limitations

- **Windows only.** The entry point is PowerShell, process cleanup uses `taskkill` and `tasklist`, and the skills and the tool allowlist name `.venv/Scripts/python.exe`.
- **The qualification session can read files outside the repository.** Combined with web search, a successful prompt injection could leak the content of a local file through a search query. Run the pipeline under a Windows user that holds nothing you would mind losing, and see step E7 of the checklist.
- **The enrichment session can cause CRM writes.** They are limited to two fields, only when empty, on records the pipeline pushed. See "What each session is allowed to do".
- **Permission boundaries are asserted by tests against configuration, and verified against the real CLI only through the manual checklist.** The tests prove which flags and lists the driver passes. They cannot prove what the CLI does with them.
- **The crawler's requests are tested against stand-ins, not against the web.** Its functions are tested with local HTML files, and the whole script is tested with every network call replaced. The spider that follows links is a library component and is not run in the offline suite. The test against a real site is opt-in.
- **The plain client identity is untested on real sites.** Requests without a browser fingerprint are refused by more sites than requests that look like a browser. How many accounts that costs is not measured.
- **The spider's robots.txt user agent is set through an internal hook of the crawl library** (pinned to one version in `requirements.txt`). If the hook is missing, the crawler prints a warning and the spider matches robots.txt as `*`.
- **The contract is in code.** The verdict keys, their tokens and the CRM option labels are mapped in `scripts/hubspot_writer.py`. Changing them means changing the prompt, the writer, the schema and `tests/contract_spec.py` together. The contract tests fail until all four agree.
- **The HubSpot link host is in code** (`portal_url`), for one hosting region.
- **The Apollo steps are not covered offline beyond their Python half.** The tests stub the `claude` binary. The connector calls, the scheduled task and the real CRM writes are verified by hand with `tests/LIVE_CHECKLIST.md`.
- **Rows copied from Apollo are checked for format, not for truth.** See the tradeoff above.
- **Usage limits are detected from an HTTP 429 status or from known phrases at the start of a line.** If the CLI words its message differently, the limit is treated as an error and the run falls back to the consecutive-error stop.
- **The lock is a file with an age check,** not an atomic lock. It prevents the realistic case (a manual run during the scheduled one), not two processes starting in the same instant.
- **No accuracy measurement.** See "What this does not measure".
- **Local state has no backup.** If `state/` is lost, the local dedupe history is lost. Apollo's dated lists remain.
- **Writing verdicts back to Apollo is off in the shipped settings** (`register_results: false`), and `apollo.enrich` is reserved and not implemented.
- **The relative path in `.mcp.json`** assumes `claude` is started from the repo root. If your setup does not resolve it, use an absolute path locally.

## How this was built

I wrote the requirements and the rules the pipeline must never break, approved the architecture, and later changed it, for example dropping the middle verdict. The code and the tests were written with Claude Code, Anthropic's coding agent, which also proposed the Python driver design. I ran the original against my own accounts. The public defaults are stricter and have not been run against real sites. The LLM steps also run on Claude Code, as skills started headless by the driver.

## Setup

Requirements: Windows, Python 3.11, the Claude Code CLI, an Apollo account connected as a claude.ai connector, and a HubSpot private-app token.

```powershell
cd <repo>

# 1. Python environment
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2. HubSpot private-app token into Windows Credential Manager
.venv\Scripts\python -m keyring set account-sourcing HUBSPOT_TOKEN

# 3. Check which login the Claude Code CLI uses
claude auth status
```

If `pip` fails with a "No such file or directory" error inside the `hubspot` package, the repo path is too long for Windows. Clone to a shorter path or enable long path support.

### Run the offline tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

The suite uses a stub in place of the `claude` binary, a stub crawler, a fake HubSpot client, and a harness that replaces every network call of the real crawler. It makes no network calls, spends no credits and writes to no CRM. The crawler tests against a real website are skipped unless you pass `--live-web` and set `SOURCING_LIVE_DOMAIN` to a public site you are allowed to crawl.

### Configuration

`config/search.json` is the Apollo search. The shipped file is an example and the pipeline refuses to run it:

```powershell
.venv\Scripts\python scripts\apollo_url.py resolve
```

prints `"ok": false` until you replace it. To replace it, build the search in Apollo's Companies tab, copy the URL, start `claude` in the repo and run `/search-config <url>`. The skill translates the URL, lists every filter it could not translate with the fallback it will use, and asks you to acknowledge each one.

`config/signals.json` holds the crawl signals. Check it with:

```powershell
.venv\Scripts\python scripts\signals.py
```

`config/settings.json` holds the limits:

| Key | Shipped value | What it does |
|---|---|---|
| `per_run_cap` | 50 | upper limit on companies qualified in one run |
| `claude_auth` | `"subscription"` | how the CLI is authenticated; see "Credentials and billing" |
| `model` | `opus` | model for the qualification session |
| `source_model` | `sonnet` | model for the Apollo sessions |
| `run_deadline_min` | 330 | the run stops after this many minutes |
| `max_attempts` | 2 | attempts per company before it is left for a person |
| `max_consecutive_errors` | 5 | stop after this many errors in a row |
| `hubspot_write` | `true` | set `false` to run without writing to HubSpot |
| `hubspot.portal_id`, `hubspot.owner_id` | placeholders | your portal id (for record links) and the owner set on new records |
| `hubspot_tiers` | `["TIER_A"]` | qualified companies in other tiers are held in `runs/held_qualified.csv` instead of the CRM |
| `crawler.user_agent` | sample | the name and contact address the crawler sends |
| `crawler.impersonate_browser`, `crawler.stealth_fallback`, `crawler.session_page_fetch` | `false` | see "How the crawler behaves" |
| `register_results` | `false` | write each verdict back to Apollo after the run |
| `apollo.max_search_pages` | 10 | credit ceiling: Apollo search pages per run date (one credit per page with results) |
| `apollo.enrich_pushed` | `true` | fill employee count and industry on pushed records (one Apollo credit per matched company, each company once) |
| `apollo.max_enrich_per_call` | 10 | credit ceiling of one enrichment call |

Three things in the settings and the report that are easy to misread:

- **`register_results` appears at two levels.** The top-level key is read by the driver: when `false`, `/register-results` is never started. `apollo.register_results` is read by the skill: when `false`, the skill answers `disabled` and writes nothing. Both have to be `true` for verdicts to be written back to Apollo. The shipped top-level value is `false`.
- **`total_cost_usd_equiv`** in `runs/<date>.json` is the sum of the `total_cost_usd` figures the CLI reports for the sessions of the run. It is an estimate at list prices, not a charge.
- **`crawler.user_agent`** has to be changed before anything is crawled.

### Run it

```powershell
# dry run: no Apollo pull, no CRM writes, queue unchanged. The qualification still runs and uses Claude usage.
.venv\Scripts\python scripts\run_nightly.py --cap 3 --dry-run

# real run, small cap
.venv\Scripts\python scripts\run_nightly.py --cap 3

# only finish what is already queued
.venv\Scripts\python scripts\run_nightly.py --resume

# keep resuming until the queue is empty
.venv\Scripts\python scripts\run_until_done.py --stop-at 08:00

# queue companies from an Apollo accounts CSV export instead of a search
.venv\Scripts\python scripts\import_csv.py "apollo-accounts-export.csv" --dry-run
```

A dry run never calls `/source`, so it needs pending companies in an existing queue file.

To schedule it, work through `tests/LIVE_CHECKLIST.md` first, then register the task with the commands in the header of `scripts/nightly.ps1`. In the morning, read `runs/<date>.md` or start `claude` and run `/morning`.

## Adapting it to your own company

1. **Replace the prompt.** Rewrite `prompts/prompt1.md` for your own ideal customer profile. Keep the three placeholders (`{{COMPANY}}`, `{{DOMAIN}}`, `{{APOLLO_RECORD}}`) and the JSON footer.
2. **Replace the search.** Run `/search-config` with your own Apollo URL, then review `pre_filter` and `exclude_keyword_tags` in `config/search.json`.
3. **Replace the crawl signals.** Edit `config/signals.json`: the words to flag on a page, the job titles to list first, and the paths to visit or skip.
4. **Decide on the contract.** If the sample's verdict fields fit your prompt, keep them. If not, change the mappings at the top of `scripts/hubspot_writer.py`, mirror them in `tests/contract_spec.py`, and regenerate the schema:

   ```powershell
   .venv\Scripts\python scripts\verdict_schema.py --write
   ```

5. **Create the CRM properties.** The writer fills company properties prefixed `sourcing_`. Create them in your portal, or change the prefix in `scripts/hubspot_writer.py` and in the tests.

   Enumerations: `sourcing_qualification_status`, `sourcing_account_stage`, `sourcing_sourced_from`, `sourcing_account_tier`, `sourcing_need_strength`, `sourcing_current_approach`, `sourcing_timing`, `sourcing_evidence_confidence`.

   Text: `sourcing_qualification_reason`, `sourcing_strongest_signal`, `sourcing_main_uncertainty`, `sourcing_roles_to_contact`, `sourcing_evidence_links`. Date: `sourcing_p1_date`.

   The option labels each enumeration needs are in `tests/contract_spec.py` (`HS_ENUMS`).
6. **Set your ids and your name.** Replace the placeholder `portal_id` and `owner_id` in the `hubspot` block of `config/settings.json`, or set the environment variables `HUBSPOT_PORTAL_ID` and `HUBSPOT_OWNER_ID`, which take precedence. An empty owner id sets no owner on new records. Change the link host in `portal_url` (`scripts/hubspot_writer.py`) if your portal is in another region. Put your own name and contact address in `crawler.user_agent`.
7. **Choose the billing mode.** Set `claude_auth`.
8. **Review the tool lists** in `.claude/settings.json` and `DEFAULT_SOURCE_DISALLOWED` against the connectors on your own account.

## Repo layout

```
prompts/prompt1.md              the qualification prompt (sample)
prompts/p1_verdict.schema.json  JSON Schema of the verdict, generated from the contract
config/search.json              the Apollo search (example; written by /search-config)
config/signals.json             crawl signals (sample)
config/settings.json            caps, models, timeouts, budgets, ids, billing mode, crawler options
.claude/skills/                 source, qualify, enrich, register-results, search-config, morning
.claude/settings.json           tool allowlist and denylist for this project
.mcp.json                       the page-fetching MCP server (used only with crawler.session_page_fetch)
scripts/run_nightly.py          the driver
scripts/run_until_done.py       optional wrapper that resumes until the queue is empty
scripts/nightly.ps1             Task Scheduler entry point
scripts/source_queue.py         candidate selection, dedupe, queue file, result mapping
scripts/apollo_url.py           Apollo URL → search.json, readiness check
scripts/source_pack.py          site crawl, no LLM
scripts/signals.py              loads and validates config/signals.json
scripts/hubspot_writer.py       HubSpot upsert, qualified only; the verdict contract
scripts/verdict_schema.py       generates the JSON Schema from the contract
scripts/enrich.py               employee count and industry for pushed records
scripts/import_csv.py           queue an Apollo CSV export as a batch
scripts/prioritize.py           move a batch's matching companies to the front
docs/system-design.md           the longer design document
docs/sample-morning-report.md   the sample report, generated by tests/sample_report.py
tests/                          offline pytest suite, TEST_PLAN.md, LIVE_CHECKLIST.md
state/  runs/                   created at run time, git-ignored
```

Steps after qualification, such as deeper account research and contact selection, are out of scope for this repo. They pick up CRM records that have `sourcing_p1_date` set.
