"""
XLS extractor — extracts text from old Excel (.xls) files using xlrd.
Converts each sheet into a markdown table.

Requires: pip install xlrd
"""

import xlrd


def extract(file_path: str) -> str:
    wb = xlrd.open_workbook(file_path)
    parts = []

    for sheet in wb.sheets():
        parts.append(f"## Sheet: {sheet.name}\n")
        for row_idx in range(sheet.nrows):
            row = sheet.row(row_idx)
            cells = [str(cell.value).strip() for cell in row]
            parts.append(" | ".join(cells))
        parts.append("")

    return "\n".join(parts)
