# Live checklist

These steps spend Apollo credits, write to HubSpot, need Task Scheduler or need the real CLI, so the automated tests
do not run them.
Work through them in order on your own accounts. Tick each box only once its "Expect" line is true.

Before you start: `.venv` exists, `HUBSPOT_TOKEN` is in keyring (`account-sourcing`/`HUBSPOT_TOKEN`), the Apollo
connector is authorised in claude.ai, the `hubspot` block of `config/settings.json` (or the environment variables
`HUBSPOT_PORTAL_ID` and `HUBSPOT_OWNER_ID`) holds your own ids, and the `sourcing_*` company properties exist in your portal.
`claude_auth` in `config/settings.json` says how you pay for Claude, and `crawler.user_agent` holds a name and a contact
address of your own. You have read the terms of the services you connect. `.venv\Scripts\python -m pytest tests -q` is green.

## A. Apollo (spends about 1-2 search credits)
- [ ] **A1.** Run `.venv\Scripts\python scripts\apollo_url.py parse "<your Companies-tab URL>"`.
  Expect: the JSON `filters` match the filters you set in the UI. Every filter it could not translate is in `unmapped`
  with a `fallback`, for example person titles and keyword exclusions. The Net New flag shows under `handled`. Then run
  `/search-config <url>` interactively. It writes `config/search.json` and asks you to acknowledge each unmapped param.
  Expect: `.venv\Scripts\python scripts\apollo_url.py resolve` prints `"ok": true`. Until then `/source` refuses to run,
  because the shipped `config/search.json` is an EXAMPLE.
- [ ] **A2 (headless: the path the nightly uses).** Print the exact flags the driver passes:
  `.venv\Scripts\python -c "import sys;sys.path.insert(0,'scripts');import run_nightly as r;print(r.source_args('<today>',5,'opus',r.load_settings(r.REPO_ROOT)))"`.
  Then run `claude` with those arguments from the repo root. The invocation is `/source <YYYY-MM-DD> 5`: positional date, then cap,
  with no `--cap` flag. Expect:
  - `is_error: false`. The final JSON line has `status: ok`, `pending` ≤ 5, `credits_used: 1` and `registered == pulled`.
  - The session does not stop to ask you about the credit spend. You gave that confirmation in advance, in writing:
    `apollo.max_search_pages` in `config/settings.json` is the number of search credits one run date may spend, and the
    skill treats that setting as the confirmation Apollo's tool asks for. Check the number before you run this.
    The Apollo tools come from the claude.ai connector (`mcp__claude_ai_Apollo_io__*`),
    so `claude` must be logged in with the account that has the connector, and run without `--bare`.
  - `state/source/<today>/page-1.jsonl`, `candidates.json` and `registered.json` exist. `state/queue/<today>.jsonl` has
    the pending lines with domain, name, apollo_org_id and apollo_account_id filled in.
  - Apollo → Lists (accounts) shows `sourced-<today>` holding every pulled company, including prefiltered ones.
  - Apollo → Settings → Credits shows 1 search credit used.
- [ ] **A2c (row shape).** Open `state/source/<today>/page-1.jsonl` and record which of industry / headcount / country /
  technologies / funding_total the Apollo search rows actually carry. The HubSpot writer (numberofemployees) and the
  Prompt 1 context depend on these.
- [ ] **A2d (Net New).** Re-run the same search in the Apollo UI with Net New on. Expect: the
  companies from A2 are gone. On the **next night**, the `/source` summary (in `runs/<date>.json` → `source.summary`) must show
  `"resurfaced": 0`. A value > 0 means registering by domain did NOT remove orgs from Net New.
- [ ] **A3.** Run `/source <today> 5` again the same day. Expect: `status: exists`, no Apollo search and no credit spend.
- [ ] **A4.** `/register-results` never creates the `Sourcing P1 Result` field. While the field does not exist, expect the
  list fallback after a driver run: `sourced-<date>-qualified` / `-maybe` / `-rejected` / `-prefiltered` / `-error`.
  To use the field instead, create it by hand (Settings → Fields → Account, picklist Qualified / Maybe / Not Qualified /
  Prefiltered / Error, with the label exactly `Sourcing P1 Result`). Re-running `/register-results <date>` must then report `written: 0`,
  which shows it is idempotent. Note that `register_results` is `false` in the shipped `config/settings.json`.

## B. Prompt 1 quality (uses Claude usage, no HubSpot writes)
- [ ] **B1.** Qualify 5 companies you already judged by hand as qualified, using the driver's exact flags. For each one:
  first run `.venv\Scripts\python scripts\source_pack.py <domain> --light`,
  write `state/work/<domain>/apollo.json` (any queue row), then print the flags with
  `.venv\Scripts\python -c "import sys;sys.path.insert(0,'scripts');import run_nightly as r;print(r.qualify_args('<domain>','<Name>','opus',r.load_settings(r.REPO_ROOT)))"`
  and run `claude` with them. Expect: the verdicts agree with your own, and
  `r.parse_verdict(<result>)` accepts the final line. Note `total_cost_usd` and the usage per company.
  If known-good accounts come back NOT_QUALIFIED, the prompt is too strict for companies whose work is not public.
- [ ] **B2 (buyer/vendor calibration).** Qualify a few companies you know are vendors. Expect
  `buyer_or_vendor: VENDOR` and a qualification that is not `QUALIFIED`. Then qualify a few borderline buyers that
  must stay QUALIFIED. Any miss here means Part 1 of `prompts/prompt1.md` needs another pass before the nightly goes live.

## C. End-to-end driver (writes to HubSpot)
- [ ] **C1 (dry).** `--dry-run` never calls `/source`, so today's queue must already exist. Run A2 first, or copy a
  queue file to `state/queue/<today>.jsonl`. Then run `.venv\Scripts\python scripts\run_nightly.py --cap 3 --dry-run`.
  Expect: `runs/<today>-dry-run.md` and `.json` exist, the queue file is unchanged, and no HubSpot record changes. The dry run
  still makes read-only HubSpot lookups and runs Prompt 1, which uses Claude usage.
- [ ] **C2 (real).** Run `.venv\Scripts\python scripts\run_nightly.py --cap 3`. Expect: each QUALIFIED company
  whose tier is listed in `hubspot_tiers` is in HubSpot (search by domain) with `sourcing_qualification_status=Qualified`,
  `sourcing_account_stage=Qualified`, `sourcing_sourced_from=Apollo`, `sourcing_p1_date=<today>` and `sourcing_evidence_links`
  pipe-separated. NOT_QUALIFIED companies are **not** in HubSpot. `account_tier` shows correctly, with no 400 error.
- [ ] **C3.** Re-run the same command. Expect: no duplicate companies in HubSpot (search each domain and
  get 1 result) and none in the Apollo list. No `/source` or `/qualify` call happens, since all lines are already `done`. The first report is
  kept: the rerun writes `runs/<today>-2.md`.
- [ ] **C4 (no downgrade).** Pick one company that is already `Qualified` in HubSpot, preferably at a later stage.
  Add it by hand to `state/queue/<today>.jsonl` as `pending` and run the driver with `--cap 1`. Expect:
  HubSpot action `exists` in the report and no field changes on the record: status, stage and text are all untouched.
- [ ] **C4b (human verdict).** Do the same with a company whose HubSpot status is `Needs Review` or `Not Qualified`.
  If Prompt 1 now says QUALIFIED, expect queue status `needs_review` in the "needs review" section of the report and no HubSpot change.
- [ ] **C5.** Start `--cap 5` and kill it with Ctrl+C after the first company. Re-run with `--resume`.
  Expect: it continues from the first `pending` line and nothing is processed twice.
- [ ] **C6 (usage limit).** This is covered by the stubbed tests (`test_run_nightly.py`). For a live check, run the driver
  near the end of a usage window. Expect: a clean stop, `stopped_reason` in `runs/<date>.md`, and the remaining lines still `pending`.
- [ ] **C7 (leftovers from earlier days).** Leave a line `status:"pending"` in yesterday's `state/queue/<yesterday>.jsonl`, then
  run today's driver. Expect: yesterday's pending line is processed **before** (or instead of) a new `/source` pull. Without
  this, companies already registered in Apollo, and so gone from Net New, are lost for good.

## D. Scheduling
- [ ] **D1.** Register the task with the commands in the header of `scripts/nightly.ps1` (task
  `AccountSourcing\Nightly`, 01:00, run whether logged on or not). Then run `schtasks /Run /TN "AccountSourcing\Nightly"`.
  Expect: `runs/<today>.log` ends with `nightly.ps1 exit 0`, and `runs/<today>.md` shows a `/source` that succeeded. That proves the
  claude.ai Apollo connector (OAuth) and the keyring HubSpot token work from the Task Scheduler context. If `/source` fails
  there, re-create the task with `/IT` ("only when user is logged on") and leave the session locked.
- [ ] **D2.** Check that the task has "Wake the computer to run this task" set, a 6 h limit, and 01:00 daily. The next morning,
  `runs/<date>.md` exists.

## E. What each session can really do (the real CLI, no credits, no writes)

The offline tests check the flags and lists the driver passes. Only the real CLI can show what those flags do.
Run these from the repo root, with the driver's exact flags, and keep the answers with your notes.

- [ ] **E1 (qualify session, tools).** Print the flags:
  `.venv\Scripts\python -c "import sys;sys.path.insert(0,'scripts');import run_nightly as r;a=r.qualify_args('example.com','Example','opus',r.load_settings(r.REPO_ROOT));print(a[2:])"`.
  Start `claude -p "List every tool you can call in this session, one per line, with built-in tools and MCP tools in separate lists. Do not call any of them."` followed by those flags.
  Expect: the built-in list is exactly Read, Glob, Grep, WebSearch. No Bash, PowerShell, Write, Edit or WebFetch.
  No Apollo tool, no HubSpot tool, no mail, calendar, drive or notes tool. No page-fetch tool while
  `crawler.session_page_fetch` is `false`.
- [ ] **E2 (qualify session, refusals).** With the same flags, ask the session to do four things it must not be
  able to do: run `echo test` in a shell, write a file `state/should-not-exist.txt`, call an Apollo search tool,
  and fetch `https://example.com` with a page-fetch tool. Expect: each is refused or the tool does not exist, the
  session does not stop to ask for permission, and `state/should-not-exist.txt` does not exist afterwards.
- [ ] **E3 (qualify session, environment).** Set dummy values for `HUBSPOT_TOKEN` and `ANTHROPIC_API_KEY` in your
  shell, then run `.venv\Scripts\python -c "import sys;sys.path.insert(0,'scripts');import run_nightly as r;e=r.child_env();print([k for k in ('HUBSPOT_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN') if k in e])"`.
  Expect `[]` with `claude_auth` set to `subscription`, and only the API names with `api_key`. `HUBSPOT_TOKEN`
  must never be in the list.
- [ ] **E4 (Apollo sessions, tools).** Print the flags with `r.source_args('<today>',5,'opus',r.load_settings(r.REPO_ROOT))`
  in place of `qualify_args`, and ask the same "list every tool" question. Expect: the seven Apollo tools the
  skill names, Read, Write and Edit, and Bash. No WebSearch, no WebFetch, no page-fetch tool, no HubSpot, mail,
  calendar, drive or notes tool.
- [ ] **E5 (Apollo sessions, refusals).** With the source flags, ask the session to search the web for anything, to
  fetch `https://example.com`, and to run `.venv/Scripts/python.exe scripts/source_pack.py example.com --light`.
  Expect: all three are refused and no folder `state/work/example.com` appears.
- [ ] **E6 (page fetch switch).** Only if you intend to use it: set `crawler.session_page_fetch` to `true`, repeat E1,
  and expect four page-fetch tools and no stealth tool. Set it back to `false` afterwards unless you have decided
  to run with it.
- [ ] **E7 (qualify session, files outside the repository).** Put a file with a harmless marker, for example
  `marker-7421`, in your home folder. With the flags from E1, ask the session to read that file by its full path and
  to repeat its content. Expect, with the shipped configuration: it can. The README lists this under Known
  limitations. If you want it closed, replace `Read` by a path rule for the repository in the project list and in
  `qualify_tools`, repeat this step, and check that E1 and a normal `/qualify` run still work. That change is not
  shipped because it could not be verified offline.

If any expectation fails, the README's table "What each session is allowed to do" is wrong for your CLI version.
Do not schedule the job until it is true again.
