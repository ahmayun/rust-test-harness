#!/usr/bin/env python3
"""Rewrite a staged test crate to use a hand-written main instead of libtest.

Finds #[test] functions, makes them pub(crate), strips #[test], and appends a
main() that calls each test (with catch_unwind). #[should_panic] expects a
panic; #[ignore] is skipped.

Usage: own_main.py <crate-root.rs>
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


ATTR_RE = re.compile(r"^\s*#\[(?P<body>.*)\]\s*(?://.*)?$")
FN_RE = re.compile(
    r"^(?P<indent>\s*)(?P<vis>pub(?:\([^)]*\))?\s+)?(?P<async>async\s+)?fn\s+(?P<name>\w+)\s*[<(]"
)
MOD_INLINE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*\{")
MOD_SEMI_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*;")
PATH_ATTR_RE = re.compile(r'#\[path\s*=\s*"(?P<path>[^"]+)"\]')


@dataclass
class TestFn:
    file: Path
    module_path: list[str]  # relative to crate root
    name: str
    attrs: list[str]  # raw attribute bodies inside #[...]
    line_fn: int  # 0-based line index of fn
    async_: bool = False


@dataclass
class FileMods:
    """mod name -> resolved file path for `mod x;` in this file."""

    children: dict[str, Path] = field(default_factory=dict)


def strip_attr_body(line: str) -> str | None:
    m = ATTR_RE.match(line)
    return m.group("body").strip() if m else None


def is_test_attr(body: str) -> bool:
    return body == "test" or body.startswith("test(") or body.startswith("test ")


def is_ignore_attr(body: str) -> bool:
    if body == "ignore" or body.startswith("ignore(") or body.startswith("ignore ="):
        return True
    # #[cfg_attr(..., ignore)] / #[cfg_attr(..., ignore = "...")]
    if body.startswith("cfg_attr(") and "ignore" in body:
        return True
    return False


def is_should_panic_attr(body: str) -> bool:
    return body == "should_panic" or body.startswith("should_panic")


def is_cfg_attr(body: str) -> bool:
    return body.startswith("cfg(") or body.startswith("cfg_attr(")


def resolve_mod_file(parent_file: Path, name: str, path_attr: str | None) -> Path | None:
    parent_dir = parent_file.parent
    if path_attr:
        cand = (parent_dir / path_attr).resolve()
        return cand if cand.is_file() else None
    for cand in (parent_dir / f"{name}.rs", parent_dir / name / "mod.rs"):
        if cand.is_file():
            return cand.resolve()
    return None


def collect_mod_map(root: Path) -> dict[Path, list[str]]:
    """Map absolute file path -> module path from crate root."""
    root = root.resolve()
    file_to_mod: dict[Path, list[str]] = {root: []}
    queue: list[tuple[Path, list[str]]] = [(root, [])]
    visited = {root}

    while queue:
        file, mod_path = queue.pop(0)
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        pending_path_attr: str | None = None
        for line in lines:
            body = strip_attr_body(line)
            if body is not None:
                m = PATH_ATTR_RE.match(f"#[{body}]")
                if m:
                    pending_path_attr = m.group("path")
                continue
            m = MOD_SEMI_RE.match(line)
            if m:
                name = m.group("name")
                child = resolve_mod_file(file, name, pending_path_attr)
                pending_path_attr = None
                if child and child not in visited:
                    visited.add(child)
                    child_path = mod_path + [name]
                    file_to_mod[child] = child_path
                    queue.append((child, child_path))
                continue
            pending_path_attr = None
            # Inline mods: we still scan the same file with an updated path
            # via brace tracking in discover_tests_in_file
    return file_to_mod


def discover_tests_in_file(file: Path, base_mod: list[str]) -> list[TestFn]:
    text = file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tests: list[TestFn] = []
    pending_attrs: list[str] = []
    # stack of (brace_depth_at_mod_entry, mod_name)
    mod_stack: list[tuple[int, str]] = []
    depth = 0

    def current_mod() -> list[str]:
        return base_mod + [m for _, m in mod_stack]

    i = 0
    while i < len(lines):
        line = lines[i]
        body = strip_attr_body(line)
        if body is not None:
            pending_attrs.append(body)
            i += 1
            continue

        m_inline = MOD_INLINE_RE.match(line)
        if m_inline and "{" in line:
            mod_stack.append((depth, m_inline.group("name")))
            pending_attrs.clear()
            depth += line.count("{") - line.count("}")
            while mod_stack and depth <= mod_stack[-1][0]:
                mod_stack.pop()
            i += 1
            continue

        m_fn = FN_RE.match(line)
        if m_fn and any(is_test_attr(a) for a in pending_attrs):
            if m_fn.group("async"):
                # Skip async tests — own-main harness is sync-only.
                pending_attrs.clear()
                i += 1
                continue
            tests.append(
                TestFn(
                    file=file,
                    module_path=current_mod(),
                    name=m_fn.group("name"),
                    attrs=list(pending_attrs),
                    line_fn=i,
                    async_=False,
                )
            )
            pending_attrs.clear()
            depth += line.count("{") - line.count("}")
            while mod_stack and depth <= mod_stack[-1][0]:
                mod_stack.pop()
            i += 1
            continue

        if line.strip() and not line.strip().startswith("//"):
            # Non-attribute, non-test line clears pending attrs (e.g. blank kept)
            if pending_attrs and not line.strip().startswith("#["):
                pending_attrs.clear()

        depth += line.count("{") - line.count("}")
        while mod_stack and depth <= mod_stack[-1][0]:
            mod_stack.pop()
        i += 1

    return tests


def transform_file(file: Path, tests_in_file: list[TestFn]) -> None:
    if not tests_in_file:
        return
    by_line = {t.line_fn: t for t in tests_in_file}
    lines = file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        if strip_attr_body(lines[i]) is not None:
            j = i
            attrs_lines: list[str] = []
            while j < len(lines) and strip_attr_body(lines[j]) is not None:
                attrs_lines.append(lines[j])
                j += 1
            if j < len(lines) and j in by_line:
                for al in attrs_lines:
                    body = strip_attr_body(al)
                    if body is not None and is_test_attr(body):
                        continue
                    out.append(al)
                fn_line = lines[j]
                m = FN_RE.match(fn_line.rstrip("\n"))
                if m and not m.group("vis"):
                    indent = m.group("indent") or ""
                    rest = fn_line[len(indent) :]
                    out.append(f"{indent}pub(crate) {rest}")
                else:
                    out.append(fn_line)
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    file.write_text("".join(out), encoding="utf-8")


def rust_path(mod_path: list[str], name: str) -> str:
    if not mod_path:
        return name
    return "::".join(mod_path + [name])


def generate_main(tests: list[TestFn], *, indent: str = "") -> str:
    lines: list[str] = [
        "",
        "// ----- generated by external-tester --own-main -----",
        "#[allow(unused_mut, unused_variables)]",
        "fn main() {",
        "    let mut passed: u32 = 0;",
        "    let mut failed: u32 = 0;",
        "    let mut ignored: u32 = 0;",
        "",
    ]

    for t in tests:
        full = rust_path(t.module_path, t.name)
        ignore = any(is_ignore_attr(a) for a in t.attrs)
        should_panic = any(is_should_panic_attr(a) for a in t.attrs)
        cfgs = [a for a in t.attrs if is_cfg_attr(a) and not is_ignore_attr(a)]

        for c in cfgs:
            lines.append(f"    #[{c}]")
        lines.append("    {")
        lines.append(f'        eprint!("test {full} ... ");')
        if ignore:
            lines.append('        eprintln!("ignored");')
            lines.append("        ignored += 1;")
        elif should_panic:
            lines.append("        let r = ::std::panic::catch_unwind(|| {")
            lines.append(f"            {full}();")
            lines.append("        });")
            lines.append("        if r.is_err() {")
            lines.append('            eprintln!("ok");')
            lines.append("            passed += 1;")
            lines.append("        } else {")
            lines.append('            eprintln!("FAILED (expected panic)");')
            lines.append("            failed += 1;")
            lines.append("        }")
        else:
            lines.append("        let r = ::std::panic::catch_unwind(|| {")
            lines.append(f"            {full}();")
            lines.append("        });")
            lines.append("        if r.is_ok() {")
            lines.append('            eprintln!("ok");')
            lines.append("            passed += 1;")
            lines.append("        } else {")
            lines.append('            eprintln!("FAILED");')
            lines.append("            failed += 1;")
            lines.append("        }")
        lines.append("    }")
        lines.append("")

    lines += [
        '    eprintln!("own-main result: {passed} passed; {failed} failed; {ignored} ignored");',
        "    if failed > 0 {",
        "        ::std::process::exit(101);",
        "    }",
        "}",
        "",
    ]
    if indent:
        return "\n".join(indent + ln if ln else ln for ln in lines)
    return "\n".join(lines)


def crate_has_main(root: Path) -> bool:
    text = root.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"(?m)^\s*fn\s+main\s*\(", text))


CRATE_CFG_RE = re.compile(r"^#!\[cfg\((?P<pred>[^\]]*)\)\]\s*$")


def extract_crate_cfg(lines: list[str]) -> tuple[str | None, list[str]]:
    """Return (cfg_predicate, lines_without_crate_cfg)."""
    pred = None
    out: list[str] = []
    for line in lines:
        m = CRATE_CFG_RE.match(line.rstrip("\n"))
        if m and pred is None:
            pred = m.group("pred").strip()
            continue
        out.append(line)
    return pred, out


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <crate-root.rs>", file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.is_file():
        print(f"error: not a file: {root}", file=sys.stderr)
        return 1

    if crate_has_main(root):
        print(f"own-main: skip {root.name} (already has main)")
        return 0

    raw_lines = root.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    crate_cfg, body_lines = extract_crate_cfg(raw_lines)

    # If the crate is gated (e.g. #![cfg(windows)]), wrap body in a module so a
    # host build still gets an empty main() instead of a missing-main error.
    wrap_mod = "__et_tests"
    if crate_cfg is not None:
        # Rewrite root without #![cfg]; body goes inside #[cfg] mod later.
        root.write_text("".join(body_lines), encoding="utf-8")

    file_to_mod = collect_mod_map(root)
    all_tests: list[TestFn] = []
    for file, mod_path in file_to_mod.items():
        all_tests.extend(discover_tests_in_file(file, mod_path))

    if not all_tests:
        # Still emit an empty main so the bin links (e.g. fully cfg'd-out crates).
        if crate_cfg is not None:
            stub = (
                f"// ----- generated by external-tester --own-main -----\n"
                f"#[cfg(not({crate_cfg}))]\n"
                f"fn main() {{}}\n"
                f"#[cfg({crate_cfg})]\n"
                f"fn main() {{}}\n"
            )
            # Restore gated body empty + stub — original body was written ungated;
            # re-wrap for correctness on the target OS.
            gated_body = "".join(body_lines)
            root.write_text(
                f"#[cfg({crate_cfg})]\nmod {wrap_mod} {{\n{gated_body}\n}}\n{stub}",
                encoding="utf-8",
            )
            print(f"own-main: no tests after cfg; stub main for {root.name}")
            return 0
        print(f"own-main: no #[test] functions found in {root}", file=sys.stderr)
        return 1

    by_file: dict[Path, list[TestFn]] = {}
    for t in all_tests:
        by_file.setdefault(t.file, []).append(t)
    for file, ts in by_file.items():
        transform_file(file, ts)

    if crate_cfg is not None:
        # Prefix module paths with wrap_mod; wrap root body in cfg mod; dual main.
        for t in all_tests:
            t.module_path = [wrap_mod] + t.module_path
        # Re-read transformed root (only root file content for wrap — child files stay)
        transformed = root.read_text(encoding="utf-8", errors="replace")
        main_inner = generate_main(all_tests)
        # Calls use __et_tests::... so main stays outside the mod.
        root.write_text(
            f"#[cfg({crate_cfg})]\n"
            f"mod {wrap_mod} {{\n"
            f"{transformed}"
            f"\n}}\n"
            f"#[cfg(not({crate_cfg}))]\n"
            f"fn main() {{}}\n"
            f"#[cfg({crate_cfg})]\n"
            f"{main_inner.lstrip()}",
            encoding="utf-8",
        )
    else:
        main_src = generate_main(all_tests)
        with root.open("a", encoding="utf-8") as f:
            f.write(main_src)

    print(f"own-main: {len(all_tests)} tests wired in {root.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
