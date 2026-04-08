# ---
# Google Colab Docling Extraction Script
# ---
# Paste this entire file into a Google Colab cell.
# 
# Prerequisites: 
# 1. Open Google Colab and set Runtime -> Change runtime type -> T4 GPU.
# 2. Grant permission to mount Google Drive.
# 3. Ensure your documents are in a folder on Google Drive.

import os
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

# 1. INSTALLATION
print("Installing Docling and dependencies...")
# Use --quiet to keep the output clean
!pip install --quiet docling pypdf

import torch
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions, AcceleratorDevice

# 2. CONFIGURATION
DRIVE_MOUNT_PATH = "/content/drive"
# Change this path to point to your documents folder on Google Drive
# Based on your Rclone setup: gdrive:gojep_data
INPUT_DRIVE_DIR = "My Drive/gojep_data/tenders/documents" 
OUTPUT_SUBDIR = "extracted_docs"

# --- REPROCESSING SETTINGS ---
FORCE_REPROCESS = False  # Set to True to re-extract EVERYTHING
RETRY_FAILED = True      # Set to True to retry files that have a .failed marker
# -----------------------------

# 3. GOOGLE DRIVE (mounted by run_in_colab.py before this script runs)
FULL_INPUT_PATH = os.path.join(DRIVE_MOUNT_PATH, INPUT_DRIVE_DIR)

# 4. INITIALIZE DOCLING
print(f"CUDA Available: {torch.cuda.is_available()}")

# Optimized for GPU
accelerator_options = AcceleratorOptions(
    device=AcceleratorDevice.AUTO # Will pick CUDA if available
)

pdf_opts = PdfPipelineOptions()
pdf_opts.accelerator_options = accelerator_options
pdf_opts.do_ocr = False # Set to True if documents are scanned images
pdf_opts.do_table_structure = True
pdf_opts.do_picture_classification = False
pdf_opts.do_picture_description = False

converter = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)}
)

# 5. EXTRACTION LOGIC
def process_file(file_path: str) -> bool:
    file_name = os.path.basename(file_path)
    parent_dir = os.path.dirname(file_path)
    output_dir = os.path.join(parent_dir, OUTPUT_SUBDIR)
    os.makedirs(output_dir, exist_ok=True)
    
    output_file = os.path.join(output_dir, f"{file_name}.json")
    failed_marker = os.path.join(output_dir, f"{file_name}.failed")

    if os.path.exists(output_file) and not FORCE_REPROCESS:
        print(f"  [skip] {file_name} (already extracted)")
        return False

    if os.path.exists(failed_marker) and not FORCE_REPROCESS and not RETRY_FAILED:
        print(f"  [skip] {file_name} (previously failed)")
        return False
    
    # Clean up markers if we are reprocessing
    if os.path.exists(failed_marker):
        os.remove(failed_marker)

    print(f"  [processing] {file_name}...", end="", flush=True)
    try:
        result = converter.convert(file_path)
        doc = result.document
        markdown = doc.export_to_markdown()

        # Split into chunks (mimicking the gojep implementation)
        sections = re.split(r'(?=^## )', markdown, flags=re.MULTILINE)
        chunks = []
        current_headings = []
        for section in sections:
            if not section.strip(): continue
            lines = section.strip().splitlines()
            heading = lines[0].lstrip("#").strip() if lines[0].startswith("#") else None
            if heading:
                current_headings = [heading]
            chunks.append({
                "text": section.strip(),
                "headings": list(current_headings),
                "page": None,
            })

        output_data = {
            "source_file": file_name,
            "extension": os.path.splitext(file_name)[1].lstrip("."),
            "extraction_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "content": {
                "markdown": markdown,
                "chunks": chunks
            },
        }

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
            
        print(" done.")
        return True
    except Exception as e:
        print(f" ERROR: {str(e)}")
        # Save a marker for failure
        with open(failed_marker, "w", encoding="utf-8") as f:
            json.dump({"error": str(e), "attempts": 1}, f)
        return False

# 6. RUNNER
def run_extraction():
    if not os.path.exists(FULL_INPUT_PATH):
        print(f"ERROR: Input path not found: {FULL_INPUT_PATH}")
        return

    print(f"Scanning {FULL_INPUT_PATH}...")
    
    all_files = []
    for root, dirs, files in os.walk(FULL_INPUT_PATH):
        # Skip existing extracted folders
        if OUTPUT_SUBDIR in dirs:
            dirs.remove(OUTPUT_SUBDIR)
        
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in [".pdf", ".docx"]:
                all_files.append(os.path.join(root, f))

    total = len(all_files)
    print(f"Found {total} files (.pdf/.docx).")
    
    processed = 0
    for i, file_path in enumerate(all_files, start=1):
        print(f"[{i}/{total}]", end="")
        if process_file(file_path):
            processed += 1
            
    print(f"\nCompleted. Processed {processed} new documents.")

# Start
run_extraction()
