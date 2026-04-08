"""
LLM-based tender analysis agent.

Pipeline per tender folder:
  1. Fetch structured metadata from gojep_tenders_current (fallback: gojep_tenders_all)
  2. Collect extracted document text from extracted_docs/*.json
  3. Split documents into chunks of ≤27,500 tokens at file boundaries
  4. Send each chunk to local LLM (default) or OpenRouter (if local disabled); merge results
  5. Validate the response has required fields
  6. Save result to Supabase (gojep_analysis_results) + local analysis.json sidecar

Failure handling:
  - On API error or parse failure: write .analysis_failed marker, skip on future runs
  - On rate limit or transient error: exponential backoff up to MAX_RETRIES
  - --reanalyse flag: delete existing sidecar + marker, force re-analysis
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import settings as config

logger = logging.getLogger(__name__)

# ── Constants ---------------------------------------------------------------

LOCAL_LLM_MAX_CHARS  = 110_000   # empirically validated: >110k chars → timeouts on CPU
OPENROUTER_MAX_CHARS = 400_000   # ~100k tokens — fits large cloud model context windows
MAX_CONTEXT_CHARS    = OPENROUTER_MAX_CHARS  # retained for backwards compat
LOCAL_LLM_TOKEN_LIMIT    = 27_500    # ~110k chars — empirically safe for local CPU model
OPENROUTER_TOKEN_LIMIT   = 120_000   # headroom below qwen free's 126k token context window
ANALYSIS_TIMEOUT  = 300          # seconds — local LLM only; OpenRouter uses its own timeout
RATE_LIMIT_DELAY  = 1            # seconds between successful calls
MAX_RETRIES       = 3            # max retries on transient errors
RETRY_BASE_DELAY  = 5            # seconds — doubles each retry

# Safety-net noise filter for the analysis pipeline.
# Primary filtering is handled upstream by pre_process_workflow/classify_documents.py
# before Docling extraction. This catches anything that slips through.
NOISE_FILE_PATTERNS = [
    "c4t_",          # XML portal exports — always machine-readable noise
    "sheet ",        # drawing sheet filenames: SHEET 1.pdf.json etc.
]

# Fields the LLM must return — warn if any are null
REQUIRED_FIELDS = [
    "contract_title", "procuring_entity", "contract_type", "scope_of_work",
    "eligibility_requirements", "mandatory_documents", "key_milestones",
]

# DB fields to fetch from Supabase
DB_SELECT_FIELDS = ",".join([
    "resource_id", "competition_unique_id", "title", "procuring_entity",
    "procurement_type", "services_subtype", "procurement_method",
    "evaluation_mechanism", "description", "detailed_description",
    "funding_source", "submission_deadline", "bid_opening_date",
    "site_visit_date", "clarification_period_end",
    "ppc_ncc_categories", "cpv_codes",
])

# DB metadata fields stored with db_ prefix in the analysis row
DB_META_FIELDS = [
    "title", "procuring_entity", "procurement_type", "services_subtype",
    "procurement_method", "evaluation_mechanism", "description",
    "detailed_description", "funding_source", "submission_deadline",
    "bid_opening_date", "site_visit_date", "clarification_period_end",
    "ppc_ncc_categories", "cpv_codes",
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


# ── Supabase metadata fetch -------------------------------------------------

def _extract_resource_id_from_folder(tender_folder: str, folder_name: str) -> Optional[str]:
    """
    Derive the DB resource_id from the extracted_docs PDF filename.
    Files are named {entity_code}_{resource_id}.pdf.json — e.g. 1000_9145038.pdf.json.
    Returns the long numeric resource_id, or None if not found.
    """
    extracted_dir = os.path.join(tender_folder, "extracted_docs")
    if not os.path.exists(extracted_dir):
        return None
    entity_prefix = folder_name.split("_")[0] + "_"
    for fname in os.listdir(extracted_dir):
        # Match: {entity_prefix}{digits}.pdf.json
        if fname.startswith(entity_prefix) and fname.endswith(".pdf.json"):
            stem = fname[len(entity_prefix):].replace(".pdf.json", "")
            if stem.isdigit():
                return stem
    return None


def _fetch_db_metadata(folder_name: str, db, tender_folder: str = "") -> Optional[Dict[str, Any]]:
    """
    Fetch tender metadata for a given folder.

    Lookup strategy (in order):
      1. competition_unique_id = folder_name with '/' (e.g. '1000/972' from '1000_972')
      2. resource_id = long numeric ID extracted from the PDF filename in extracted_docs
    Checks gojep_tenders_current then gojep_tenders_all for each strategy.
    """
    # Strategy 1: folder name with underscore → slash (DB format)
    competition_uid = folder_name.replace("_", "/", 1)

    # Strategy 2: resource_id from PDF filename in extracted_docs
    file_resource_id = _extract_resource_id_from_folder(tender_folder, folder_name) if tender_folder else None

    for table in [config.SUPABASE_TABLE_TENDERS_CURRENT, config.SUPABASE_TABLE_TENDERS_ALL]:
        try:
            result = db.supabase.table(table)\
                .select(DB_SELECT_FIELDS)\
                .eq("competition_unique_id", competition_uid)\
                .limit(1)\
                .execute()
            logger.debug(f"[{table}] competition_unique_id='{competition_uid}' → {len(result.data)} row(s)")
            if result.data:
                return result.data[0]
        except Exception as e:
            logger.warning(f"competition_unique_id lookup failed in {table} for '{competition_uid}': {e}")

        if file_resource_id:
            try:
                result = db.supabase.table(table)\
                    .select(DB_SELECT_FIELDS)\
                    .eq("resource_id", file_resource_id)\
                    .limit(1)\
                    .execute()
                logger.debug(f"[{table}] resource_id='{file_resource_id}' → {len(result.data)} row(s)")
                if result.data:
                    return result.data[0]
            except Exception as e:
                logger.warning(f"resource_id lookup failed in {table} for '{file_resource_id}': {e}")

    logger.warning(
        f"No DB metadata found for '{folder_name}' — "
        f"tried competition_unique_id='{competition_uid}', resource_id='{file_resource_id}'"
    )
    return None


def _format_metadata_header(meta: Dict[str, Any]) -> str:
    """Format DB metadata as a readable block to prepend to the document context."""
    def _val(key):
        v = meta.get(key)
        if v is None:
            return "Not stated"
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) if v else "None"
        return str(v)

    return "\n".join([
        "=== STRUCTURED METADATA FROM PROCUREMENT DATABASE ===",
        f"Title              : {_val('title')}",
        f"Procuring Entity   : {_val('procuring_entity')}",
        f"Procurement Type   : {_val('procurement_type')}",
        f"Services Subtype   : {_val('services_subtype')}",
        f"Procurement Method : {_val('procurement_method')}",
        f"Evaluation Method  : {_val('evaluation_mechanism')}",
        f"Funding Source     : {_val('funding_source')}",
        f"Submission Deadline: {_val('submission_deadline')}",
        f"Bid Opening Date   : {_val('bid_opening_date')}",
        f"Site Visit Date    : {_val('site_visit_date')}",
        f"Clarification End  : {_val('clarification_period_end')}",
        f"PPC/NCC Categories : {_val('ppc_ncc_categories')}",
        f"CPV Codes          : {_val('cpv_codes')}",
        f"Description        : {_val('description')}",
        f"Detailed Description: {_val('detailed_description')}",
        "=== END METADATA ===",
    ])


# ── Document context builders -----------------------------------------------

def _extract_text_from_json(data: Dict[str, Any]) -> str:
    """Pull plain text from an extracted_docs JSON file regardless of format."""
    content = data.get("content", {})
    parts = []

    if "chunks" in content:
        for chunk in content["chunks"]:
            t = chunk.get("text", "").strip()
            if t:
                parts.append(t)

    if "pages" in content:
        for page in content["pages"]:
            t = page.get("text", "").strip()
            if t:
                parts.append(t)
            for table in page.get("tables", []):
                row_texts = []
                for row in table:
                    cells = [str(c).strip() for c in row if c and str(c).strip()]
                    if cells:
                        row_texts.append(" | ".join(cells))
                if row_texts:
                    parts.append("\n".join(row_texts))

    if "markdown" in content and not parts:
        parts.append(content["markdown"])

    if isinstance(content, dict) and not parts:
        for sheet_name, rows in content.items():
            if sheet_name == "error":
                continue
            if isinstance(rows, list):
                parts.append(f"[Sheet: {sheet_name}]")
                for row in rows[:50]:
                    if isinstance(row, dict):
                        cells = [f"{k}: {v}" for k, v in row.items() if v and str(v).strip()]
                        if cells:
                            parts.append("  " + " | ".join(cells))

    if "slides" in content:
        for slide in content["slides"]:
            parts.extend(slide.get("texts", []))

    if "raw_text" in content:
        parts.append(content["raw_text"])

    return "\n\n".join(parts)


def _prioritise_files(json_files: List[str]) -> List[str]:
    """Sort so solicitation/bidding docs come first, XML/notices last."""
    priority_keywords = [
        "solicitation", "bidding document", "bid document", "rfp", "rfb", "itb",
        "terms of reference", "tor", "scope of service", "scope of work",
        "appendix", "addendum",
    ]
    def _score(path: str) -> int:
        name = os.path.basename(path).lower()
        for i, kw in enumerate(priority_keywords):
            if kw in name:
                return i
        if "competition notice" in name or "c4t_" in name:
            return 999
        return 100
    return sorted(json_files, key=_score)


def _is_noise_file(filename: str) -> bool:
    """Return True for files that add verbosity without useful LLM extraction signal."""
    name = filename.lower()
    return any(pattern in name for pattern in NOISE_FILE_PATTERNS)


def _split_text_into_parts(text: str, token_limit: int) -> List[str]:
    """
    Split a large text into parts of at most token_limit tokens.
    Prefers splitting at paragraph breaks (\n\n), falls back to line breaks,
    then hard-cuts at the char limit as a last resort.
    """
    # Use 2 chars/token (conservative) — Gemma 4's BPE tokenizer averages
    # 1.5-2 chars/token for DOC/raw_text content vs 3-4 for clean prose.
    char_limit = token_limit * 2
    if len(text) <= char_limit:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > char_limit:
        split_pos = remaining.rfind("\n\n", 0, char_limit)
        if split_pos == -1:
            split_pos = remaining.rfind("\n", 0, char_limit)
        if split_pos == -1:
            split_pos = char_limit
        parts.append(remaining[:split_pos].strip())
        remaining = remaining[split_pos:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _extract_file_texts(tender_folder: str) -> List[Tuple[str, str, int]]:
    """
    Load and extract text from all non-noise JSON files in extracted_docs/.
    Files larger than LOCAL_LLM_TOKEN_LIMIT are split into labelled sub-parts
    at paragraph boundaries before entering the chunking pipeline.
    Returns list of (source_filename, text, token_count) ordered by priority.
    """
    extracted_dir = os.path.join(tender_folder, "extracted_docs")
    if not os.path.exists(extracted_dir):
        return []

    json_files = _prioritise_files([
        os.path.join(extracted_dir, f)
        for f in os.listdir(extracted_dir)
        if f.endswith(".json") and not _is_noise_file(f)
    ])

    results = []
    for path in json_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        source_file = data.get("source_file", os.path.basename(path))
        text = _extract_text_from_json(data).strip()
        if not text:
            continue

        if len(text) // 2 <= LOCAL_LLM_TOKEN_LIMIT:
            results.append((source_file, text, len(text) // 2))
        else:
            parts = _split_text_into_parts(text, LOCAL_LLM_TOKEN_LIMIT)
            for i, part in enumerate(parts, start=1):
                label = f"{source_file} [part {i}/{len(parts)}]"
                results.append((label, part, len(part) // 2))

    return results


def _split_into_chunks(
    file_texts: List[Tuple[str, str, int]],
    chunk_token_limit: int,
) -> List[List[Tuple[str, str]]]:
    """
    Group (source_file, text) pairs into chunks where each chunk stays under
    chunk_token_limit tokens. Splits at file boundaries — never mid-document.
    Returns a list of chunks, each chunk being [(source_file, text), ...].
    """
    chunks: List[List[Tuple[str, str]]] = []
    current_chunk: List[Tuple[str, str]] = []
    current_tokens = 0

    for source_file, text, token_count in file_texts:
        if current_chunk and (current_tokens + token_count) > chunk_token_limit:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0
        current_chunk.append((source_file, text))
        current_tokens += token_count

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def build_chunk_contexts(
    tender_folder: str,
    db_meta: Optional[Dict[str, Any]],
) -> Tuple[List[Tuple[str, List[str]]], bool]:
    """
    Build one context string per chunk based on token count.

    Always chunks at LOCAL_LLM_TOKEN_LIMIT (27,500 tokens) and uses the local model.
    Each chunk is safe for the local CPU model regardless of total document size.
    Falls back to OpenRouter only when local LLM is disabled in config.

    Returns ([(context_str, source_files), ...], use_local).
    """
    meta_header = ""
    if db_meta:
        meta_header = _format_metadata_header(db_meta) + "\n\n=== TENDER DOCUMENTS ===\n"

    file_texts = _extract_file_texts(tender_folder)
    if not file_texts:
        return [], True

    use_local = config.ANALYSIS_USE_LOCAL_LLM
    chunks = _split_into_chunks(file_texts, LOCAL_LLM_TOKEN_LIMIT)
    num_chunks = len(chunks)

    chunk_contexts = []
    for i, chunk in enumerate(chunks, start=1):
        parts = [meta_header] if meta_header else []
        if num_chunks > 1:
            parts.append(f"[Part {i} of {num_chunks} — extract all fields visible in this part]\n")
        source_files = []
        for source_file, text in chunk:
            source_files.append(source_file)
            parts.append(f"\n\n=== FILE: {source_file} ===\n{text}")
        chunk_contexts.append(("".join(parts), source_files))

    return chunk_contexts, use_local


# ── Merge multi-chunk results -----------------------------------------------

def _merge_parsed_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merge extraction results from multiple chunks into one record.
      - Scalar fields: first non-null value wins (chunk 1 has the most complete header)
      - List fields:   union across all chunks, deduplicated by string representation
    """
    merged: Dict[str, Any] = {}
    seen: Dict[str, set] = {f: set() for f in LIST_FIELDS}

    for parsed in results:
        for field in ANALYSIS_OUTPUT_FIELDS:
            val = parsed.get(field)
            if val is None or val == "" or val == []:
                continue
            if field in LIST_FIELDS:
                items = val if isinstance(val, list) else [val]
                bucket = merged.setdefault(field, [])
                for item in items:
                    key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
                    if key not in seen[field]:
                        seen[field].add(key)
                        bucket.append(item)
            else:
                merged.setdefault(field, val)   # first non-null wins

    return merged


# ── LLM API call with retry -------------------------------------------------

def _call_llm(context: str, use_local: bool = True) -> str:
    """
    Send context to the appropriate LLM endpoint based on routing signal.

    use_local=True  → local Gemma 4 (fast, free, context ≤ LOCAL_LLM_MAX_CHARS)
    use_local=False → OpenRouter (handles large contexts that would timeout locally)

    Retries on transient 5xx errors and 429 rate limits with exponential backoff.
    """
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]

    # Use local LLM only when explicitly enabled in config AND context is small enough
    route_local = use_local and config.ANALYSIS_USE_LOCAL_LLM

    if route_local:
        url = config.LOCAL_LLM_URL
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": config.LOCAL_LLM_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": config.LOCAL_LLM_MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }
        call_timeout = ANALYSIS_TIMEOUT
    else:
        model_key = config.ANALYSIS_MODEL
        model_id = config.OPENROUTER_MODELS.get(model_key)
        if not model_id:
            raise ValueError(f"ANALYSIS_MODEL '{model_key}' not found in OPENROUTER_MODELS")
        url = config.OPENROUTER_URL
        headers = {
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/gojep-platform",
            "X-Title": "GOJEP Tender Analyser",
        }
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4000,
            "reasoning": {"enabled": True},
        }
        call_timeout = 120  # OpenRouter is network-bound, not CPU-bound

    delay = RETRY_BASE_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=call_timeout)
            if resp.status_code == 429:
                wait = delay + (attempt * 2)
                print(f"  -> Rate limited (429), waiting {wait}s before retry {attempt}/{MAX_RETRIES}...", flush=True)
                time.sleep(wait)
                delay *= 2
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                logger.warning(f"LLM returned empty content. Full response: {json.dumps(data)[:1000]}")
                raise RuntimeError("Model returned empty content")
            return content
        except requests.exceptions.Timeout:
            if attempt == MAX_RETRIES:
                raise
            print(f"  -> Timeout on attempt {attempt}/{MAX_RETRIES}, retrying in {delay}s...", flush=True)
            time.sleep(delay)
            delay *= 2
        except requests.exceptions.HTTPError as e:
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                print(f"  -> Server error ({resp.status_code}), retrying in {delay}s...", flush=True)
                time.sleep(delay)
                delay *= 2
            else:
                raise

    raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts")


# ── Parse + validate LLM response ------------------------------------------

def _parse_llm_response(raw: str) -> Dict[str, Any]:
    """Extract JSON from the LLM response, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse LLM JSON: {e}")
        return {"parse_error": str(e)}


def _validate_parsed(parsed: Dict[str, Any]) -> List[str]:
    """Check required fields are present and non-null. Returns list of warnings."""
    warnings = []
    if "parse_error" in parsed:
        warnings.append(f"LLM response was not valid JSON: {parsed['parse_error']}")
        return warnings
    for field in REQUIRED_FIELDS:
        val = parsed.get(field)
        if val is None or val == [] or val == "":
            warnings.append(f"Required field '{field}' is null/empty")
    return warnings


# ── Failure marker helpers --------------------------------------------------

def _write_failure_marker(path: str, reason: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"error": reason, "timestamp": datetime.now(timezone.utc).isoformat()}, f)
    except Exception:
        pass


def _read_failure_marker(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("error", "unknown")
    except Exception:
        return "unknown"


# ── Per-folder analysis -----------------------------------------------------

def _resource_id_from_folder(folder_name: str) -> str:
    """
    Best-effort resource_id from folder name.
    The folder is named after competition_unique_id (e.g. '1000_972') or resource_id.
    We return the full folder name as the analysis record's resource_id to avoid
    collisions between folders sharing the same first segment (e.g. 1000_972, 1000_973).
    The actual DB resource_id is populated from fetched metadata when available.
    """
    return folder_name


def analyse_tender_folder(tender_folder: str, db=None, reanalyse: bool = False) -> bool:
    """
    Analyse a single tender folder. Returns True if a new analysis was saved.

    reanalyse=True: delete existing sidecar and failure marker, force re-run.
    """
    folder_name = os.path.basename(tender_folder)
    resource_id = _resource_id_from_folder(folder_name)
    sidecar_path = os.path.join(tender_folder, "analysis.json")
    failed_marker_path = os.path.join(tender_folder, ".analysis_failed")

    if reanalyse:
        for p in [sidecar_path, failed_marker_path]:
            if os.path.exists(p):
                os.unlink(p)

    if os.path.exists(sidecar_path):
        logger.debug(f"Already analysed, skipping: {folder_name}")
        return False

    if os.path.exists(failed_marker_path):
        reason = _read_failure_marker(failed_marker_path)
        logger.debug(f"Previously failed ({reason}), skipping: {folder_name}")
        return False

    # 1. Fetch DB metadata (pass full folder_name — used as competition_unique_id lookup)
    db_meta = None
    if db:
        db_meta = _fetch_db_metadata(folder_name, db, tender_folder=tender_folder)

    # 2. Build chunk contexts
    chunk_contexts, use_local = build_chunk_contexts(tender_folder, db_meta)
    if not chunk_contexts:
        logger.warning(f"No context available for {folder_name}, skipping.")
        return False

    num_chunks = len(chunk_contexts)
    all_source_files: List[str] = []
    for _, src in chunk_contexts:
        for f in src:
            if f not in all_source_files:
                all_source_files.append(f)
    total_tokens = sum(len(ctx) // 4 for ctx, _ in chunk_contexts)
    route_label = "local" if use_local else "OpenRouter"

    print(
        f"  -> Analysing {folder_name} "
        f"({'with' if db_meta else 'without'} DB metadata, "
        f"~{total_tokens:,} tokens, {len(all_source_files)} doc files, "
        f"{num_chunks} chunk(s), route={route_label})...",
        flush=True,
    )

    # 3. Call LLM per chunk
    parsed_results: List[Dict[str, Any]] = []
    raw_responses: List[str] = []
    for i, (context, _) in enumerate(chunk_contexts, start=1):
        if num_chunks > 1:
            print(f"  -> Chunk {i}/{num_chunks} (~{len(context) // 4:,} tokens)...", flush=True)
        try:
            raw_response = _call_llm(context, use_local=use_local)
        except Exception as e:
            msg = str(e)
            logger.error(f"LLM API call failed for {folder_name} chunk {i}/{num_chunks}: {msg}")
            print(f"  -> ERROR (API, chunk {i}): {msg}", flush=True)
            _write_failure_marker(failed_marker_path, f"API error chunk {i}: {msg}")
            return False
        raw_responses.append(raw_response)
        parsed_results.append(_parse_llm_response(raw_response))

    # 4. Merge chunks + validate
    parsed = _merge_parsed_results(parsed_results) if num_chunks > 1 else parsed_results[0]
    warnings = _validate_parsed(parsed)
    if warnings:
        for w in warnings:
            print(f"  -> WARN: {w}", flush=True)
            logger.warning(f"{folder_name}: {w}")

    # If response couldn't be parsed at all, write failure marker
    if "parse_error" in parsed and len(parsed) == 1:
        msg = parsed["parse_error"]
        _write_failure_marker(failed_marker_path, f"Parse error: {msg}")
        return False

    # 5. Build result record
    # Use the actual resource_id from DB metadata if found; otherwise fall back to folder_name
    db_resource_id = db_meta.get("resource_id") if db_meta else None
    result: Dict[str, Any] = {
        "resource_id": db_resource_id or resource_id,
        "tender_folder": folder_name,
        "competition_unique_id": db_meta.get("competition_unique_id") if db_meta else folder_name,
        "source_files": all_source_files,
        "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_llm_response": raw_responses if num_chunks > 1 else raw_responses[0],
        "validation_warnings": warnings,
    }
    for field in ANALYSIS_OUTPUT_FIELDS:
        result[field] = parsed.get(field)
    if db_meta:
        for field in DB_META_FIELDS:
            result[f"db_{field}"] = db_meta.get(field)

    # 6. Save local sidecar
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save sidecar for {folder_name}: {e}")
        return False

    # 7. Push to Supabase
    if db:
        try:
            db_row: Dict[str, Any] = {
                "resource_id": resource_id,
                "tender_folder": folder_name,
                "competition_unique_id": result.get("competition_unique_id"),
                "source_files": all_source_files,
                "analysis_timestamp": result["analysis_timestamp"],
                "raw_llm_response": raw_responses if num_chunks > 1 else raw_responses[0],
            }
            for field in ANALYSIS_OUTPUT_FIELDS:
                val = result.get(field)
                db_row[field] = val if field not in LIST_FIELDS else (
                    val if isinstance(val, list) else ([] if val is None else [val])
                )
            if db_meta:
                for field in DB_META_FIELDS:
                    db_row[f"db_{field}"] = db_meta.get(field)

            db.supabase.table(config.SUPABASE_TABLE_ANALYSIS_RESULTS)\
                .upsert(db_row, on_conflict="tender_folder")\
                .execute()
            logger.debug(f"Upserted analysis for {folder_name}")
        except Exception as e:
            logger.warning(f"Supabase upsert failed for {folder_name}: {e} (local sidecar saved)")

    title = parsed.get("contract_title") or parsed.get("procuring_entity") or folder_name
    ctype = parsed.get("contract_type", "?")
    print(f"  -> Done: [{ctype}] {title}", flush=True)
    return True


# ── Runner ------------------------------------------------------------------

def run_tender_analysis(limit: int = 0, reanalyse: bool = False) -> Dict[str, Any]:
    """
    Walk all tender document folders and analyse each with the LLM.
    limit=0 processes all. reanalyse=True forces re-analysis of already-done folders.
    Returns summary stats + per-folder log.
    """
    docs_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "documents")
    if not os.path.exists(docs_dir):
        logger.warning(f"Documents directory not found: {docs_dir}")
        return {"total": 0, "analysed": 0, "skipped": 0, "errors": 0, "warnings": 0}

    # Initialise DB client
    db = None
    try:
        from db.supabase_client import SupabaseClient
        db = SupabaseClient()
    except Exception as e:
        logger.warning(f"Supabase unavailable — results saved locally only: {e}")

    tender_folders = sorted([
        os.path.join(docs_dir, d)
        for d in os.listdir(docs_dir)
        if os.path.isdir(os.path.join(docs_dir, d))
    ])
    if limit > 0:
        tender_folders = tender_folders[:limit]

    total = len(tender_folders)
    analysed = 0
    skipped = 0
    errors = 0
    total_warnings = 0
    log_entries = []

    print(f"Found {total} tender folders to analyse.", flush=True)
    if reanalyse:
        print("  (--reanalyse active: existing analyses will be overwritten)", flush=True)

    run_start = datetime.now(timezone.utc)

    for i, folder in enumerate(tender_folders, start=1):
        folder_name = os.path.basename(folder)
        sidecar_path = os.path.join(folder, "analysis.json")
        failed_path = os.path.join(folder, ".analysis_failed")

        print(f"[{i}/{total}] {folder_name}", flush=True)

        # Quick skip without loading anything
        if not reanalyse:
            if os.path.exists(sidecar_path):
                skipped += 1
                continue
            if os.path.exists(failed_path):
                reason = _read_failure_marker(failed_path)
                print(f"  -> Previously failed ({reason}), skipping", flush=True)
                skipped += 1
                continue

        try:
            if analyse_tender_folder(folder, db=db, reanalyse=reanalyse):
                analysed += 1
                # Count warnings from the sidecar
                try:
                    with open(sidecar_path, encoding="utf-8") as f:
                        w = len(json.load(f).get("validation_warnings", []))
                        total_warnings += w
                except Exception:
                    pass
                log_entries.append({"folder": folder_name, "status": "analysed"})
                time.sleep(RATE_LIMIT_DELAY)
            else:
                skipped += 1
                log_entries.append({"folder": folder_name, "status": "skipped"})
        except Exception as e:
            print(f"  -> ERROR: {e}", flush=True)
            logger.error(f"Analysis failed for {folder}: {e}")
            _write_failure_marker(failed_path, str(e))
            errors += 1
            log_entries.append({"folder": folder_name, "status": "error", "error": str(e)})

    # Write per-run log
    _write_run_log(run_start, total, analysed, skipped, errors, total_warnings, log_entries)

    return {
        "total": total,
        "analysed": analysed,
        "skipped": skipped,
        "errors": errors,
        "warnings": total_warnings,
    }


def _write_run_log(
    run_start: datetime,
    total: int,
    analysed: int,
    skipped: int,
    errors: int,
    warnings: int,
    entries: List[Dict],
) -> None:
    """Write a timestamped run log to data/logs/."""
    log_dir = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = run_start.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"analysis_{timestamp}.json")
    payload = {
        "run_start": run_start.isoformat(),
        "run_end": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total,
            "analysed": analysed,
            "skipped": skipped,
            "errors": errors,
            "warnings": warnings,
        },
        "folders": entries,
    }
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nRun log saved: {log_path}", flush=True)
    except Exception as e:
        logger.warning(f"Could not write run log: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_tender_analysis()
    print(f"\nAnalysis complete: {result}")
