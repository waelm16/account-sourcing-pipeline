---
description: Morning review of the nightly sourcing + Prompt 1 run. Summarises runs/<date>.md, lists what reached HubSpot, what needs the owner, and what carries over. Read-only.
arguments: [date]
---
Morning review for the nightly run. Date argument: "$date" (if it is empty or
not a YYYY-MM-DD date, use the most recent run).

This skill is READ-ONLY. Do not write to HubSpot or Apollo, do not edit queue
files, and do not re-run anything. If something needs action, say which
command the owner should run.

# Inputs (use the Read and Glob tools, no shell)

1. Glob `runs/*.json` and pick the report for the date: `runs/<date>.json` plus
   any same-day reruns `runs/<date>-2.json`, `runs/<date>-3.json` (read all of
   them; together they are the night). Ignore `*-dry-run*` files unless the
   owner asked about a dry run or they are the only files for that date.
2. Read the matching `runs/<date>*.md` for the rendered table, and the last
   ~40 lines of `runs/<date>.log` only if a report says `stopped_reason` is
   `driver_crash`, `source_failed` or `hubspot_unavailable`.
3. For each company in the report with `verdict` QUALIFIED or status
   `needs_review`, you may Read `state/work/<domain>/p1.json` for the one-line
   reason, strongest signal and main uncertainty. Do not read p1.md unless
   the owner asks about a specific company.
4. Glob `state/queue/*.jsonl` and count lines still `pending` or `error`
   (attempts < 2) across all dates: that is tomorrow's carry-over.

# Output (short, scannable, no preamble)

**Night of <date>**: one line with pulled / prefiltered / processed /
qualified / not qualified / errors, and how the run ended
(`stopped_reason`, in plain words: completed, cap reached, usage limit hit at
company N, deadline, API errors, HubSpot unavailable, crash).

**New in HubSpot** (action created/updated): a table with company, tier,
need strength, one-line reason, HubSpot link. These are ready for the
downstream research steps, which are out of scope for this repo (they pick up
records that have `sourcing_p1_date` set).

**Held out of HubSpot**: QUALIFIED companies whose tier is not in
`hubspot_tiers` (config/settings.json, e.g. TIER_B when only TIER_A
goes to HubSpot). Give the count per tier and point to
`runs/held_qualified.csv` (read it for names if there are fewer than ~10).

**Needs you** (list each, with the exact reason and link):
- `needs_review`: Qualified by P1 but not written. `human_verdict_exists`
  means HubSpot already holds a Needs Review / Not Qualified set by hand, so decide
  whether to flip it. `conflict` means duplicate records share the domain, so
  merge them. `invalid` means an enum value was rejected by HubSpot.
- backstop downgrades (QUALIFIED to NOT_QUALIFIED because not BUYER): worth a
  30-second look, since vendors are the most common false positive.
- scope warnings from /source (company outside the search scope).
- report notes (for example `/source` warnings, candidates not registered in
  Apollo, orphaned runs recovered or not).

**Errors and carry-over**: count and one line per error type (timeout,
unparseable verdict, skill error, transient API). Companies that hit 2 attempts
are no longer retried automatically: list them by domain.

**Next step**: one line. If the run stopped on a usage limit or crashed:
`powershell -File scripts\nightly.ps1 -Resume` (it only drains the queue). If
there is nothing to do, say so.

Keep it under ~40 lines unless the owner asks for more detail.
