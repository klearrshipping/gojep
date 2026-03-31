"""
Extract data from GOJEP award notice PDFs into JSON.

Reads each PDF for:
- AcroForm fields (fillable PDFs)
- Text per page (layout text; no OCR)
- Document metadata
- Checkbox state via PDF content-stream analysis (ePPS radio buttons)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader
from pypdf.generic import IndirectObject

from config import settings as config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ePPS checkbox / radio-button detection
# ---------------------------------------------------------------------------
# ePPS renders checkboxes as inline SVG-like vector paths in the content stream
# (no AcroForm widgets).  A *selected* radio button contains a filled inner
# circle (radius ≈ 5.8 pts) in addition to the outer ring; an *unselected* one
# only has the outer ring.
#
# The sentinel path command that appears exclusively inside selected buttons:
_SELECTED_INNER_CIRCLE = "5.79999995 4 m"

# Y-coordinate tolerance (pts) for grouping buttons on the same visual row.
_ROW_Y_TOLERANCE = 5.0

# Known ePPS procurement-type options in display order (left → right).
_PROCUREMENT_TYPE_OPTIONS = ["Works", "Goods", "Services"]

# Known ePPS procurement-method options in display order (top → bottom).
_PROCUREMENT_METHOD_OPTIONS = [
    "Open - ICB",
    "Open - NCB",
    "Restricted Bidding (RB)",
    "Single Source (SS)",
    "Emergency Procedure",
]


def _extract_checkbox_selections(
    reader: PdfReader,
    page_index: int = 0,
) -> Dict[str, Any]:
    """
    Parse the ePPS content stream on *page_index* and return the selected values
    for procurement_type and procurement_method.

    Strategy
    --------
    1. Walk the page content stream, maintaining a cumulative Y-offset stack
       that mirrors the PDF graphics-state (q/Q) nesting.
    2. Each ``%SVG setup … %SVG end`` block corresponds to one checkbox.
       A *selected* button contains the inner-circle fill path
       ``5.79999995 4 m`` (radius ≈ 5.8 pts); unselected buttons do not.
    3. The accumulated Y at the moment of each ``%SVG setup`` gives the
       checkbox's page-space row.  Buttons whose Y values cluster within
       ``_ROW_Y_TOLERANCE`` pts belong to the same horizontal row.
    4. Row with the smallest Y (topmost on the inverted PDF canvas) →
       procurement-type choices (Works / Goods / Services).
       Remaining rows (one per method) → procurement-method choices.

    Returns a dict with keys ``procurement_type`` and/or
    ``procurement_method_selected`` when unambiguously detected.
    """
    result: Dict[str, Any] = {}
    try:
        page = reader.pages[page_index]
        contents = page.get("/Contents")
        if contents is None:
            return result
        if isinstance(contents, IndirectObject):
            contents = contents.get_object()

        # Assemble raw bytes from a single stream object or an array of streams.
        if hasattr(contents, "get_data"):
            raw = contents.get_data().decode("latin-1", errors="replace")
        elif isinstance(contents, list):
            parts = []
            for item in contents:
                obj = item.get_object() if isinstance(item, IndirectObject) else item
                if hasattr(obj, "get_data"):
                    parts.append(obj.get_data().decode("latin-1", errors="replace"))
            raw = "\n".join(parts)
        else:
            return result

        # ------------------------------------------------------------------
        # Tokenise: record positions of q, Q, cm, %SVG setup, and SVG blocks
        # ------------------------------------------------------------------
        events: List[Tuple] = []

        for m in re.finditer(r"1 0 0 1 [\d.]+ ([\d.]+) cm", raw):
            events.append((m.start(), "cm", float(m.group(1))))

        for m in re.finditer(r"\bq\b", raw):
            events.append((m.start(), "q"))

        for m in re.finditer(r"\bQ\b", raw):
            events.append((m.start(), "Q"))

        for m in re.finditer(r"%SVG setup", raw):
            events.append((m.start(), "svg_setup"))

        for m in re.finditer(r"%SVG start(.*?)%SVG end", raw, re.DOTALL):
            events.append((m.start(), "svg_block", m.group(1)))

        events.sort(key=lambda e: e[0])

        # ------------------------------------------------------------------
        # Walk events → collect (y, is_selected) per checkbox
        # ------------------------------------------------------------------
        y_stack: List[float] = [0.0]
        checkboxes: List[Tuple[float, bool]] = []  # (abs_y, selected)

        for ev in events:
            kind = ev[1]
            if kind == "q":
                y_stack.append(y_stack[-1])
            elif kind == "Q":
                if len(y_stack) > 1:
                    y_stack.pop()
            elif kind == "cm":
                y_stack[-1] += ev[2]
            elif kind == "svg_setup":
                # Record the page-space Y at the moment this checkbox starts.
                checkboxes.append((y_stack[-1], False))  # placeholder
            elif kind == "svg_block":
                # Fill in the selected flag for the most recent svg_setup entry.
                if checkboxes:
                    y_val, _ = checkboxes[-1]
                    is_sel = _SELECTED_INNER_CIRCLE in ev[2]
                    checkboxes[-1] = (y_val, is_sel)

        if not checkboxes:
            return result

        # ------------------------------------------------------------------
        # Group checkboxes into rows by Y proximity
        # ------------------------------------------------------------------
        checkboxes.sort(key=lambda c: c[0])
        rows: List[List[Tuple[float, bool]]] = []
        for cb in checkboxes:
            placed = False
            for row in rows:
                if abs(cb[0] - row[0][0]) <= _ROW_Y_TOLERANCE:
                    row.append(cb)
                    placed = True
                    break
            if not placed:
                rows.append([cb])

        # Sort rows top-to-bottom (ascending Y in the flipped PDF canvas).
        rows.sort(key=lambda r: r[0][0])

        # ------------------------------------------------------------------
        # Map rows to field values
        # ------------------------------------------------------------------
        if rows:
            # First row = procurement-type (Works / Goods / Services)
            type_row = rows[0]
            for idx, (_, is_sel) in enumerate(type_row):
                if is_sel and idx < len(_PROCUREMENT_TYPE_OPTIONS):
                    result["procurement_type"] = _PROCUREMENT_TYPE_OPTIONS[idx]
                    break

        if len(rows) > 1:
            # Remaining rows = procurement methods, one per row
            method_rows = rows[1:]
            for idx, row in enumerate(method_rows):
                if any(is_sel for _, is_sel in row):
                    if idx < len(_PROCUREMENT_METHOD_OPTIONS):
                        result["procurement_method_selected"] = _PROCUREMENT_METHOD_OPTIONS[idx]
                    break

    except Exception as exc:  # noqa: BLE001
        logger.debug("checkbox detection failed: %s", exc)

    return result


def parse_contract_award_notice_text(full_text: str) -> Dict[str, Any]:
    """
    Turn GOJEP "Contract Award Notice" PDF text into flat JSON-friendly fields.
    ePPS PDFs are usually not AcroForms; the data lives in layout text (see full_text).
    """
    if not full_text or not full_text.strip():
        return {}

    t = full_text.replace("\r\n", "\n")

    def grab(pat: str, flags: int = re.DOTALL) -> Optional[str]:
        m = re.search(pat, t, flags)
        if not m:
            return None
        s = m.group(1).strip()
        return s if s else None

    out: Dict[str, Any] = {}

    out["official_name"] = grab(r"Official name:\s*(.+?)(?=\nPostal address:|\nSECTION\s|\Z)")
    out["postal_address"] = grab(r"Postal address:\s*(.+?)(?=\nSECTION\s|\Z)")
    out["title"] = grab(r"TITLE:\s*(.+?)(?=\nTender Reference Number:|\Z)")
    out["tender_reference_number"] = grab(r"Tender Reference Number:\s*\n?\s*(\S+)")

    # procurement_type and procurement_method_selected are injected by
    # _extract_checkbox_selections() at the extract_award_pdf() level;
    # we do NOT attempt to guess them from text here.

    out["name_of_contractor"] = grab(
        r"Name of contractor \(1\)\s*\n?\s*(.+?)(?=\nPPC Category Code and Titles|\Z)"
    )
    ppc_block = grab(
        r"PPC Category Code and Titles \(1\)\s*\n?\s*(.+?)(?=\nCPV codes \(1\)|\Z)"
    )
    if ppc_block:
        out["ppc_category_code_and_title"] = [ln.strip() for ln in ppc_block.split("\n") if ln.strip()]

    cpv_block = grab(r"CPV codes \(1\)\s*\n(.+?)(?=\nContract price \(1\)|\Z)", re.DOTALL)
    if cpv_block:
        out["cpv_codes"] = [ln.strip() for ln in cpv_block.split("\n") if ln.strip()]

    price_line = grab(r"Contract price \(1\)\s*\n?\s*(.+?)(?=\nLevel of Competition|\Z)")
    if price_line:
        out["contract_price_raw"] = price_line
        pm = re.search(
            r"([\d,]+\.?\d*)\s*Currency:\s*(\w+)",
            price_line,
        )
        if pm:
            out["contract_price_amount"] = pm.group(1).replace(",", "")
            out["contract_price_currency"] = pm.group(2)

    loc_line = grab(r"Level of Competition\s*\n\s*([^\n]*)")
    if loc_line and loc_line.strip():
        val = loc_line.strip()
        # Avoid merged page markers like "2/ 2" when the field is blank.
        if not re.match(r"^\d+\s*/\s*\d+\s*$", val):
            out["level_of_competition"] = val

    # Contract Award Criteria suffix (e.g. "LCS", "QCBS") appears on the page-2
    # header line: "Contract Award Criteria <CODE>".
    cac = grab(r"Contract Award Criteria\s+([A-Z][A-Z0-9/\-]+)")
    if cac:
        out["contract_award_criteria"] = cac

    out["funding_source"] = grab(r"Funding Source:\s*([^\n]+)")
    m_fp = re.search(r"Funding Providers:\s*\n([^\n]*)", t)
    if m_fp:
        fp_line = m_fp.group(1).strip()
        if fp_line and not re.match(r"^Contract award date\b", fp_line):
            out["funding_providers"] = fp_line

    out["contract_award_date"] = grab(r"Contract award date\s*\nDate:\s*([^\n(]+)")

    # Principal site of performance: stop at any of several possible next labels,
    # or at a SECTION header — whichever comes first.  The \Z fallback is
    # intentionally excluded here so that a blank field returns None rather than
    # consuming the rest of the document.
    out["principal_site_of_performance"] = grab(
        r"Principal site of performance\s*\n?\s*(.+?)"
        r"(?=\nCommencement date|\nContract award date|\nSECTION\s|\nDATE OF DISPATCH|\Z)",
        re.DOTALL,
    )
    # Reject the value if it spans multiple labelled sections (indicates blank field).
    psop = out.get("principal_site_of_performance", "")
    if psop and re.search(
        r"\n(Commencement date|Contract award date|SECTION\s|DATE OF DISPATCH|Duration\b)",
        psop,
    ):
        out["principal_site_of_performance"] = None

    out["commencement_date"] = grab(r"Commencement date\s*\nDate:\s*([^\n(]+)")
    m_dur = re.search(r"\bDuration\b\s*\n", t)
    if m_dur:
        rest = t[m_dur.end() :]
        first_line = rest.split("\n", 1)[0].strip()
        if first_line and not re.match(r"^SECTION\b", first_line):
            out["duration"] = first_line

    # Justification: the section is only populated for emergency/urgency awards.
    # Guard against capturing content from later sections when the field is blank.
    raw_just = grab(
        r"Specify the Emergency or Extreme urgency of the award\s*\n(.+?)"
        r"(?=\nDATE OF DISPATCH OF THIS NOTICE:|\Z)",
        re.DOTALL,
    )
    if raw_just:
        # Reject if the captured text IS the dispatch-notice line or starts with
        # "DATE OF DISPATCH" (happens when the field is blank and \Z matched).
        if not re.match(r"DATE OF DISPATCH", raw_just.strip()):
            out["justification"] = raw_just

    out["date_of_dispatch_of_notice"] = grab(
        r"DATE OF DISPATCH OF THIS NOTICE:\s*\n?\s*([^\n(]+)"
    )

    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _decode_pdf_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, IndirectObject):
        try:
            value = value.get_object()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, (list, tuple)):
        return [_decode_pdf_value(v) for v in value]
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return value.hex()
    if isinstance(value, dict):
        return {str(k): _decode_pdf_value(v) for k, v in value.items()}
    return str(value)


def _flatten_form_fields(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not fields:
        return {}
    out: Dict[str, Any] = {}
    for name, field in fields.items():
        if not isinstance(field, dict):
            continue
        v = field.get("/V")
        if v is None:
            continue
        key = str(name)
        out[key] = _decode_pdf_value(v)
    return out


def extract_award_pdf(pdf_path: str) -> Dict[str, Any]:
    """
    Parse one notice PDF. Returns a dict safe to JSON-serialize.
    """
    result: Dict[str, Any] = {
        "source_file": os.path.basename(pdf_path),
        "source_path": pdf_path,
        "page_count": 0,
        "metadata": {},
        "form_fields": {},
        "parsed_fields": {},
        "text_by_page": [],
        "full_text": "",
        "error": None,
    }

    try:
        reader = PdfReader(pdf_path, strict=False)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"open_failed: {e}"
        return result

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                result["error"] = "encrypted_pdf_needs_password"
                return result
        except Exception as e:  # noqa: BLE001
            result["error"] = f"decrypt_failed: {e}"
            return result

    result["page_count"] = len(reader.pages)

    meta = reader.metadata
    if meta:
        for k in meta:
            try:
                v = meta[k]
                result["metadata"][str(k)] = _decode_pdf_value(v) if v is not None else None
            except Exception:  # noqa: BLE001
                result["metadata"][str(k)] = None

    try:
        raw_fields = reader.get_fields()
        result["form_fields"] = _flatten_form_fields(raw_fields)
    except Exception as e:  # noqa: BLE001
        logger.debug("get_fields failed for %s: %s", pdf_path, e)

    texts: List[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            t = ""
            result.setdefault("text_extraction_warnings", []).append(f"page_{i + 1}: {e}")
        t = t.strip()
        result["text_by_page"].append({"page": i + 1, "text": t})
        if t:
            texts.append(t)

    result["full_text"] = "\n\n".join(texts)
    result["parsed_fields"] = parse_contract_award_notice_text(result["full_text"])

    # Overlay accurate checkbox selections detected from the content stream.
    # These supersede any text-based guesses (there are none now, but being
    # explicit guards against future regressions).
    checkbox_fields = _extract_checkbox_selections(reader, page_index=0)
    if checkbox_fields:
        result["parsed_fields"].update(checkbox_fields)

    return result


def _sanitize_part(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return re.sub(r"[^\w\-.]+", "_", s)


def expected_pdf_filename(row: Dict[str, Any], index: int) -> str:
    """Same naming rules as get_awards.download_pdfs_for_awards."""
    parts: List[str] = []
    rp = _sanitize_part(row.get("row_number"))
    if rp:
        parts.append(f"row{rp}")
    cp = _sanitize_part(row.get("resource_id"))
    if cp:
        parts.append(f"contract{cp}")
    np = _sanitize_part(row.get("pdf_resource_id"))
    if np:
        parts.append(f"notice{np}")
    if not parts:
        parts.append(f"award_index{index}")
    return "_".join(parts) + ".pdf"


def resolve_pdf_path(row: Dict[str, Any], index: int, awards_root: str) -> Optional[str]:
    rel = row.get("pdf_local_relpath")
    if rel:
        p = os.path.normpath(os.path.join(awards_root, rel))
        if os.path.isfile(p):
            return p
    pdf_subdir = getattr(config, "AWARDS_PDF_SUBDIR", "pdf")
    fallback = os.path.join(awards_root, pdf_subdir, expected_pdf_filename(row, index))
    if os.path.isfile(fallback):
        return fallback
    return None


def _latest_awards_json_path() -> Optional[str]:
    d = config.AWARDS_OUTPUT_DIRECTORY
    if not os.path.isdir(d):
        return None
    candidates = [
        os.path.join(d, name)
        for name in os.listdir(d)
        if name.startswith("awards_") and name.endswith(".json")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def extract_from_awards_json(
    input_json_path: str,
    awards_root: Optional[str] = None,
    limit: Optional[int] = None,
    progress_every: int = 200,
) -> Dict[str, Any]:
    awards_root = awards_root or config.AWARDS_OUTPUT_DIRECTORY
    with open(input_json_path, encoding="utf-8") as f:
        rows: List[Dict[str, Any]] = json.load(f)
    if not isinstance(rows, list):
        raise ValueError("Awards JSON must be a list")

    if limit is not None:
        rows = rows[:limit]

    total = len(rows)
    logger.info("Starting extraction: %s award rows from %s", total, input_json_path)
    t0 = time.perf_counter()
    records: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        pdf_path = resolve_pdf_path(row, i, awards_root)
        base = {
            "row_number": row.get("row_number"),
            "resource_id": row.get("resource_id"),
            "pdf_resource_id": row.get("pdf_resource_id"),
            "pdf_url": row.get("pdf_url"),
            "title": row.get("title"),
        }
        if not pdf_path:
            records.append(
                {
                    **base,
                    "pdf_path": None,
                    "extracted": None,
                    "error": "pdf_file_not_found",
                }
            )
        else:
            extracted = extract_award_pdf(pdf_path)
            err = extracted.pop("error", None)
            records.append({**base, "pdf_path": pdf_path, "extracted": extracted, "error": err})

        current = i + 1
        if progress_every > 0 and current % progress_every == 0:
            elapsed = time.perf_counter() - t0
            rate = current / elapsed if elapsed > 0 else 0.0
            remaining = total - current
            eta_s = remaining / rate if rate > 0 else 0.0
            logger.info(
                "Progress: %s/%s (%.1f%%) ~%.2f rows/s elapsed=%.0fs ETA~%.0fs",
                current,
                total,
                100.0 * current / total if total else 0.0,
                rate,
                elapsed,
                eta_s,
            )

    elapsed = time.perf_counter() - t0
    logger.info(
        "Extraction pass finished: %s rows in %.1fs (avg %.3fs/row)",
        total,
        elapsed,
        elapsed / total if total else 0.0,
    )

    return {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "source_awards_json": os.path.abspath(input_json_path),
        "awards_root": os.path.abspath(awards_root),
        "record_count": len(records),
        "records": records,
    }


def save_details_payload(payload: Dict[str, Any], output_path: Optional[str] = None) -> str:
    out_dir = config.AWARDS_DETAILS_OUTPUT_DIRECTORY
    os.makedirs(out_dir, exist_ok=True)
    if not output_path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(out_dir, f"award_details_{ts}.json")
    nrec = int(payload.get("record_count") or 0)
    if nrec >= 500:
        logger.info("Writing output JSON (%s records) — large files can take a few minutes", nrec)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("Wrote %s records to %s", payload.get("record_count"), output_path)
    return output_path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract JSON from GOJEP award notice PDFs")
    p.add_argument(
        "--input-json",
        type=str,
        default=None,
        help="Awards extract JSON (default: newest awards_*.json under data/awards)",
    )
    p.add_argument(
        "--awards-root",
        type=str,
        default=None,
        help="Folder that contains pdf/ (default: config AWARDS_OUTPUT_DIRECTORY)",
    )
    p.add_argument("--limit", type=int, default=None, help="Process only first N rows (testing)")
    p.add_argument(
        "--progress-every",
        type=int,
        default=200,
        metavar="N",
        help="Log progress every N rows with ETA (0 = disable; default: 200)",
    )
    p.add_argument("--output", type=str, default=None, help="Output JSON path")
    p.add_argument(
        "--single-pdf",
        type=str,
        default=None,
        help="Extract one PDF file only; ignores --input-json",
    )
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.single_pdf:
        if not os.path.isfile(args.single_pdf):
            raise SystemExit(f"File not found: {args.single_pdf}")
        data = extract_award_pdf(os.path.abspath(args.single_pdf))
        out = args.output or os.path.join(
            config.AWARDS_DETAILS_OUTPUT_DIRECTORY,
            f"award_detail_single_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json",
        )
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(out)
        return

    input_path = args.input_json or _latest_awards_json_path()
    if not input_path:
        raise SystemExit("No --input-json and no awards_*.json under data/awards")
    if not os.path.isfile(input_path):
        raise SystemExit(f"Input not found: {input_path}")

    payload = extract_from_awards_json(
        input_path,
        awards_root=args.awards_root,
        limit=args.limit,
        progress_every=max(0, args.progress_every),
    )
    path = save_details_payload(payload, args.output)
    print(path)


if __name__ == "__main__":
    main()
