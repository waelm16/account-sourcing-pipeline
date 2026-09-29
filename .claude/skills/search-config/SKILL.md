---
description: Owner-run and interactive. Paste an Apollo Companies-tab URL. Claude translates it into config/search.json (the search the nightly /source runs), shows which filters could not be translated and which fallback applies to each, and gets the owner's sign-off.
background: false
arguments: [url]
---
Apollo URL: $url

This skill is interactive. The owner is present, so ask them whenever a
decision is theirs. The nightly job runs whatever this skill leaves in
config/search.json, unattended, so every filter that cannot be translated
needs their explicit acknowledgement.

# Tools

- Python through Bash, from the repo root:
  `.venv/Scripts/python.exe scripts/apollo_url.py ...`
- Read and Edit on config/search.json and config/settings.json.
- Apollo (`mcp__claude_ai_Apollo_io__*` or `mcp__apollo__*`):
  `apollo_labels_index` (free) is allowed. `apollo_mixed_companies_search` is
  allowed only for the sizing check in step 6, and only after the owner
  says yes to spending 1 credit.
- Nothing else. No account creation, no list writes, no field creation.

# Steps

## 1. Get the URL
If $url is empty, ask the owner to open the Apollo **Companies** tab with their
filters applied and paste the full browser URL
(`https://app.apollo.io/#/companies?...`).

## 2. Translate (read-only)
Run `.venv/Scripts/python.exe scripts/apollo_url.py parse "<url>"` and show:

- **Mapped filters.** A table of MCP parameter → value. Say each value in
  words, e.g. `organization_num_employees_ranges ["201,500","501,1000"]` =
  201 to 1,000 employees.
- **Handled.** For example, the Net New flag: /source keeps only the
  `organizations` (net-new) bucket.
- **Unmapped.** Each one with its `reason` and `fallback`. These are the
  filters the nightly search will NOT apply. Be concrete about the effect,
  e.g. "without the industry tag the search is not restricted to one industry".
- **Notes.** For example, keyword exclusions are handled by pre_filter.

Common unmapped cases and what to tell them:
- `organizationIndustryTagIds[]`: Apollo's industry picker. Offer to add
  `organization_naics_codes` (plus any SIC exclusions) instead.
- `personTitles[]` / `qPersonTitles[]` (a person-level signal): the
  company search cannot express "someone with this title works there". There
  are two options. (a) Accept it: Prompt 1 can look for those titles itself.
  (b) List mode: the owner runs the search in the UI, saves the companies to an
  accounts list, and we set `mode: "list"` with
  `filters.account_label_ids: [<list id>]`. Resolve the list id with
  `apollo_labels_index`, never from memory. In list mode there is no Net New:
  dedupe comes from the local queue history.
- `qKeywords` / keyword exclusions: can be approximated with pre_filter
  regexes, or dropped.

## 3. Decide
Ask the owner to confirm the translation, and ask whether they want any
suggested substitute filters added. Add nothing without a clear yes.

## 4. Write
1. Run `.venv/Scripts/python.exe scripts/apollo_url.py parse "<url>" --write`.
   This replaces `filters`, `unmapped` and `source_url` in config/search.json,
   keeps `pre_filter`, and resets `acknowledged_unmapped` to empty.
2. Apply any substitute filters or list mode they approved by editing
   `filters` / `mode` in config/search.json.
3. For each unmapped param they explicitly accept running without, run
   `.venv/Scripts/python.exe scripts/apollo_url.py ack "<param exactly as listed>"`.
   Never use `ack --all` on their behalf unless they say "acknowledge all".
4. Run `.venv/Scripts/python.exe scripts/apollo_url.py resolve`. It must print
   `"ok": true`. If it doesn't, show the errors (missing base filters such as
   industry codes, headcount or locations; unacknowledged params) and loop back
   to step 3. The nightly /source refuses to run until `resolve` is ok.

## 5. Pre-filter
Show `pre_filter` from config/search.json in plain words. It excludes by
name/industry regex. A prefiltered company is still registered in Apollo and
never qualified, so keep the regexes conservative. Country and headcount
mismatches only produce warnings. Ask whether they want any changes, and edit
only on their yes.

## 6. Optional sizing check (1 credit)
Offer, don't assume: "Run one test search (1 Apollo credit) to see how many
net-new companies match?" Only on yes, call `apollo_mixed_companies_search`
with the `params` from `resolve` plus `per_page: 5, page: 1`. Report the
total (`pagination.total_entries`) and 5 sample names/domains from the
`organizations` bucket, and say how many nights that pool covers at the
current `per_run_cap`. Register nothing.

## 7. Wrap up
Summarise in 3 to 5 lines: the mode, the key filters, what was acknowledged,
the per-night cap (`per_run_cap` in config/settings.json) and the search-page
ceiling (`apollo.max_search_pages`). The next nightly run uses this search.
Remind them the change is not committed until they commit config/search.json.
