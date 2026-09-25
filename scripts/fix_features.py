#!/usr/bin/env python3
"""Adjust #![feature(...)] on a crate root based on a rustc error log.

- Removes unknown features (E0635)
- Adds features mentioned as unstable / suggested by rustc help lines
Exits 0 if the source was changed, 1 if nothing to do.
"""
from __future__ import annotations

import re
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <crate-root.rs> <rustc-log>", file=sys.stderr)
        return 2

    src_path, log_path = sys.argv[1], sys.argv[2]
    log = open(log_path, encoding="utf-8", errors="replace").read()
    src = open(src_path, encoding="utf-8", errors="replace").read()

    unknown = set(re.findall(r"unknown feature `([^`]+)`", log))
    needed: set[str] = set()
    for m in re.finditer(r"unstable (?:library )?feature `([^`]+)`", log):
        needed.add(m.group(1))
    for m in re.finditer(r"help: add `#!\[feature\(([^)]+)\)\]`", log):
        for n in m.group(1).split(","):
            n = n.strip()
            if n:
                needed.add(n)

    existing: set[str] = set()
    for m in re.finditer(r"#!\[feature\(([^)]+)\)\]", src):
        for n in m.group(1).split(","):
            n = n.strip()
            if n:
                existing.add(n)

    to_remove = sorted(unknown & existing)
    to_add = sorted((needed - existing) - unknown)

    if not to_remove and not to_add:
        return 1

    lines = src.splitlines(True)
    new_lines: list[str] = []
    for line in lines:
        m = re.match(r"#!\[feature\(([^)]+)\)\]\s*$", line)
        if m:
            feats = [
                f.strip()
                for f in m.group(1).split(",")
                if f.strip() and f.strip() not in unknown
            ]
            if not feats:
                continue
            if len(feats) == 1:
                new_lines.append(f"#![feature({feats[0]})]\n")
            else:
                new_lines.append(f"#![feature({', '.join(feats)})]\n")
            continue
        new_lines.append(line)

    if to_add:
        insert = "".join(f"#![feature({n})]\n" for n in to_add)
        last_attr = -1
        for i, line in enumerate(new_lines):
            if line.startswith("#!["):
                last_attr = i
            elif last_attr >= 0 and line.strip() and not line.startswith("#!["):
                break
        if last_attr >= 0:
            new_lines.insert(last_attr + 1, insert)
        else:
            new_lines.insert(0, insert)

    open(src_path, "w", encoding="utf-8").write("".join(new_lines))
    bits = []
    if to_remove:
        bits.append("removed " + ", ".join(to_remove))
    if to_add:
        bits.append("added " + ", ".join(to_add))
    print("; ".join(bits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
