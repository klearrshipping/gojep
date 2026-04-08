"""
XML extractor — extracts readable text from XML files using ElementTree.
Returns plain text with tag names stripped, preserving content.
"""

import xml.etree.ElementTree as ET


def extract(file_path: str) -> str:
    tree = ET.parse(file_path)
    root = tree.getroot()
    parts = []

    def walk(node, depth=0):
        text = (node.text or "").strip()
        tail = (node.tail or "").strip()
        tag = node.tag.split("}")[-1] if "}" in node.tag else node.tag  # strip namespace
        if text:
            parts.append(f"[{tag}] {text}")
        for child in node:
            walk(child, depth + 1)
        if tail:
            parts.append(tail)

    walk(root)
    return "\n".join(parts)
