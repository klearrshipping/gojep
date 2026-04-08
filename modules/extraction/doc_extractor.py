"""
DOC extractor — extracts text from old Word (.doc) files using docx2txt.
Falls back to antiword (if installed) for files docx2txt can't handle.

Requires: pip install docx2txt
"""

import subprocess
import tempfile
import os


def extract(file_path: str) -> str:
    # Try docx2txt first (works on many .doc files)
    try:
        import docx2txt
        text = docx2txt.process(file_path)
        if text and text.strip():
            return text.strip()
    except Exception:
        pass

    # Fallback: antiword (if installed on the system)
    try:
        result = subprocess.run(
            ["antiword", file_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    raise RuntimeError(f"Could not extract text from {file_path} — install antiword or convert to .docx")
