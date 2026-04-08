# ---
# Google Colab Tender Analysis Script — Gemma 4 26B (MoE, 4-bit)
# ---
# Paste this entire file into a Google Colab cell (or run cell-by-cell).
#
# Prerequisites:
#   1. Runtime -> Change runtime type -> T4 GPU (free) or A100 (Pro)
#   2. Google Drive contains extracted_docs folders at:
#      My Drive/gojep_data/tenders/documents/<folder_name>/extracted_docs/
#   3. Set SUPABASE_URL and SUPABASE_KEY below if you want DB saves.
#
# Output per folder:  <folder>/analysis.json  (same format as Modal pipeline)
# Skips folders that already have analysis.json or .analysis_failed markers.
# ---

import os, json, re, time
from datetime import datetime, timezone

# ── 1. INSTALL ────────────────────────────────────────────────────────────────
# Run this block first, then restart the runtime when prompted.

import os, re
if "COLAB_" not in "".join(os.environ.keys()):
    raise SystemExit("This script must run in Google Colab")

import torch
os.system("pip install -q huggingface_hub")
# Compile llama-cpp-python from source with CUDA — guarantees GPU inference
os.system('CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python --upgrade --force-reinstall --no-cache-dir -q')
print("Installation complete.")
print(f"CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")


# ── 2. CONFIGURATION ──────────────────────────────────────────────────────────

DRIVE_MOUNT_PATH = "/content/drive"
INPUT_DRIVE_DIR  = "My Drive/gojep_data/tenders/documents"   # rclone: gdrive:gojep_data

# Supabase — set to None to skip DB saves and only write local analysis.json
SUPABASE_URL = None   # e.g. "https://xxxx.supabase.co"
SUPABASE_KEY = None   # service_role key

# Processing controls
FORCE_REANALYSE = False   # True = re-run even if analysis.json exists
SKIP_FAILED     = False   # True = skip folders with .analysis_failed marker
SINGLE_FOLDER   = ""      # Set to a folder name (e.g. "1000_972") to run only that one

# Model — Gemma 4 2B GGUF Q4_K_M (~3.1GB), leaves 11GB headroom for inference
MODEL_NAME       = "unsloth/gemma-4-E2B-it-GGUF"
GGUF_NAME        = "gemma-4-E2B-it-Q4_K_M.gguf"
MAX_NEW_TOKENS   = 2_000
TEMPERATURE      = 0.1

# Chunking
TOKEN_LIMIT      = 6_000    # tokens per chunk (~12k chars at 2 chars/token)
CHARS_PER_TOKEN  = 2

# Supabase table names (must match config/settings.py)
TABLE_ANALYSIS_RESULTS  = "gojep_analysis_results"
TABLE_CONTRACT_ANALYSIS = "gojep_contract_analysis"


# ── 3. MOUNT DRIVE ────────────────────────────────────────────────────────────

if not os.path.exists(DRIVE_MOUNT_PATH):
    from google.colab import drive
    drive.mount(DRIVE_MOUNT_PATH)

DOCS_DIR = os.path.join(DRIVE_MOUNT_PATH, INPUT_DRIVE_DIR)
print(f"Documents path: {DOCS_DIR}")
print(f"Exists: {os.path.exists(DOCS_DIR)}")


# ── 4. LOAD MODEL ────────────────────────────────────────────────────────────

from llama_cpp import Llama

print(f"Loading {MODEL_NAME} ({GGUF_NAME}) ...")
model = Llama.from_pretrained(
    repo_id=MODEL_NAME,
    filename=GGUF_NAME,
    n_gpu_layers=-1,
    n_ctx=8192,
    verbose=True,
)
print("Model ready.")


# ── 5. SYSTEM PROMPT (identical to Modal pipeline) ────────────────────────────

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

# Noise file patterns to exclude (mirrors analyse_tender.py)
NOISE_FILE_PATTERNS = ["c4t_", "sheet "]

# Fields stored with db_ prefix
DB_META_FIELDS = [
    "title", "procuring_entity", "procurement_type", "services_subtype",
    "procurement_method", "evaluation_mechanism", "description",
    "detailed_description", "funding_source", "submission_deadline",
    "bid_opening_date", "site_visit_date", "clarification_period_end",
    "ppc_ncc_categories", "cpv_codes",
]

LIST_FIELDS = {
    "eligibility_requirements", "experience_requirements",
    "financial_requirements", "mandatory_documents",
    "evaluation_criteria", "key_milestones", "lots", "special_conditions",
}


# ── 6. HELPERS ────────────────────────────────────────────────────────────────

def _is_noise(filename):
    name = filename.lower()
    return any(p in name for p in NOISE_FILE_PATTERNS)


def _extract_text(data):
    """Pull plain text from an extracted_docs JSON file (all format types)."""
    content = data.get("content", {})
    parts = []
    if "chunks" in content:
        for c in content["chunks"]:
            t = c.get("text", "").strip()
            if t: parts.append(t)
    if "pages" in content:
        for pg in content["pages"]:
            t = pg.get("text", "").strip()
            if t: parts.append(t)
            for table in pg.get("tables", []):
                rows = [" | ".join(str(c).strip() for c in row if str(c).strip()) for row in table]
                if rows: parts.append("\n".join(rows))
    if "markdown" in content and not parts:
        parts.append(content["markdown"])
    if isinstance(content, dict) and not parts:
        for sheet_name, rows in content.items():
            if sheet_name == "error": continue
            if isinstance(rows, list):
                parts.append(f"[Sheet: {sheet_name}]")
                for row in rows[:50]:
                    if isinstance(row, dict):
                        cells = [f"{k}: {v}" for k, v in row.items() if v and str(v).strip()]
                        if cells: parts.append("  " + " | ".join(cells))
    if "slides" in content:
        for slide in content["slides"]:
            parts.extend(slide.get("texts", []))
    if "raw_text" in content:
        parts.append(content["raw_text"])
    return "\n\n".join(parts)


def _prioritise_files(paths):
    priority_keywords = [
        "solicitation", "bidding document", "bid document", "rfp", "rfb", "itb",
        "terms of reference", "tor", "scope of service", "scope of work",
        "appendix", "addendum",
    ]
    def _score(p):
        name = os.path.basename(p).lower()
        for i, kw in enumerate(priority_keywords):
            if kw in name: return i
        if "competition notice" in name or "c4t_" in name: return 999
        return 100
    return sorted(paths, key=_score)


def _split_text(text, token_limit):
    char_limit = token_limit * CHARS_PER_TOKEN
    if len(text) <= char_limit:
        return [text]
    parts, remaining = [], text
    while len(remaining) > char_limit:
        pos = remaining.rfind("\n\n", 0, char_limit)
        if pos == -1: pos = remaining.rfind("\n", 0, char_limit)
        if pos == -1: pos = char_limit
        parts.append(remaining[:pos].strip())
        remaining = remaining[pos:].strip()
    if remaining: parts.append(remaining)
    return parts


def _load_file_texts(folder_path):
    """Load all non-noise extracted docs, split large files into sub-parts."""
    extracted_dir = os.path.join(folder_path, "extracted_docs")
    if not os.path.exists(extracted_dir):
        return []
    json_files = _prioritise_files([
        os.path.join(extracted_dir, f)
        for f in os.listdir(extracted_dir)
        if f.endswith(".json") and not _is_noise(f)
    ])
    results = []
    for path in json_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        source = data.get("source_file", os.path.basename(path))
        text = _extract_text(data).strip()
        if not text: continue
        if len(text) // CHARS_PER_TOKEN <= TOKEN_LIMIT:
            results.append((source, text))
        else:
            parts = _split_text(text, TOKEN_LIMIT)
            for i, part in enumerate(parts, 1):
                results.append((f"{source} [part {i}/{len(parts)}]", part))
    return results


def _build_chunks(file_texts, meta_header=""):
    """Group file texts into chunks that stay under TOKEN_LIMIT."""
    chunks, current, current_tokens = [], [], 0
    for source, text in file_texts:
        tokens = len(text) // CHARS_PER_TOKEN
        if current and current_tokens + tokens > TOKEN_LIMIT:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append((source, text))
        current_tokens += tokens
    if current:
        chunks.append(current)
    # Build prompt strings
    prompts = []
    n = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        parts = [meta_header] if meta_header else []
        if n > 1:
            parts.append(f"[Part {i} of {n} — extract all fields visible in this part]\n")
        for source, text in chunk:
            parts.append(f"\n\n=== FILE: {source} ===\n{text}")
        prompts.append("".join(parts))
    return prompts


def _format_meta_header(meta):
    def v(k):
        val = meta.get(k)
        if val is None: return "Not stated"
        if isinstance(val, list): return ", ".join(str(x) for x in val) if val else "None"
        return str(val)
    return "\n".join([
        "=== STRUCTURED METADATA FROM PROCUREMENT DATABASE ===",
        f"Title              : {v('title')}",
        f"Procuring Entity   : {v('procuring_entity')}",
        f"Procurement Type   : {v('procurement_type')}",
        f"Services Subtype   : {v('services_subtype')}",
        f"Procurement Method : {v('procurement_method')}",
        f"Evaluation Method  : {v('evaluation_mechanism')}",
        f"Funding Source     : {v('funding_source')}",
        f"Submission Deadline: {v('submission_deadline')}",
        f"Bid Opening Date   : {v('bid_opening_date')}",
        f"Site Visit Date    : {v('site_visit_date')}",
        f"Clarification End  : {v('clarification_period_end')}",
        f"PPC/NCC Categories : {v('ppc_ncc_categories')}",
        f"CPV Codes          : {v('cpv_codes')}",
        f"Description        : {v('description')}",
        f"Detailed Description: {v('detailed_description')}",
        "=== END METADATA ===",
        "",
        "=== TENDER DOCUMENTS ===",
    ])


def _parse_llm_json(raw):
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except: pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return None


def _merge_results(chunks):
    """Merge multiple chunk results, preferring non-null values from earlier chunks."""
    if len(chunks) == 1:
        return chunks[0]
    merged = {}
    for chunk in chunks:
        for k, v in chunk.items():
            if k not in merged or merged[k] is None:
                merged[k] = v
            elif isinstance(v, list) and isinstance(merged[k], list):
                seen = {json.dumps(x, sort_keys=True) for x in merged[k]}
                for item in v:
                    if json.dumps(item, sort_keys=True) not in seen:
                        merged[k].append(item)
                        seen.add(json.dumps(item, sort_keys=True))
    return merged


def _run_inference(prompt):
    """Run a single prompt through the model and return the raw text response."""
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user",   "content": prompt},
    ]
    response = model.create_chat_completion(
        messages=messages,
        max_tokens=MAX_NEW_TOKENS,
        temperature=TEMPERATURE,
    )
    return response["choices"][0]["message"]["content"]


# ── 7. SUPABASE (optional) ────────────────────────────────────────────────────

_supabase = None

def _get_db():
    global _supabase
    if _supabase is not None:
        return _supabase
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        os.system("pip install -q supabase")
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Supabase connected.")
    except Exception as e:
        print(f"Supabase connection failed: {e}")
        _supabase = None
    return _supabase


def _fetch_db_meta(folder_name):
    db = _get_db()
    if not db: return None
    competition_uid = folder_name.replace("_", "/", 1)
    select = (
        "resource_id,competition_unique_id,title,procuring_entity,"
        "procurement_type,services_subtype,procurement_method,evaluation_mechanism,"
        "description,detailed_description,funding_source,submission_deadline,"
        "bid_opening_date,site_visit_date,clarification_period_end,"
        "ppc_ncc_categories,cpv_codes"
    )
    for table in ["gojep_tenders_current", "gojep_tenders_all"]:
        try:
            r = db.table(table).select(select).eq("competition_unique_id", competition_uid).limit(1).execute()
            if r.data: return r.data[0]
        except Exception as e:
            print(f"  DB lookup failed ({table}): {e}")
    return None


def _save_to_db(folder_name, result, db_meta):
    db = _get_db()
    if not db: return
    now = datetime.now(timezone.utc).isoformat()
    competition_uid = folder_name.replace("_", "/", 1)
    resource_id = db_meta.get("resource_id") if db_meta else None
    row = {
        "tender_folder": folder_name,
        "competition_unique_id": competition_uid,
        "analysis_timestamp": now,
        **{k: v for k, v in result.items() if k not in ("folder", "analysed_at", "analysis_timestamp")},
    }
    if resource_id:
        row["resource_id"] = resource_id
    try:
        db.table(TABLE_ANALYSIS_RESULTS).upsert(row, on_conflict="tender_folder").execute()
        print(f"  -> DB: saved to {TABLE_ANALYSIS_RESULTS}")
    except Exception as e:
        print(f"  -> DB: save failed: {e}")
    try:
        db.table(TABLE_CONTRACT_ANALYSIS)\
          .update({"analysis_timestamp": now})\
          .eq("competition_unique_id", competition_uid)\
          .execute()
    except Exception as e:
        print(f"  -> DB: contract_analysis timestamp update failed: {e}")


# ── 8. MAIN RUNNER ────────────────────────────────────────────────────────────

def _has_extracted_docs(folder_path):
    d = os.path.join(folder_path, "extracted_docs")
    return os.path.isdir(d) and any(f.endswith(".json") for f in os.listdir(d))


def run_analysis():
    if not os.path.exists(DOCS_DIR):
        print(f"ERROR: Documents directory not found: {DOCS_DIR}")
        return

    all_folders = sorted([
        d for d in os.listdir(DOCS_DIR)
        if os.path.isdir(os.path.join(DOCS_DIR, d))
    ])

    if SINGLE_FOLDER:
        all_folders = [f for f in all_folders if f == SINGLE_FOLDER]
        if not all_folders:
            print(f"Folder '{SINGLE_FOLDER}' not found.")
            return

    # Filter to pending folders
    pending = []
    for folder_name in all_folders:
        folder_path = os.path.join(DOCS_DIR, folder_name)
        sidecar = os.path.join(folder_path, "analysis.json")
        failed  = os.path.join(folder_path, ".analysis_failed")
        if not FORCE_REANALYSE and os.path.exists(sidecar):
            continue
        if SKIP_FAILED and not FORCE_REANALYSE and os.path.exists(failed):
            continue
        if not _has_extracted_docs(folder_path):
            continue
        pending.append((folder_name, folder_path))

    total = len(pending)
    print(f"\nPending: {total} folder(s)\n")
    if total == 0:
        print("Nothing to analyse.")
        return

    done_count, failed_count = 0, 0
    run_start = time.time()

    for idx, (folder_name, folder_path) in enumerate(pending, 1):
        elapsed_total = int(time.time() - run_start)
        print(f"\n[{idx}/{total}] {folder_name}  (total elapsed: {elapsed_total//60}m {elapsed_total%60}s)", flush=True)

        if FORCE_REANALYSE:
            for marker in ["analysis.json", ".analysis_failed"]:
                p = os.path.join(folder_path, marker)
                if os.path.exists(p): os.unlink(p)

        # Load DB metadata
        db_meta = _fetch_db_meta(folder_name)
        meta_header = _format_meta_header(db_meta) if db_meta else ""

        # Load files and build prompt chunks
        file_texts = _load_file_texts(folder_path)
        if not file_texts:
            print(f"  No extractable content — skipping")
            continue

        prompts = _build_chunks(file_texts, meta_header)
        num_chunks = len(prompts)
        print(f"  {num_chunks} chunk(s), {'with' if db_meta else 'no'} DB metadata", flush=True)

        # Run inference on each chunk
        parsed_chunks = []
        folder_start = time.time()
        failed = False

        for ci, prompt in enumerate(prompts, 1):
            print(f"  chunk {ci}/{num_chunks}...", end="", flush=True)
            try:
                raw = _run_inference(prompt)
                parsed = _parse_llm_json(raw)
                if parsed:
                    parsed_chunks.append(parsed)
                    print(f" ok", flush=True)
                else:
                    print(f" JSON parse failed", flush=True)
                    print(f"  raw: {raw[:200]}")
            except Exception as e:
                import traceback
                print(f" ERROR: {e}", flush=True)
                traceback.print_exc()
                failed = True
                break

        if failed or not parsed_chunks:
            open(os.path.join(folder_path, ".analysis_failed"), "w").close()
            failed_count += 1
            print(f"  -> FAILED")
            continue

        # Merge multi-chunk results
        merged = _merge_results(parsed_chunks)

        # Attach metadata
        now = datetime.now(timezone.utc).isoformat()
        if db_meta:
            for field in DB_META_FIELDS:
                merged[f"db_{field}"] = db_meta.get(field)
        merged["folder"]             = folder_name
        merged["analysed_at"]        = now
        merged["analysis_timestamp"] = now

        # Save local sidecar
        sidecar_path = os.path.join(folder_path, "analysis.json")
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        # Save to Supabase if configured
        _save_to_db(folder_name, merged, db_meta)

        done_count += 1
        folder_elapsed = int(time.time() - folder_start)
        print(f"  -> SAVED ({folder_elapsed}s for {num_chunks} chunk(s))")

    total_elapsed = int(time.time() - run_start)
    print(f"\n{'='*50}")
    print(f"Analysis complete — {total_elapsed//60}m {total_elapsed%60}s total")
    print(f"  Done  : {done_count}")
    print(f"  Failed: {failed_count}")


# Start
run_analysis()
