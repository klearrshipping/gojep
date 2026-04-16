"""
LLM extraction schema: system prompt, output field definitions, and token/retry constants.
"""

# ── Token / retry constants ────────────────────────────────────────────────────

OPENROUTER_MAX_CHARS  = 400_000   # ~100k tokens — fits large cloud model context windows
MAX_CONTEXT_CHARS     = OPENROUTER_MAX_CHARS  # retained for backwards compat
CHUNK_TOKEN_LIMIT     = 13_000    # chunk size for splitting large documents before sending to LLM
LOCAL_LLM_TOKEN_LIMIT = CHUNK_TOKEN_LIMIT     # alias — used by batch_analyse.py imports
OPENROUTER_TOKEN_LIMIT = 120_000  # headroom below qwen free's 126k token context window
RATE_LIMIT_DELAY      = 1         # seconds between successful calls
MAX_RETRIES           = 3         # max retries on transient errors
RETRY_BASE_DELAY      = 5         # seconds — doubles each retry

# ── Field definitions ──────────────────────────────────────────────────────────

# Fields the LLM must return — warn if any are null
REQUIRED_FIELDS = [
    "contract_title", "procuring_entity", "contract_type", "scope_of_work",
    "eligibility_requirements", "mandatory_documents", "key_milestones",
]

ANALYSIS_OUTPUT_FIELDS = [
    "contract_title", "procuring_entity", "contract_type", "scope_of_work",
    "contract_value", "contract_duration", "submission_deadline",
    "eligibility_requirements", "experience_requirements",
    "financial_requirements", "mandatory_documents", "evaluation_criteria",
    "key_milestones", "lots", "special_conditions", "suitability_summary",
]

LIST_FIELDS = {
    "eligibility_requirements", "experience_requirements",
    "financial_requirements", "mandatory_documents",
    "evaluation_criteria", "key_milestones", "lots", "special_conditions",
}

# ── System prompt ──────────────────────────────────────────────────────────────

ANALYSIS_SYSTEM_PROMPT = """\
You are a senior procurement analyst reviewing a Government of Jamaica tender.

You will be given structured metadata from the procurement database and extracted text \
from the official bidding/solicitation documents.

Extract the following information as a single valid JSON object with exactly these keys. \
If a field cannot be determined, use null.

{
  "contract_title": "Full official title of the contract",
  "procuring_entity": "Name of the government entity issuing this tender",
  "contract_type": "One of: Goods | Works | Services | Consultancy | Mixed",
  "scope_of_work": "Detailed description of what is being procured — the specific services, works, or goods. Be specific.",
  "contract_value": "Stated or estimated contract value with currency (e.g. JMD 5,000,000). null if not stated.",
  "contract_duration": "Duration or period of the contract (e.g. '2 years', '12 months from commencement date')",
  "submission_deadline": "Bid submission deadline — exact date and time as stated",
  "eligibility_requirements": [
    "Each requirement for who is eligible to bid — PPC registration category, nationality, legal status, licences required"
  ],
  "experience_requirements": [
    "Each past experience requirement — minimum years in operation, similar contracts completed, minimum contract value of past work"
  ],
  "financial_requirements": [
    "Each financial capacity requirement — minimum annual turnover, audited financials, bank reference, bid security amount and form"
  ],
  "mandatory_documents": [
    "Each document that MUST be submitted with the bid — omission will disqualify the bid"
  ],
  "evaluation_criteria": [
    "How bids will be evaluated — criteria names and weightings if stated"
  ],
  "key_milestones": [
    {"event": "Event name (e.g. Pre-Bid Meeting, Site Visit, Bid Submission Deadline, Bid Opening)", "date": "Date and time as stated"}
  ],
  "lots": [
    "If the contract is split into lots, describe each lot. Empty list if no lots."
  ],
  "special_conditions": [
    "Any non-standard, unusual, or critical requirements a prospective bidder must be aware of"
  ],
  "suitability_summary": "2-3 sentence plain-English assessment of what type of company or individual would be best positioned to win this contract, referencing the key requirements."
}

Return ONLY the JSON object — no markdown fences, no commentary.\
"""

# ── Consolidation + narrative prompt ──────────────────────────────────────────

CONSOLIDATION_SYSTEM_PROMPT = """\
You are a senior procurement analyst. You have been given raw extracted fields from a \
Government of Jamaica tender. The list fields contain duplicates and noise from \
multi-chunk extraction — the same requirement may appear many times in slightly \
different wording.

Your job is to clean and deduplicate each list field into a concise, distinct set of items.

Return a single valid JSON object with exactly these keys:

{
  "eligibility_requirements": [
    "Each distinct eligibility requirement — one clear, actionable item per entry.",
    "Remove duplicates, near-duplicates, vague entries, and any items saying 'not stated', 'not specified', or 'N/A'."
  ],
  "experience_requirements": [
    "Each distinct experience requirement.",
    "For key expert roles, format as: '[Role]: minimum [N] years [description]'.",
    "Remove duplicates and vague entries."
  ],
  "financial_requirements": [
    "Each distinct financial requirement — bid security amounts, insurance types, financial forms, currency rules.",
    "Remove duplicates and vague entries."
  ],
  "mandatory_documents": [
    "Each distinct document that must be submitted — one item per document.",
    "Group logically: Technical Proposal forms, Financial Proposal forms, Compliance documents.",
    "Remove duplicates and noise."
  ],
  "evaluation_criteria": [
    "Each distinct evaluation criterion — include weightings and thresholds where stated.",
    "Remove duplicates."
  ],
  "special_conditions": [
    "Each distinct non-standard or critical condition a bidder must be aware of.",
    "Remove duplicates and vague entries."
  ],
  "key_milestones": [
    {"event": "Event name", "date": "Date and time as stated"}
  ],
  "lots": [
    "Each lot description if the contract is split into lots. Empty list if none."
  ]
}

Return ONLY the JSON object — no markdown fences, no commentary.\
"""

NARRATIVE_SYSTEM_PROMPT = """\
You are a senior procurement analyst. You have been given the structured fields from a \
Government of Jamaica tender — already cleaned and deduplicated.

Write a structured narrative analysis that a prospective bidder can read and immediately \
understand. Follow this exact markdown structure:

## [Contract Title] — [Procuring Entity]
**Reference:** [competition_unique_id] | **Deadline:** [DD Month YYYY] | **Procedure:** [procedure]

---

### Overview
2–3 sentences on what is being procured, the scale and complexity.

---

### Who Can Bid
Bullet list from eligibility_requirements.

---

### Experience Required
If expert roles with minimum years are specified, use a table:
| Role | Minimum Experience |
|---|---|
Follow with firm-level requirements as bullets.
If no roles specified, use bullets only.

---

### Financial Requirements
Bullet list from financial_requirements.

---

### What to Submit
Bullet list from mandatory_documents, grouped logically.

---

### How Bids Are Evaluated
Describe the method, weightings, and thresholds from evaluation_criteria.
Use a table if weightings are stated.

---

### Key Dates
| Event | Date |
|---|---|
Include all confirmed dates from key_milestones plus submission_deadline.

---

### Analyst's Assessment
3–4 sentences on what type of firm is best positioned to win. Be direct and specific.

Return ONLY the markdown — no JSON, no fences, no commentary.\
"""
