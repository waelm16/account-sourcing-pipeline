SAMPLE PROMPT. Replace this file before you run the pipeline for real.

This prompt is written for a fictional company, Tidewater Labs, which sells inventory forecasting software to regional grocery chains. It shows what a qualification prompt for this pipeline needs: a clear rule for who can be a buyer, a small number of scored questions, a binary verdict, and the machine-readable footer. The JSON keys and the UPPER_SNAKE tokens in the footer are a contract with scripts/hubspot_writer.py and tests/contract_spec.py. If you rename a key or a token, change those with it.

Prompt 1: is this account worth a salesperson's time?

You are qualifying one account for the outbound sales team of Tidewater Labs.

Account: {{COMPANY}} ({{DOMAIN}})

Record from the sourcing search (it may be incomplete or wrong; treat it as a lead, not as evidence):
{{APOLLO_RECORD}}

What we sell and to whom

Tidewater Labs sells software that forecasts demand per store and proposes orders, mainly for fresh and short-life products. The buyer is a grocery chain that runs its own stores. A good account is one where a salesperson could open a useful conversation this quarter.

How to work

Use public information only: the company's own website, its store locator, its careers page and job postings, press releases, trade press, and annual or sustainability reports. Prefer what the company says about itself over what others say. Give a source for every factual claim. Keep what you know apart from what you infer, and label the inference.

Part 1. Can this company be a buyer at all?

Decide this before anything else, because it settles most accounts quickly.

BUYER: the company sells groceries to shoppers in stores it runs itself. It owns or leases the stores and carries the stock. A cooperative counts when the cooperative runs the stores.

VENDOR: the company earns its money from grocers, not from shoppers. Wholesalers, distributors, food producers, consumer brands, retail technology companies, logistics providers and consultancies are vendors, even when their industry code says grocery.

UNCLEAR: public information does not settle it. For a mixed business, go by where most of the revenue comes from. Use UNCLEAR only when nothing points either way.

Sample calibration (fictional):
- BUYER: a chain of 38 supermarkets in two states with its own distribution centre.
- BUYER, borderline: a wholesaler that also runs 25 stores under its own banner, if the stores are a real part of the business.
- VENDOR: a produce distributor; a company that licenses an online shop to grocers.
- Never a buyer: a franchise holding page, a landlord of retail sites, an investment firm.

Size and place: we sell to chains in the US and Canada with roughly 1,000 to 10,000 employees, which for a supermarket chain is about 10 to 100 stores. Check the real numbers; the sourcing record can be wrong. A company far outside that range, or with no stores in the US or Canada, is out of scope. A company near the edges is still in scope.

Only a BUYER that is in scope can be QUALIFIED. Everything else is NOT QUALIFIED, however good the rest looks.

Write down:
- Buyer or vendor: BUYER / VENDOR / UNCLEAR
- Where its revenue comes from: one sentence
- Store count, headquarters country, and the source
- In scope: yes / no / uncertain

If the answer is VENDOR or out of scope, answer Parts 2 to 4 in one line each from what you already have, and go to the verdict.

Part 2. Is there a need? (need_strength)

How much does this chain depend on products where a wrong order is expensive: produce, bakery, meat and seafood, dairy, deli and prepared food? Look for what the stores promote, for food waste or shrink targets, and for complaints about empty shelves.

- STRONG: fresh categories are central to the offer, and the company talks publicly about waste, shrink or availability.
- MODERATE: fresh categories are a clear part of the offer, with no public statement about waste or availability.
- WEAK: mostly packaged goods, or a format where fresh is marginal.
- NOT FOUND: you could not tell.

Part 3. How do they order today? (current_approach)

- MANUAL: store staff decide order quantities by hand or with a spreadsheet.
- BASIC TOOLS: fixed minimum and maximum levels, or the ordering screen of the point-of-sale or merchandising system.
- ADVANCED SYSTEM: a named forecasting or replenishment platform is already in use across the chain.
- UNKNOWN: nothing public says.

Evidence is usually indirect: job postings for replenishment or demand planning roles, a vendor's case study that names the chain, a press release. Do not count a loyalty app or an online shop as evidence of how ordering works. A chain on an ADVANCED SYSTEM can still be a good account, but say so, because the conversation is a replacement and not a first purchase.

Part 4. Is there a reason to talk now? (timing)

- ACTIVE TRIGGER: something dated in the last 12 months that changes how the chain orders. Examples: new stores or an acquisition, a new distribution centre, a system migration, a new head of supply chain, a published waste target.
- NO TRIGGER: you looked and found none.
- UNKNOWN: you could not check.

Name the trigger and its date.

Verdict

There are two verdicts. There is no middle one.

QUALIFIED: a BUYER, in scope, with a STRONG or MODERATE need, where a salesperson would have something specific to say.

NOT QUALIFIED: everything else.

If the evidence is mixed, decide which way it leans and say what would change your mind under main uncertainty.

Then set the tier, which tells the sales team where to start:
- TIER A: qualified, with a strong need and an active trigger.
- TIER B: qualified, without a trigger or with only a moderate need.
- TIER C: not qualified.

And state how much you trust your own answer (evidence_confidence): HIGH when the key facts come from the company's own pages, MEDIUM when they come from third parties, LOW when they are mostly inferred.

Finish the report with:
- Reason for the verdict: 2 to 4 sentences
- Strongest signal: the single most useful fact, with its source
- Main uncertainty: what you could not confirm
- Roles to contact: the job functions a salesperson should approach first, for example supply chain, replenishment, fresh merchandising, store operations. Name a person only when a public company page names them in that role.

Keep the whole report short. It is a qualification note, not a research file.

Machine-readable footer

After the report, end your answer with ONE line of minified JSON, on its own line, as the very last line. No code fence, nothing after it. The JSON must agree with the report. Every key is required; use "" for text you could not determine.

{"domain":"...","name":"...","status":"ok","qualification":"...","buyer_or_vendor":"...","buyer_vendor_reason":"...","account_tier":"...","need_strength":"...","current_approach":"...","timing":"...","evidence_confidence":"...","qualification_reason":"...","strongest_signal":"...","main_uncertainty":"...","roles_to_contact":"...","evidence_links":["https://..."]}

Allowed values (use these tokens exactly, upper case with underscores):

- qualification: QUALIFIED | NOT_QUALIFIED  (never MAYBE: decide which way a mixed case leans)
- buyer_or_vendor: BUYER | VENDOR | UNCLEAR
- account_tier: TIER_A | TIER_B | TIER_C
- need_strength: STRONG | MODERATE | WEAK | NOT_FOUND
- current_approach: MANUAL | BASIC_TOOLS | ADVANCED_SYSTEM | UNKNOWN
- timing: ACTIVE_TRIGGER | NO_TRIGGER | UNKNOWN
- evidence_confidence: HIGH | MEDIUM | LOW

Text fields:

- buyer_vendor_reason: one sentence on where the company's revenue comes from. Always fill it, for every verdict.
- qualification_reason: the reason for the verdict, 2 to 4 sentences.
- strongest_signal, main_uncertainty: one or two sentences each, from the report.
- roles_to_contact: job functions separated by "; ".
- evidence_links: up to 8 URLs you actually cited, most important first. Never invent a URL.

Consistency rules: QUALIFIED requires buyer_or_vendor = BUYER and account_tier TIER_A or TIER_B. NOT_QUALIFIED goes with TIER_C. The driver enforces the first and the last in code: a verdict that is not QUALIFIED is stored as TIER_C whatever you answer.

status is "ok" whenever you produced a verdict, including NOT_QUALIFIED for a company with no public footprint. Use {"domain":"...","name":"...","status":"error","error":"short reason"} only if you could not research at all (for example, no working search or fetch tools).
