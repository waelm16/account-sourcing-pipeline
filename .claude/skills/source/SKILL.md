---
description: Nightly step 1. Pull net-new companies from the Apollo search in config/search.json, register every pulled company in Apollo (dated accounts list) and write state/queue/<date>.jsonl. Unattended. The final line is JSON.
background: false
arguments: [date, cap]
---
Run date: $date
Cap: $cap

You are running unattended (usually `claude -p "/source <date> <cap>"` from
scripts/run_nightly.py). Nobody is present to answer a question, so do not
ask one. On anything unexpected, stop and emit the error JSON (see "Final
line").

If $date is empty, use today's local date (YYYY-MM-DD). If $cap is empty, use
`per_run_cap` from config/settings.json.

# Credit authorisation (given in advance by the account owner)

Apollo's tools ask for the account owner's confirmation before a call that
spends credits. This job runs unattended, so the owner has given that
confirmation in advance and in writing, in config/settings.json:
`apollo.max_search_pages` is the number of search credits this job may spend
for one run date. That setting is the confirmation. It covers this and
nothing more:

- `apollo_mixed_companies_search`, at most `apollo.max_search_pages` calls
  for this run date (1 credit per call that returns results). Count the page
  files already in state/source/$date/ against the ceiling. It is a ceiling,
  never a target.
- If the setting is missing, zero or not a number, there is no authorisation:
  make no search call and emit the error JSON.
- Enrichment is not authorised here. `apollo.enrich` is reserved and not
  implemented in v1, so ignore it. Prompt 1 does its own research.
- No other credit-consuming Apollo call is authorised. People search,
  people match, contact creation, sequences, emails and custom-field creation
  are all forbidden here.

Because the confirmation is on file, do not ask for it again. If a tool
response contains an `apollo_plan_limit` block or says credits are exhausted,
stop and put its `message` in the error JSON.

# Tools (not optional)

- Apollo: the claude.ai Apollo connector (`mcp__claude_ai_Apollo_io__*`) or,
  if the project has one, `mcp__apollo__*`. Match tools by suffix. Use ONLY:
  `apollo_mixed_companies_search`, `apollo_organizations_lookup` (free),
  `apollo_accounts_bulk_create`, `apollo_labels_add_entity_ids_to_label_names`,
  `apollo_labels_index`. Use one `_conversation_ref` token for the whole run.
- Python through Bash, exactly in this form, from the repo root:
  `.venv/Scripts/python.exe scripts/<script>.py ...` (forward slashes).
- Read / Write, only for config/ (read) and state/source/$date/ (write).
- Nothing else. No web search, no page fetching, no HubSpot. Never write
  state/queue/*.jsonl yourself; `source_queue.py build` does that.

# Steps

## 0. Settings
Read config/settings.json. Compute:
- cap = $cap, or `per_run_cap`
- max_pages = `apollo.max_search_pages`
- per_page = min(`apollo.per_page`, 100)
- list_name = `list_name_pattern` with `{date}` replaced by $date

## 1. Already done?
If state/queue/$date.jsonl exists, run
`.venv/Scripts/python.exe scripts/source_queue.py build --date $date`
(it reports `"status":"exists"`), print its output as the final line, and stop.
Make no Apollo call.

## 2. Resume?
- If state/source/$date/candidates.json exists: go straight to step 5. Do NOT
  search again.
- If page-*.jsonl files exist but candidates.json does not: run step 4's
  `select` first, then continue paging only if step 4's rules allow it.

## 3. Validate the search
Run `.venv/Scripts/python.exe scripts/apollo_url.py resolve --date $date`.
If it exits non-zero or prints `"ok": false`, emit the error JSON with its
`errors` joined by "; " and stop. Make no Apollo call. The owner must run
/search-config first.
Otherwise keep `mode` and `params` from its output, and `exclude_params` if
it is present.

## 3b. Excluded companies (free)
Skip this step if `exclude_params` is absent or state/source/$date/exclude.jsonl
already exists.
Otherwise, for n = 1, 2, ... up to 10: call `apollo_organizations_lookup` with
every key of `exclude_params` exactly as given, plus `page: n` and
`per_page: 100`. Append one line per returned organization to
state/source/$date/exclude.jsonl: `{"org_id": <id>, "domain": <domain>, "name": <name>}`
(copy exactly; leave `domain` out when the row has none). Stop when a page
returns fewer than 100 organizations. If the first page returns none, write an
empty exclude.jsonl. This lookup costs no credits. `select` marks these
companies `skipped_prefilter` (reason `excluded_keyword_tag`), so they are
registered in Apollo but never qualified.

## 4. Search and transcribe, page by page
For n = 1, 2, ... (starting after any existing page files):

1. Call `apollo_mixed_companies_search` with every key of `params` exactly as
   given, plus `page: n` and `per_page: per_page`.
2. Pick the rows:
   - mode `net_new_search`: use ONLY the `organizations` bucket (net-new) and
     ignore the `accounts` bucket completely.
   - mode `list`: use the `accounts` bucket (the list's saved accounts).
3. Write state/source/$date/page-<n>.jsonl, one JSON object per row, in the
   order Apollo returned them, with these keys:
   - `org_id`: the row's `id` (net_new) or its `organization_id` (list)
   - `account_id`: list mode only, the row's `id`
   - `name`
   - `domain`: `primary_domain`, else `domain`, else `website_url`
   - `industry`
   - `headcount`: `estimated_num_employees`
   - `country`
   - `technologies`: technology names, only if the row carries them
   - `funding_total`: `total_funding`
   - `latest_funding_date`: `latest_funding_round_date`
   - `latest_funding_stage`
   - `job_titles`: only if the row carries them

   Copy values exactly. Leave a key out when the row does not have it. Never
   guess, never fill from memory, never "fix" a domain. If the page returned
   0 rows, write an empty file.
4. Run `.venv/Scripts/python.exe scripts/source_queue.py select --date $date --cap <cap>`.
   If it reports `invalid > 0`, compare `invalid_detail` with the tool result
   you already have. Rewrite that page file once, correctly, and re-run
   select. Do not search again for this.
5. Stop paging when ANY of these is true: `need_more` is false; n equals
   max_pages; n is at least the response's `pagination.total_pages`; or the
   page had 0 rows. Otherwise go on to n+1.

If select reports `to_register: 0`, skip to step 8. `build` will report
`"status":"empty"` and no Apollo write happens.

## 5. Register in Apollo (never skip, never partial on purpose)
Read state/source/$date/candidates.json. Register EVERY entry in
`candidates` (both `pending` and `skipped_prefilter`: a prefiltered company
must never resurface either). Also register every domain in `resurfaced`;
those are registered but not queued.

- mode `net_new_search`: call `apollo_accounts_bulk_create` with
  `run_dedupe: true` at the top level, in chunks of at most 100. Pass
  `{"name", "domain"}` per company, or `{"name"}` when there is no domain.
  Collect every account the response returns, from both the created accounts
  and `existing_accounts`, as
  `{"domain", "name", "account_id": <account id>, "organization_id"}`.
- mode `list`: do not create anything. The candidates already carry
  `apollo_account_id`.

## 6. Add to the dated list
Call `apollo_labels_add_entity_ids_to_label_names` with every account id
from step 5 (plus, in list mode, the candidates' `apollo_account_id`),
`label_names: [list_name]` and `modality: "accounts"`, in chunks of at most
100. The call is idempotent, and it creates the list if missing. Keep the
list's `app_url`.

## 7. Record the registration
Write state/source/$date/registered.json:
`{"list_name": "<list_name>", "list_url": "<app_url>", "accounts": [ ...step 5 entries... ]}`

## 8. Build the queue
1. Run `.venv/Scripts/python.exe scripts/source_queue.py build --date $date --check`.
   If `unregistered > 0`, retry step 5 and step 6 ONCE for the entries in
   `missing`, merge the new accounts into registered.json, and re-run
   `--check`. Do not retry more than once. Skip the retry for any domain named
   in a `dedupe_name_collision` warning: Apollo's dedupe matched a
   different company with the same name, so a retry returns the same wrong
   account. Never edit registered.json to force such a match. The warning reaches the morning
   report either way.
2. Run `.venv/Scripts/python.exe scripts/source_queue.py build --date $date`.
   It writes state/queue/$date.jsonl atomically and prints the summary.

# Final line

End your reply with exactly one line of minified JSON and nothing after it.

- On success, that line is the output of the last `build` command, verbatim.
  Its keys: status (ok|exists|empty), date, pulled, pending, prefiltered,
  resurfaced, registered, unregistered, credits_used, list_name, queue_file,
  warnings, error.
- On failure:
  `{"status":"error","date":"$date","pulled":0,"pending":0,"prefiltered":0,"resurfaced":0,"registered":0,"unregistered":0,"credits_used":<page files written with rows>,"list_name":null,"queue_file":null,"warnings":[],"error":"<one sentence: which step, what happened>"}`

A failure after step 4 is safe. candidates.json exists, so the next /source
for the same date resumes at step 5 without searching again, and bulk_create
dedupe returns the ids of accounts that were already created.
