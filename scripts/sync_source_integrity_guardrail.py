#!/usr/bin/env python3
"""Insert the source-integrity pointer into literature-heavy SKILL.md files.

The canonical rule stays in skills/shared-references/source-integrity.md.
This script does not copy that rule and does not add skills.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.source_integrity import LITERATURE_SKILLS, SKILL_GUARDRAIL_BLOCK  # noqa: E402

_BLOCK_RE = re.compile(
    r"<!-- source-integrity-guardrail -->\n> \*\*Guardrails:\*\*[^\n]*\n?",
)


def _insert(text: str) -> str:
    if "<!-- source-integrity-guardrail -->" in text:
        return _BLOCK_RE.sub(SKILL_GUARDRAIL_BLOCK, text, count=1)
    frontmatter = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    if not frontmatter:
        raise ValueError("SKILL.md is missing YAML frontmatter")
    return text[: frontmatter.end()] + "\n" + SKILL_GUARDRAIL_BLOCK + "\n" + text[frontmatter.end() :]


def main() -> None:
    for skill in sorted(LITERATURE_SKILLS):
        path = ROOT / "skills" / skill / "SKILL.md"
        if not path.exists():
            print(f"skip missing {skill}")
            continue
        original = path.read_text(encoding="utf-8")
        updated = _insert(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            print(f"updated {skill}")
        else:
            print(f"ok {skill}")


if __name__ == "__main__":
    main()
