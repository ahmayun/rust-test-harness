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


FN_RE = re.compile(
    r"^(?P<indent>\s*)(?P<vis>pub(?:\([^)]*\))?\s+)?(?P<async>async\s+)?"
    r"fn\s+(?P<name>\$\{[^}]+\}|\$?\w+)\s*[<(]"
)
MOD_INLINE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*\{")
MOD_SEMI_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*;")
MOD_DECL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<vis>pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*(?P<tail>[;{].*)$"
)
PATH_ATTR_RE = re.compile(r'^path\s*=\s*"(?P<path>[^"]+)"')
CRATE_CFG_BODY_RE = re.compile(r"^cfg\((?P<pred>.*)\)\s*$")
ATTR_START_RE = re.compile(r"^(\s*)#(!?)\[")


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


@dataclass
class ParsedAttr:
    """One #[...] or #![...] attribute, possibly spanning multiple lines."""

    body: str
    inner: bool  # True for #![...]
    start: int  # inclusive line index
    end: int  # exclusive line index


def parse_attr(lines: list[str], start: int) -> ParsedAttr | None:
    """If lines[start] begins an attribute, return it (supports multi-line)."""
    raw = lines[start]
    # strip keepends for matching
    line = raw.rstrip("\n\r")
    m = ATTR_START_RE.match(line)
    if not m:
        return None
    inner = m.group(2) == "!"
    # Collect until bracket depth returns to 0 (after the opening `[`).
    depth = 0
    started = False
    j = start
    chunks: list[str] = []
    while j < len(lines):
        piece = lines[j].rstrip("\n\r")
        chunks.append(piece)
        for ch in piece:
            if ch == "[":
                depth += 1
                started = True
            elif ch == "]" and started:
                depth -= 1
        j += 1
        if started and depth == 0:
            break
    else:
        return None

    full = "\n".join(chunks)
    # Body = inside the outermost #[ ... ] / #![ ... ]
    lb = full.find("[")
    rb = full.rfind("]")
    if lb < 0 or rb < 0 or rb <= lb:
        return None
    body = full[lb + 1 : rb].strip()
    # Collapse internal newlines/whitespace in body for easier matching.
    body = re.sub(r"\s+", " ", body)
    return ParsedAttr(body=body, inner=inner, start=start, end=j)


def is_test_attr(body: str) -> bool:
    if body == "test" or body.startswith("test(") or body.startswith("test "):
        return True
    # Marker used during macro-expansion pass (see expand_macros_for_discovery).
    if body == "et::own_test" or body.startswith("et::own_test"):
        return True
    return False


def is_unconditional_ignore(body: str) -> bool:
    return body == "ignore" or body.startswith("ignore(") or body.startswith("ignore =")


# #[cfg_attr(PRED, ignore)] / #[cfg_attr(PRED, ignore = "...")]
CFG_ATTR_IGNORE_RE = re.compile(
    r"^cfg_attr\s*\(\s*(?P<pred>.+?)\s*,\s*ignore(?:\s*=\s*\"[^\"]*\")?\s*\)$"
)


def cfg_attr_ignore_pred(body: str) -> str | None:
    m = CFG_ATTR_IGNORE_RE.match(body)
    return m.group("pred").strip() if m else None


def is_should_panic_attr(body: str) -> bool:
    return body == "should_panic" or body.startswith("should_panic")


def is_cfg_attr(body: str) -> bool:
    return body.startswith("cfg(") or body.startswith("cfg_attr(")


def is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("//")


def resolve_mod_file(parent_file: Path, name: str, path_attr: str | None) -> Path | None:
    parent_dir = parent_file.parent
    if path_attr:
        cand = (parent_dir / path_attr).resolve()
        return cand if cand.is_file() else None
    for cand in (parent_dir / f"{name}.rs", parent_dir / name / "mod.rs"):
        if cand.is_file():
            return cand.resolve()
    return None


def mod_path_attr_for(parent_file: Path, name: str) -> str | None:
    """Return a #[path = "..."] for a sibling mod in the same directory as parent_file."""
    parent_dir = parent_file.parent
    for rel in (f"{name}.rs", f"{name}/mod.rs"):
        if (parent_dir / rel).is_file():
            return rel
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
        i = 0
        while i < len(lines):
            attr = parse_attr(lines, i)
            if attr is not None:
                m = PATH_ATTR_RE.match(attr.body)
                if m:
                    pending_path_attr = m.group("path")
                i = attr.end
                continue
            m = MOD_SEMI_RE.match(lines[i])
            if m:
                name = m.group("name")
                child = resolve_mod_file(file, name, pending_path_attr)
                pending_path_attr = None
                if child and child not in visited:
                    visited.add(child)
                    child_path = mod_path + [name]
                    file_to_mod[child] = child_path
                    queue.append((child, child_path))
                i += 1
                continue
            pending_path_attr = None
            i += 1
    return file_to_mod


MACRO_RULES_RE = re.compile(r"^\s*macro_rules!\s*(?:\w+\s*)?\{")


def net_brace_delta(s: str) -> int:
    """Net `{` − `}` ignoring contents of strings, chars, and comments."""
    i = 0
    n = len(s)
    delta = 0
    while i < n:
        c = s[i]
        if c == "/" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "/":
                break  # line comment
            if nxt == "*":
                i += 2
                while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        if c == '"':
            i += 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "'":
            # char lit or lifetime — lifetimes have no braces; char may be '{'
            i += 1
            if i < n and s[i] == "\\":
                i += 2
            elif i < n:
                i += 1
            if i < n and s[i] == "'":
                i += 1
            continue
        if c == "{":
            delta += 1
        elif c == "}":
            delta -= 1
        i += 1
    return delta


def discover_tests_in_file(file: Path, base_mod: list[str]) -> list[TestFn]:
    text = file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tests: list[TestFn] = []
    pending_attrs: list[str] = []
    mod_stack: list[tuple[int, str]] = []
    depth = 0
    # When not None, we're inside macro_rules! { ... } — skip (templates only).
    macro_depth: int | None = None

    def current_mod() -> list[str]:
        return base_mod + [m for _, m in mod_stack]

    i = 0
    while i < len(lines):
        line = lines[i]

        if macro_depth is not None:
            depth += net_brace_delta(line)
            if depth <= macro_depth:
                macro_depth = None
                pending_attrs.clear()
            i += 1
            continue

        if MACRO_RULES_RE.search(line) or (
            "macro_rules!" in line and "{" in line
        ):
            pending_attrs.clear()
            entry = depth
            depth += net_brace_delta(line)
            if depth > entry:
                macro_depth = entry
            i += 1
            continue

        attr = parse_attr(lines, i)
        if attr is not None and not attr.inner:
            pending_attrs.append(attr.body)
            for k in range(attr.start, attr.end):
                depth += net_brace_delta(lines[k])
            while mod_stack and depth <= mod_stack[-1][0]:
                mod_stack.pop()
            i = attr.end
            continue

        m_inline = MOD_INLINE_RE.match(line)
        if m_inline and "{" in line:
            mod_stack.append((depth, m_inline.group("name")))
            pending_attrs.clear()
            depth += net_brace_delta(line)
            while mod_stack and depth <= mod_stack[-1][0]:
                mod_stack.pop()
            i += 1
            continue

        m_fn = FN_RE.match(line)
        if m_fn and any(is_test_attr(a) for a in pending_attrs):
            if m_fn.group("async"):
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
            depth += net_brace_delta(line)
            while mod_stack and depth <= mod_stack[-1][0]:
                mod_stack.pop()
            i += 1
            continue

        if line.strip() and not is_blank_or_comment(line):
            if pending_attrs:
                pending_attrs.clear()

        depth += net_brace_delta(line)
        while mod_stack and depth <= mod_stack[-1][0]:
            mod_stack.pop()
        i += 1

    return tests


def transform_file(file: Path, tests_in_file: list[TestFn]) -> None:
    if not tests_in_file:
        return
    by_line = {t.line_fn: t for t in tests_in_file}
    lines = file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    plain = [ln.rstrip("\n\r") for ln in lines]
    out: list[str] = []
    i = 0
    while i < len(lines):
        attr = parse_attr(plain, i)
        if attr is not None and not attr.inner:
            # Scan forward through attrs / docs / blanks to a known test fn.
            j = i
            while j < len(lines):
                if j in by_line:
                    break
                a2 = parse_attr(plain, j)
                if a2 is not None:
                    j = a2.end
                    continue
                if is_blank_or_comment(plain[j]):
                    j += 1
                    continue
                break
            if j < len(lines) and j in by_line:
                k = i
                while k < j:
                    a2 = parse_attr(plain, k)
                    if a2 is not None:
                        if is_test_attr(a2.body):
                            k = a2.end
                            continue
                        out.extend(lines[a2.start : a2.end])
                        k = a2.end
                        continue
                    out.append(lines[k])
                    k += 1
                fn_line = lines[j]
                m = FN_RE.match(fn_line.rstrip("\n\r"))
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


def _vis_already_crate_visible(vis: str | None) -> bool:
    if not vis:
        return False
    v = vis.strip()
    return v == "pub" or v == "pub(crate)"


def _line_make_mod_pub_crate(line: str) -> str:
    """Rewrite `mod name` / `pub(super) mod name` → `pub(crate) mod name`."""
    ended = ""
    core = line
    if line.endswith("\n"):
        ended = "\n"
        core = line[:-1]
    if core.endswith("\r"):
        ended = "\r" + ended
        core = core[:-1]
    m = MOD_DECL_RE.match(core)
    if not m:
        return line
    if _vis_already_crate_visible(m.group("vis")):
        return line
    indent = m.group("indent") or ""
    rest = re.sub(r"^pub(?:\([^)]*\))?\s+", "", core[len(indent) :])
    return f"{indent}pub(crate) {rest}{ended}"


def pubify_modules_for_tests(
    file_to_mod: dict[Path, list[str]], tests: list[TestFn]
) -> None:
    """Make every module on a test path `pub(crate)` so crate-root main can call in.

    Libtest can invoke #[test]s in private modules; our generated main cannot,
    unless ancestor mods are visible (and the fn is pub(crate), already done).
    """
    needed: set[tuple[str, ...]] = set()
    for t in tests:
        for i in range(len(t.module_path)):
            needed.add(tuple(t.module_path[: i + 1]))
    if not needed:
        return

    for file, base_mod in file_to_mod.items():
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True
            )
        except OSError:
            continue
        plain = [ln.rstrip("\n\r") for ln in lines]
        changed = False
        mod_stack: list[tuple[int, str]] = []
        depth = 0
        i = 0
        while i < len(lines):
            attr = parse_attr(plain, i)
            if attr is not None:
                for k in range(attr.start, attr.end):
                    depth += plain[k].count("{") - plain[k].count("}")
                while mod_stack and depth <= mod_stack[-1][0]:
                    mod_stack.pop()
                i = attr.end
                continue

            m_inline = MOD_INLINE_RE.match(plain[i])
            m_semi = MOD_SEMI_RE.match(plain[i])
            m_mod = m_inline or m_semi
            if m_mod:
                name = m_mod.group("name")
                full = tuple(base_mod + [m for _, m in mod_stack] + [name])
                if full in needed:
                    new_line = _line_make_mod_pub_crate(lines[i])
                    if new_line != lines[i]:
                        lines[i] = new_line
                        plain[i] = new_line.rstrip("\n\r")
                        changed = True
                if m_inline and "{" in plain[i]:
                    mod_stack.append((depth, name))
                depth += plain[i].count("{") - plain[i].count("}")
                while mod_stack and depth <= mod_stack[-1][0]:
                    mod_stack.pop()
                i += 1
                continue

            depth += plain[i].count("{") - plain[i].count("}")
            while mod_stack and depth <= mod_stack[-1][0]:
                mod_stack.pop()
            i += 1

        if changed:
            file.write_text("".join(lines), encoding="utf-8")


def rust_path(mod_path: list[str], name: str) -> str:
    """Path used from crate-root main. Prefix crate:: to avoid str/type clashes."""
    if not mod_path:
        return name
    return "crate::" + "::".join(mod_path + [name])


def generate_main(tests: list[TestFn], *, indent: str = "") -> str:
    lines: list[str] = [
        "",
        "// ----- generated by external-tester --own-main -----",
        "#[allow(unused_mut, unused_variables)]",
        "fn main() {",
        "    // Mimic libtest: non-flag CLI args are substring filters on test names.",
        "    // (e.g. process_spawning's child invoke passes \"child\" and must run nothing.)",
        "    let filters: ::std::vec::Vec<::std::string::String> = ::std::env::args()",
        "        .skip(1)",
        "        .filter(|a| !a.starts_with('-'))",
        "        .collect();",
        "    let matches = |name: &str| -> bool {",
        "        filters.is_empty() || filters.iter().any(|f| name.contains(f.as_str()))",
        "    };",
        "    let mut passed: u32 = 0;",
        "    let mut failed: u32 = 0;",
        "    let mut ignored: u32 = 0;",
        "",
    ]

    for t in tests:
        full = rust_path(t.module_path, t.name)
        uncond_ignore = any(is_unconditional_ignore(a) for a in t.attrs)
        ignore_preds = [p for a in t.attrs if (p := cfg_attr_ignore_pred(a))]
        should_panic = any(is_should_panic_attr(a) for a in t.attrs)
        cfgs = [
            a
            for a in t.attrs
            if is_cfg_attr(a)
            and not is_unconditional_ignore(a)
            and cfg_attr_ignore_pred(a) is None
        ]

        for c in cfgs:
            lines.append(f"    #[{c}]")
        lines.append("    {")
        lines.append(f'        if matches("{full}") {{')
        lines.append(f'        eprint!("test {full} ... ");')
        if uncond_ignore:
            lines.append('        eprintln!("ignored");')
            lines.append("        ignored += 1;")
        else:
            run_lines: list[str] = []
            if should_panic:
                run_lines += [
                    "        let r = ::std::panic::catch_unwind(|| {",
                    f"            {full}();",
                    "        });",
                    "        if r.is_err() {",
                    '            eprintln!("ok");',
                    "            passed += 1;",
                    "        } else {",
                    '            eprintln!("FAILED (expected panic)");',
                    "            failed += 1;",
                    "        }",
                ]
            else:
                run_lines += [
                    "        let r = ::std::panic::catch_unwind(|| {",
                    f"            {full}();",
                    "        });",
                    "        if r.is_ok() {",
                    '            eprintln!("ok");',
                    "            passed += 1;",
                    "        } else {",
                    '            eprintln!("FAILED");',
                    "            failed += 1;",
                    "        }",
                ]
            if ignore_preds:
                if len(ignore_preds) == 1:
                    any_pred = ignore_preds[0]
                else:
                    any_pred = f"any({', '.join(ignore_preds)})"
                lines.append(f"        #[cfg({any_pred})]")
                lines.append("        {")
                lines.append('            eprintln!("ignored");')
                lines.append("            ignored += 1;")
                lines.append("        }")
                lines.append(f"        #[cfg(not({any_pred}))]")
                lines.append("        {")
                for rl in run_lines:
                    lines.append("    " + rl if rl.strip() else rl)
                lines.append("        }")
            else:
                lines.extend(run_lines)
        lines.append("        }")  # end matches()
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


def extract_crate_cfg(lines: list[str]) -> tuple[str | None, list[str]]:
    """Return (cfg_predicate, lines_without that #![cfg(...)])."""
    pred = None
    out: list[str] = []
    plain = [ln.rstrip("\n\r") for ln in lines]
    i = 0
    while i < len(lines):
        attr = parse_attr(plain, i)
        if attr is not None and attr.inner and pred is None:
            m = CRATE_CFG_BODY_RE.match(attr.body)
            if m:
                pred = m.group("pred").strip()
                i = attr.end
                continue
        out.append(lines[i])
        i += 1
    return pred, out


def hoist_inner_attrs_and_fix_mods(
    root: Path, text: str
) -> tuple[str, str]:
    """Prepare body text that will live inside `mod __et_tests`.

    Returns (hoisted_crate_attrs, body_for_mod):
    - hoist `#![...]` to the real crate root (features, etc.)
    - rewrite bare `mod foo;` to `#[path = "..."]` so siblings still resolve
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return "", text
    plain = [ln.rstrip("\n\r") for ln in lines]
    hoisted: list[str] = []
    body: list[str] = []
    i = 0
    pending_path = False
    while i < len(lines):
        attr = parse_attr(plain, i)
        if attr is not None and attr.inner:
            hoisted.append(f"#![{attr.body}]\n")
            i = attr.end
            continue
        if attr is not None:
            if PATH_ATTR_RE.match(attr.body):
                pending_path = True
            body.extend(lines[attr.start : attr.end])
            i = attr.end
            continue

        m = MOD_SEMI_RE.match(plain[i])
        if m and not pending_path:
            name = m.group("name")
            rel = mod_path_attr_for(root, name)
            if rel is not None:
                indent_m = re.match(r"^(\s*)", plain[i])
                indent = indent_m.group(1) if indent_m else ""
                body.append(f'{indent}#[path = "{rel}"]\n')
            body.append(lines[i])
            pending_path = False
            i += 1
            continue

        pending_path = False
        body.append(lines[i])
        i += 1

    return "".join(hoisted), "".join(body)


def wrap_cfg_crate(
    root: Path,
    crate_cfg: str,
    wrap_mod: str,
    body_text: str,
    main_src: str | None,
) -> None:
    """Write dual-main cfg wrap with hoisted attrs and fixed mod paths.

    Body goes in a sibling `{wrap_mod}.rs` (not an inline mod) so `#[path]` and
    `mod common` resolve against the staging directory, not a virtual subdir.
    """
    hoisted, body = hoist_inner_attrs_and_fix_mods(root, body_text)
    body_file = root.parent / f"{wrap_mod}.rs"
    if not body.endswith("\n"):
        body = body + "\n"
    body_file.write_text(body, encoding="utf-8")

    parts: list[str] = []
    if hoisted:
        parts.append(hoisted)
    parts.append(f"#[cfg({crate_cfg})]\n")
    parts.append(f"mod {wrap_mod};\n")
    parts.append(f"#[cfg(not({crate_cfg}))]\n")
    parts.append("fn main() {}\n")
    if main_src is None:
        parts.append(f"#[cfg({crate_cfg})]\n")
        parts.append("fn main() {}\n")
    else:
        parts.append(f"#[cfg({crate_cfg})]\n")
        parts.append(main_src.lstrip())
    root.write_text("".join(parts), encoding="utf-8")


def insert_register_tool(text: str) -> str:
    if "register_tool(et)" in text:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inserted = False
    for ln in lines:
        if not inserted:
            s = ln.strip()
            # Keep leading inner docs / crate attrs first, then insert.
            if s.startswith("//!") or s.startswith("#![") or s == "" or s.startswith("//"):
                out.append(ln)
                continue
            out.append("#![register_tool(et)]\n")
            inserted = True
        out.append(ln)
    if not inserted:
        out.insert(0, "#![register_tool(et)]\n")
    return "".join(out)


def mark_tests_in_tree(root: Path, search_root: Path | None = None) -> int:
    """Replace #[test] with #[et::own_test] under search_root (default: root.parent)."""
    n = 0
    base = search_root if search_root is not None else root.parent
    for path in sorted(base.rglob("*.rs")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "#[test]" not in text and "#[test " not in text:
            if path.resolve() == root.resolve():
                new = insert_register_tool(text)
                if new != text:
                    path.write_text(new, encoding="utf-8")
            continue
        new = text.replace("#[test]", "#[et::own_test]")
        if path.resolve() == root.resolve():
            new = insert_register_tool(new)
        if new != text:
            path.write_text(new, encoding="utf-8")
            n += 1
    return n


def strip_all_tests_make_callable(path: Path) -> None:
    """Strip every #[test] (including inside macro_rules) and pub(crate) the fn.

    Used on the *original* sources after discovery-via-expand, so macro expansion
    at compile time yields callable pub(crate) items matching expanded paths.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    plain = [ln.rstrip("\n\r") for ln in lines]
    out: list[str] = []
    i = 0
    while i < len(lines):
        attr = parse_attr(plain, i)
        if attr is not None and not attr.inner and is_test_attr(attr.body):
            # Collect attr run (test + siblings) through docs/blanks to fn.
            j = i
            while j < len(lines):
                a2 = parse_attr(plain, j)
                if a2 is not None:
                    j = a2.end
                    continue
                if is_blank_or_comment(plain[j]):
                    j += 1
                    continue
                break
            if j < len(lines) and FN_RE.match(plain[j]):
                k = i
                while k < j:
                    a2 = parse_attr(plain, k)
                    if a2 is not None:
                        if is_test_attr(a2.body):
                            k = a2.end
                            continue
                        out.extend(lines[a2.start : a2.end])
                        k = a2.end
                        continue
                    out.append(lines[k])
                    k += 1
                fn_line = lines[j]
                m = FN_RE.match(fn_line.rstrip("\n\r"))
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
    text = "".join(out)
    # Macro templates that emit modules: `mod $name {` / `mod $name;` — not
    # matcher fragments like `mod $name:ident`.
    text = re.sub(r"\bmod\s+(\$\w+)\s*\{", r"pub(crate) mod \1 {", text)
    text = re.sub(r"\bmod\s+(\$\w+)\s*;", r"pub(crate) mod \1;", text)
    path.write_text(text, encoding="utf-8")


def prepare_sources_after_discovery(root: Path, tests: list[TestFn]) -> None:
    """Rewrite original crate so expanded #[test]s become callable pub(crate) items."""
    for path in sorted(root.parent.rglob("*.rs")):
        strip_all_tests_make_callable(path)
    file_to_mod = collect_mod_map(root)
    pubify_modules_for_tests(file_to_mod, tests)


def expand_and_discover_tests(root: Path) -> list[TestFn] | None:
    """Copy tree, mark #[test]→#[et::own_test], expand, discover. Does not modify root."""
    import os
    import shlex
    import shutil
    import subprocess
    import tempfile

    rustc = os.environ.get("ET_RUSTC")
    if not rustc:
        return None

    # Copy enough of the package that #[path]/sibling modules still resolve.
    src_dir = root.parent
    if (
        root.name == "lib.rs"
        and src_dir.name == "tests"
        and (src_dir.parent / "testing").is_dir()
    ):
        # alloctests: tests/… → ../../testing
        src_dir = src_dir.parent
    elif (src_dir.parent / "common").is_dir() and not (src_dir / "common").is_dir():
        # sync (and similar): staged as <work>/sync/lib.rs + <work>/common/
        src_dir = src_dir.parent

    tmp_home = Path(tempfile.mkdtemp(prefix="et-own-expand-"))
    try:
        dst_dir = tmp_home / src_dir.name
        shutil.copytree(src_dir, dst_dir)
        tmp_root = dst_dir / root.relative_to(src_dir)
        if not tmp_root.is_file():
            return None

        mark_tests_in_tree(tmp_root, search_root=dst_dir)

        edition = os.environ.get("ET_EDITION", "2024")
        cmd = [rustc, "-Zunpretty=expanded", "--edition", edition]
        target = os.environ.get("ET_TARGET", "").strip()
        if target:
            cmd += ["--target", target]
        extra = os.environ.get("ET_EXPAND_FLAGS", "").strip()
        if extra:
            cmd += shlex.split(extra)
        cmd.append(str(tmp_root))

        env = os.environ.copy()
        env.setdefault("RUSTC_BOOTSTRAP", "1")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_root.parent),
            check=False,
        )
        if not proc.stdout.strip() or "#[et::own_test]" not in proc.stdout:
            err = (proc.stderr or "").strip().splitlines()
            hint = next((ln for ln in err if ln.startswith("error")), None)
            if hint is None:
                hint = err[-1] if err else f"exit {proc.returncode}"
            print(
                f"own-main: macro expand failed ({hint}); using unexpanded source",
                file=sys.stderr,
            )
            if proc.stderr:
                for ln in err[:8]:
                    print(f"own-main:   {ln}", file=sys.stderr)
            return None

        expanded_path = tmp_home / "expanded.rs"
        expanded_path.write_text(proc.stdout, encoding="utf-8")
        tests = discover_tests_in_file(expanded_path, [])
        for t in tests:
            t.file = root
        return tests
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


def fix_empty_module_paths(root: Path, tests: list[TestFn]) -> int:
    """Fill blank module paths using unexpanded-tree discovery (name match).

    Expanded discovery can drop a parent mod when brace tracking desyncs; the
    original sources still have the correct file/module placement for unique names.
    """
    file_to_mod = collect_mod_map(root)
    by_name: dict[str, list[list[str]]] = {}
    for file, mod_path in file_to_mod.items():
        for t in discover_tests_in_file(file, mod_path):
            by_name.setdefault(t.name, []).append(list(t.module_path))
    fixed = 0
    for t in tests:
        if t.module_path:
            continue
        cands = by_name.get(t.name, [])
        # Prefer a non-empty unique path.
        nonempty = [p for p in cands if p]
        if len(nonempty) == 1:
            t.module_path = nonempty[0]
            fixed += 1
        elif len(cands) == 1:
            t.module_path = cands[0]
            fixed += 1
    return fixed


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

    wrap_mod = "__et_tests"
    if crate_cfg is not None:
        root.write_text("".join(body_lines), encoding="utf-8")

    expanded_tests = expand_and_discover_tests(root)
    how = "source"
    if expanded_tests is not None:
        all_tests = expanded_tests
        how = "expanded"
        fixed = fix_empty_module_paths(root, all_tests)
        print(f"own-main: discovered {len(all_tests)} tests via macro expansion")
        if fixed:
            print(f"own-main: repaired {fixed} module paths from source tree")
        prepare_sources_after_discovery(root, all_tests)
    else:
        file_to_mod = collect_mod_map(root)
        all_tests = []
        for file, mod_path in file_to_mod.items():
            all_tests.extend(discover_tests_in_file(file, mod_path))
        if all_tests:
            by_file: dict[Path, list[TestFn]] = {}
            for t in all_tests:
                by_file.setdefault(t.file, []).append(t)
            for file, ts in by_file.items():
                transform_file(file, ts)
            pubify_modules_for_tests(file_to_mod, all_tests)

    if not all_tests:
        if crate_cfg is not None:
            wrap_cfg_crate(
                root,
                crate_cfg,
                wrap_mod,
                "".join(body_lines),
                main_src=None,
            )
            print(f"own-main: no tests after cfg; stub main for {root.name}")
            return 0
        print(f"own-main: no #[test] functions found in {root}", file=sys.stderr)
        return 1

    if crate_cfg is not None:
        for t in all_tests:
            t.module_path = [wrap_mod] + t.module_path
        transformed = root.read_text(encoding="utf-8", errors="replace")
        main_inner = generate_main(all_tests)
        wrap_cfg_crate(root, crate_cfg, wrap_mod, transformed, main_inner)
    else:
        main_src = generate_main(all_tests)
        with root.open("a", encoding="utf-8") as f:
            f.write(main_src)

    print(f"own-main: {len(all_tests)} tests wired in {root.name} ({how})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
