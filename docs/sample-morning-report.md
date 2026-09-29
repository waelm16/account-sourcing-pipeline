# Nightly run 2030-01-15

- Started 2030-01-15T01:00:00+00:00, finished 2030-01-15T01:04:00+00:00
- Model `opus`, cap 10
- HubSpot records given employee count/industry: 0
- Qualified but kept out of HubSpot (tier not in hubspot_tiers): 1, listed in `runs/held_qualified.csv`
- Stopped: **completed**

| pulled | prefiltered | backlog | processed | qualified | not qualified | errors | HubSpot created | updated | exists/unchanged | needs review | still pending |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7 | 1 | 0 | 7 | 3 | 3 | 1 | 1 | 0 | 0 | 1 | 0 |

## Needs review (Qualified by P1, NOT written to HubSpot)

- **Pinecrest Markets** (pinecrest.example): HubSpot human_verdict_exists: HubSpot already says 'Not Qualified'; P1 now says Qualified. Owner to decide. https://app-na2.hubspot.com/contacts/YOUR_PORTAL_ID/record/0-2/301

## Companies

| # | company | queue | verdict | buyer/vendor | tier | HubSpot | reason / error |
|---|---|---|---|---|---|---|---|
| 1 | Harrow & Finch Grocers (harrow-and-finch.example) | 2030-01-15 | QUALIFIED | BUYER | TIER_A | [created](https://app-na2.hubspot.com/contacts/YOUR_PORTAL_ID/record/0-2/new-1) | 62 stores with a large fresh offer and a published food waste target. A second distribution centre opens this year. |
| 2 | Lakeshore Grocers (lakeshore-grocers.example) | 2030-01-15 | QUALIFIED | BUYER | TIER_B | held | 31 stores, fresh categories are a clear part of the offer. No recent change found. |
| 3 | Tri-County Supply (tricounty.example) | 2030-01-15 | NOT_QUALIFIED | VENDOR | TIER_C |  | downgraded QUALIFIED->NOT_QUALIFIED (buyer_or_vendor=VENDOR); tier TIER_B->TIER_C (not qualified). Supplies 200 independent stores and runs none of its own. |
| 4 | Northfield Grocers (northfield.example) | 2030-01-15 | NOT_QUALIFIED | BUYER | TIER_C |  | [scope: headcount=14500] A national chain with more than 300 stores, outside the size we sell to. |
| 5 | Pinecrest Markets (pinecrest.example) | 2030-01-15 | QUALIFIED | BUYER | TIER_A | [human_verdict_exists](https://app-na2.hubspot.com/contacts/YOUR_PORTAL_ID/record/0-2/301) | HubSpot human_verdict_exists: HubSpot already says 'Not Qualified'; P1 now says Qualified. Owner to decide. |
| 6 | Quillbrook Markets (quillbrook.example) | 2030-01-15 | NOT_QUALIFIED | BUYER | TIER_C |  | [crawl: blocked (http 403)] The site refused the crawler and web search found too little to judge the offer. |
| 7 | Marlow Fresh (marlow-fresh.example) | 2030-01-15 | error |  |  |  | unparseable verdict: no JSON object line in result |
