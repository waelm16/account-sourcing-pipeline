---
description: Nightly step 2b. Enrich the companies this pipeline pushed to HubSpot with Apollo firmographics (employee count, industry), 1 Apollo credit per matched company, and fill the empty HubSpot fields. Unattended and idempotent. The final line is JSON.
background: false
arguments: []
---
You are running unattended (usually `claude -p "/enrich"` after the nightly
loop). Nobody is present to answer a question, so do not ask one.

# Credit authorisation (given in advance by the account owner)

`apollo_organizations_bulk_enrich` costs 1 credit per matched company, and
Apollo's tool asks for the account owner's confirmation before it spends.
This job runs unattended, so the owner has given that confirmation in advance
and in writing, in config/settings.json: `apollo.enrich_pushed` switches
enrichment on, and `apollo.max_enrich_per_call` is the number of credits one
call of this skill may spend. `enrich.py todo` applies that ceiling: it
returns at most that many domains and reports the ceiling as
`credit_ceiling`. The authorisation covers exactly the domains `todo` returns
and nothing more.

Because the confirmation is on file, do not ask for it again. If a tool
response contains an `apollo_plan_limit` block or says credits are exhausted,
stop and put its `message` in the error JSON.

# Tools (not optional)

- Apollo, through `mcp__claude_ai_Apollo_io__*` or `mcp__apollo__*` (match
  tools by suffix). Use ONLY `apollo_organizations_bulk_enrich`. Use one
  `_conversation_ref` token for the whole run.
- Python through Bash, exactly like this, run from the repo root:
  `.venv/Scripts/python.exe scripts/enrich.py ...`
- Write, only for the `results_file` that `todo` names. Nothing else.

# Steps

## 1. What needs enriching
Run `.venv/Scripts/python.exe scripts/enrich.py todo`. It prints
`{"count": N, "domains": [...], "credit_ceiling": C, "waiting": W, "results_file": "..."}`.
Domains counted under `waiting` are over the ceiling and are left for a later call.
If count is 0, the final line is
`{"status":"ok","enriched":0,"not_found":0,"no_new_data":0,"unmapped_industry":[],"errors":[]}`. Stop.

## 2. Enrich, 10 domains per call
Call `apollo_organizations_bulk_enrich` with the domains in chunks of at most 10,
exactly as `todo` printed them.

For EVERY domain you sent, append one line to `results_file` (JSON Lines,
create the file if missing):
`{"domain": "<the domain you sent>", "found": true, "estimated_num_employees": <value>, "industry": "<value>"}`

- Match each returned organization to the domain you sent by its
  `primary_domain` (or `website_url` host). `found` is false when no
  organization in the response matches that domain.
- Copy `estimated_num_employees` and `industry` exactly as Apollo returned them.
  Leave a key out when the organization does not have it. Never guess, never
  fill from memory, never translate the industry text.

## 3. Write to HubSpot
Run `.venv/Scripts/python.exe scripts/enrich.py apply`. It fills only empty
HubSpot fields and records every domain so it is never enriched twice.

# Final line

End with exactly one line of minified JSON and nothing after it.
- On success: the output of `apply`, verbatim.
- On failure:
  `{"status":"error","enriched":0,"not_found":0,"no_new_data":0,"unmapped_industry":[],"errors":[],"error":"<one sentence: which step, what happened>"}`
