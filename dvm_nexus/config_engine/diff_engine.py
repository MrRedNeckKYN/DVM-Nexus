"""
DVM Nexus - Visual Diff Engine
Produces unified and structured diffs between configuration files.
"""

import difflib
from typing import Dict, List, Any


def generate_unified_diff(old_text: str, new_text: str, old_label: str = "Current", new_label: str = "Proposed") -> str:
    """Generate a standard unified diff string."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label)
    return "".join(diff)


def generate_structured_diff(old_text: str, new_text: str) -> List[Dict[str, Any]]:
    """
    Generate structured line-by-line diff for rich UI rendering.
    Each item: {"type": "add"|"del"|"equal", "line_num_old": int, "line_num_new": int, "text": str}
    """
    matcher = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines())
    diff_lines: List[Dict[str, Any]] = []

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for idx, line in enumerate(old_lines[i1:i2]):
                diff_lines.append({
                    "type": "equal",
                    "old_num": i1 + idx + 1,
                    "new_num": j1 + idx + 1,
                    "text": line
                })
        elif tag == "delete":
            for idx, line in enumerate(old_lines[i1:i2]):
                diff_lines.append({
                    "type": "del",
                    "old_num": i1 + idx + 1,
                    "new_num": None,
                    "text": line
                })
        elif tag == "insert":
            for idx, line in enumerate(new_lines[j1:j2]):
                diff_lines.append({
                    "type": "add",
                    "old_num": None,
                    "new_num": j1 + idx + 1,
                    "text": line
                })
        elif tag == "replace":
            for idx, line in enumerate(old_lines[i1:i2]):
                diff_lines.append({
                    "type": "del",
                    "old_num": i1 + idx + 1,
                    "new_num": None,
                    "text": line
                })
            for idx, line in enumerate(new_lines[j1:j2]):
                diff_lines.append({
                    "type": "add",
                    "old_num": None,
                    "new_num": j1 + idx + 1,
                    "text": line
                })

    return diff_lines
