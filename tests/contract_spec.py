"""Canonical cross-boundary contracts (queue line, verdict JSON, CRM option values).
Tests check each side against this file, so a mismatch shows up on whichever side drifted."""

QUEUE_KEYS = {"domain", "name", "apollo_org_id", "apollo_account_id", "industry", "headcount",
              "tech", "funding", "job_titles", "status"}

# Tokens the parser accepts for each enumerated verdict key.
VERDICT_ENUMS = {
    "status": {"ok", "error"},
    "qualification": {"QUALIFIED", "MAYBE", "NOT_QUALIFIED"},
    "buyer_or_vendor": {"BUYER", "VENDOR", "UNCLEAR"},
    "account_tier": {"TIER_A", "TIER_B", "TIER_C"},
    "need_strength": {"STRONG", "MODERATE", "WEAK", "NOT_FOUND"},
    "current_approach": {"MANUAL", "BASIC_TOOLS", "ADVANCED_SYSTEM", "UNKNOWN"},
    "timing": {"ACTIVE_TRIGGER", "NO_TRIGGER", "UNKNOWN"},
    "evidence_confidence": {"HIGH", "MEDIUM", "LOW"},
}
# Accepted by the parser so that the driver can downgrade them, but never asked of the model:
# the prompt and the JSON schema offer a binary verdict.
ACCEPTED_NOT_ASKED = {"qualification": {"MAYBE"}}

VERDICT_TEXT = {"domain", "name", "buyer_vendor_reason", "qualification_reason", "strongest_signal",
                "main_uncertainty", "roles_to_contact"}
VERDICT_KEYS = set(VERDICT_ENUMS) | VERDICT_TEXT | {"evidence_links"}

# Allowed option values of the HubSpot enumeration properties the writer fills
HS_ENUMS = {
    "sourcing_qualification_status": {"Not Reviewed", "Qualified", "Needs Review", "Not Qualified"},
    "sourcing_account_tier": {"Tier A", "Tier B", "Tier C"},
    "sourcing_need_strength": {"Strong", "Moderate", "Weak", "Not Found"},
    "sourcing_current_approach": {"Manual", "Basic Tools", "Advanced System", "Unknown"},
    "sourcing_timing": {"Active Trigger", "No Trigger", "Unknown"},
    "sourcing_evidence_confidence": {"High", "Medium", "Low"},
}


def good_verdict(**over):
    v = {
        "domain": "acme.example", "name": "Acme", "status": "ok",
        "qualification": "QUALIFIED", "buyer_or_vendor": "BUYER",
        "buyer_vendor_reason": "Shoppers pay for groceries in its own stores",
        "account_tier": "TIER_B", "need_strength": "STRONG",
        "current_approach": "BASIC_TOOLS", "timing": "NO_TRIGGER", "evidence_confidence": "MEDIUM",
        "qualification_reason": "Large fresh offer, orders placed per store", "strongest_signal": "Job post: Replenishment Analyst",
        "main_uncertainty": "Whether a forecasting system is already in use",
        "roles_to_contact": "Supply chain; Merchandising",
        "evidence_links": ["https://acme.example/careers/1", "https://acme.example/news/ordering"],
    }
    v.update(over)
    return v
