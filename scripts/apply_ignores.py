#!/usr/bin/env python3
"""Insert #[ignore = "..."] before listed test fns in a staged tree.

Usage: apply_ignores.py <stage-root> <ignore-list> <label>
  stage-root: directory containing the staged sources
  ignore-list: file with lines path::fn_name (paths relative to stage-root
               or ending with the relative path)
  label: reason string for the ignore attribute
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print(
            f"usage: {sys.argv[0]} <stage-root> <ignore-list> <reason>",
            file=sys.stderr,
        )
        return 2

    stage = Path(sys.argv[1])
    ignore_list = Path(sys.argv[2])
    reason = sys.argv[3]

    if not ignore_list.is_file():
        return 0

    applied = 0
    for raw in ignore_list.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "::" not in line:
            continue
        rel, fn = line.rsplit("::", 1)
        rel, fn = rel.strip(), fn.strip()

        # Find the file under stage (allow prefix like alloctests/...)
        candidates = list(stage.rglob(Path(rel).name))
        target = None
        for c in candidates:
            try:
                c.relative_to(stage)
            except ValueError:
                continue
            # Prefer path ending with rel
            if str(c).endswith(rel) or c.name == Path(rel).name:
                # tighter match
                if rel in str(c) or str(c).endswith(Path(rel).as_posix()):
                    target = c
                    if str(c).endswith(Path(rel).as_posix()):
                        break
        if target is None:
            # try direct join
            direct = stage / rel
            if direct.is_file():
                target = direct
        if target is None:
            # Silently skip — ignore list is global; not every stage has every file.
            continue

        text = target.read_text(encoding="utf-8")
        # Already ignored?
        pattern = re.compile(
            rf"(?P<attrs>(?:^\s*#\[[^\]]+\]\s*\n)*)"
            rf"(?P<fn>^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+{re.escape(fn)}\s*\()",
            re.MULTILINE,
        )
        m = pattern.search(text)
        if not m:
            print(f"warning: fn not found: {rel}::{fn}", file=sys.stderr)
            continue
        attrs = m.group("attrs")
        if "#[ignore" in attrs:
            continue
        insert = f'#[ignore = "{reason}"]\n'
        # Place ignore just before the fn (after any existing attrs)
        start = m.start("fn")
        text = text[:start] + insert + text[start:]
        target.write_text(text, encoding="utf-8")
        applied += 1
        print(f"ignored {rel}::{fn}")

    return 0 if applied >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
