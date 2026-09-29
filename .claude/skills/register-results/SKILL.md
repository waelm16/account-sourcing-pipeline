---
description: Nightly step 3. Write each company's Prompt 1 result for one run date back to Apollo, as the "Sourcing P1 Result" account field or, when that field does not exist, as dated result lists. Unattended and idempotent. The final line is JSON.
background: false
arguments: [date]
---
Run date: $date

You are running unattended (usually `claude -p "/register-results <date>"`
after the nightly loop). Nobody can answer questions, so never ask and never
wait. None of the calls below costs Apollo credits.

If $date is empty, use today's local date (YYYY-MM-DD).

# Tools (not optional)

- Apollo, through `mcp__claude_ai_Apollo_io__*` or `mcp__apollo__*` (match
  tools by suffix). Use ONLY these: `apollo_fields_index`,
  `apollo_accounts_update`, `apollo_labels_add_entity_ids_to_label_names`.
- NEVER call `apollo_fields_create` or `apollo_fields_update`. The owner
  creates the field by hand. Do not create accounts. Do not remove anyone
  from any list.
- Python through Bash, exactly like this, run from the repo root:
  `.venv/Scripts/python.exe scripts/source_queue.py ...`
- Read, but only on config/settings.json. You write no files yourself.

# Steps

## 1. Settings
Read config/settings.json. If `apollo.register_results` is false, the final
line is `{"status":"disabled","date":"$date"}`. Stop.
Keep `apollo.result_field_label` and `apollo.result_list_pattern`.

## 2. What still needs writing
Run `.venv/Scripts/python.exe scripts/source_queue.py unregistered --date $date`.
You get a JSON list of
`{apollo_account_id, domain, name, result, result_slug}`, where `result` is
one of Qualified, Maybe, Not Qualified, Prefiltered or Error. If the list is
empty, finish with `written: 0` (see Final line). Only these items get
written. Lines still `pending` are not in the list and are left alone.

## 3. Pick the method
Call `apollo_fields_index` with `modality: "account"`.
- Use method `field` only if a field has `label` exactly equal to
  `result_field_label`, its type is `picklist`, and its `picklist_values`
  names contain every `result` value in the list.
- Otherwise use method `list`, and set `field_missing: true` in the summary.

## 4a. Method `field`
For each item, call `apollo_accounts_update` with `id: apollo_account_id` and
`typed_custom_fields: {"<field id>": "<result>"}`. Send nothing else, so no
other account value gets overwritten. Keep track of the ids that succeed.
After every 10 successes, and once more at the end, run
`.venv/Scripts/python.exe scripts/source_queue.py mark --date $date --method field --ids <id1,id2,...>`.

## 4b. Method `list`
Group the items by `result_slug`. For each group, call
`apollo_labels_add_entity_ids_to_label_names` with
`label_names: [result_list_pattern with {date} = $date and {result} = result_slug]`
(for example `sourced-2030-01-15-qualified`), `modality: "accounts"`, and the
group's account ids, at most 100 per call. After each call succeeds, run
`.venv/Scripts/python.exe scripts/source_queue.py mark --date $date --method list --ids <ids>`.

## 5. Failures
If one call fails, record it and continue with the rest. Retry nothing more
than once. Items that are not marked stay in `unregistered` and are picked up
by the next run of this skill for the same date.

# Final line

End your reply with exactly one line of minified JSON and nothing after it:
`{"status":"ok|partial|error|disabled","date":"$date","method":"field|list|null","field_missing":true|false,"written":<n marked>,"failed":<n>,"by_result":{"Qualified":n,"Maybe":n,"Not Qualified":n,"Prefiltered":n,"Error":n},"remaining":<remaining from the last mark, or 0>,"error":null|"<one sentence>"}`

- `ok`: everything is written.
- `partial`: some items failed.
- `error`: nothing could be written, or unregistered/fields_index failed.
