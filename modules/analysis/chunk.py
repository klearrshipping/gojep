"""
Document loading, chunking, and LLM context building.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from modules.analysis.prompt import CHUNK_TOKEN_LIMIT, LOCAL_LLM_TOKEN_LIMIT

logger = logging.getLogger(__name__)

# ── Folder structure constants ─────────────────────────────────────────────────

TENDER_DATA        = "tender_data"
EMAIL_UPDATES      = "email_updates"
CLARIFICATIONS     = "clarifications"
NEW_DOCUMENTS      = "new_documents"
DOCUMENT_DOWNLOADS = "document_downloads"
EXTRACTED_DOCUMENTS = "extracted_documents"
JSON_DOCUMENTS     = "json_documents"

# ── Noise filter ───────────────────────────────────────────────────────────────

# Safety-net noise filter for the analysis pipeline.
# Primary filtering is handled upstream by pre_process_workflow/classify_documents.py
# before Docling extraction. This catches anything that slips through.
NOISE_FILE_PATTERNS = [
    "c4t_",       # XML portal exports — always machine-readable noise
    "sheet ",     # drawing sheet filenames: SHEET 1.pdf.json etc.
]


# ── Text extraction ────────────────────────────────────────────────────────────

def _extract_text_from_json(data: Dict[str, Any]) -> str:
    """Pull plain text from a json_documents JSON file regardless of format."""
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
    Prefers splitting at paragraph breaks (\\n\\n), falls back to line breaks,
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


# ── File loading ───────────────────────────────────────────────────────────────

def _extract_file_texts(tender_folder: str) -> List[Tuple[str, str, int]]:
    """
    Load and extract text from all non-noise JSON files in:
      - tender_data/json_documents/
      - email_updates/clarifications/json_documents/
      - email_updates/new_documents/json_documents/

    Files larger than LOCAL_LLM_TOKEN_LIMIT are split into labelled sub-parts
    at paragraph boundaries before entering the chunking pipeline.
    Returns list of (source_filename, text, token_count) ordered by priority.
    """
    import json

    json_dirs = [
        os.path.join(tender_folder, TENDER_DATA, JSON_DOCUMENTS),
        os.path.join(tender_folder, EMAIL_UPDATES, CLARIFICATIONS, JSON_DOCUMENTS),
        os.path.join(tender_folder, EMAIL_UPDATES, NEW_DOCUMENTS, JSON_DOCUMENTS),
    ]

    json_files = []
    for json_dir in json_dirs:
        if os.path.exists(json_dir):
            json_files.extend(
                os.path.join(json_dir, f)
                for f in os.listdir(json_dir)
                if f.endswith(".json") and not _is_noise_file(f) and f != ".manifest.json"
            )

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


# ── Chunking ───────────────────────────────────────────────────────────────────

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


# ── Context builder ────────────────────────────────────────────────────────────

def build_chunk_contexts(
    tender_folder: str,
    db_meta: Optional[Dict[str, Any]],
) -> List[Tuple[str, List[str]]]:
    """
    Build one context string per chunk based on token count.
    Always routes to OpenRouter — no local LLM.

    Returns [(context_str, source_files), ...].
    """
    from modules.analysis.fetch import _format_metadata_header

    meta_header = ""
    if db_meta:
        meta_header = _format_metadata_header(db_meta) + "\n\n=== TENDER DOCUMENTS ===\n"

    file_texts = _extract_file_texts(tender_folder)
    if not file_texts:
        return []

    chunks = _split_into_chunks(file_texts, CHUNK_TOKEN_LIMIT)
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

    return chunk_contexts
