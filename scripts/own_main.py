#!/usr/bin/env python3
"""Rewrite a staged test crate for --own-main (no libtest).

Discovers #[test] functions, makes them pub, strips #[test], and writes
et_own_tests.json. The harness scripts rewrite main + recompile once per test;
each binary only calls that test and exits — pass/fail is the exit code.
No catch_unwind, no process::Command, no env orchestration inside the binary.

Usage:
  own_main.py prepare <crate-root.rs>   # transform sources + write manifest
  own_main.py set-main <crate-root.rs> <index>  # write main for one test
"""

from __future__ import annotations

import json
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
                    out.append(f"{indent}pub {rest}")
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
    """Rewrite `mod name` / `pub(super) mod name` → `pub mod name`."""
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
    return f"{indent}pub {rest}{ended}"


def pubify_modules_for_tests(
    file_to_mod: dict[Path, list[str]], tests: list[TestFn]
) -> None:
    """Make every module on a test path `pub` so an extern thin bin can call in.

    Libtest can invoke #[test]s in private modules; our generated main cannot,
    unless ancestor mods are visible (and the fn is pub, already done).
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


OWN_MAIN_BEGIN = "// ----- generated by external-tester --own-main -----"
OWN_MAIN_END = "// ----- end own-main -----"
MANIFEST_NAME = "et_own_tests.json"
SKIP_EXIT = 77  # cfg/ignore: not applicable on this target


def manifest_path_for(root: Path) -> Path:
    return root.parent / MANIFEST_NAME


def write_manifest(root: Path, tests: list[TestFn], *, crate_cfg: str | None, how: str) -> Path:
    # Match rustc's default crate name from the file stem so type_name-based
    # tests (e.g. type-name-unsized) keep seeing the same path prefix.
    stem = root.stem.replace("-", "_")
    if not stem.isidentifier():
        stem = re.sub(r"[^A-Za-z0-9_]", "_", stem)
        if not stem or stem[0].isdigit():
            stem = f"t_{stem}"
    crate_name = stem
    data = {
        "root": str(root),
        "crate_cfg": crate_cfg,
        "crate_name": crate_name,
        "discovery": how,
        "skip_exit": SKIP_EXIT,
        "tests": [test_meta(t) for t in tests],
    }
    mp = manifest_path_for(root)
    mp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return mp


def test_meta(t: TestFn) -> dict:
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
    return {
        "path": full,
        "name": t.name,
        "should_panic": should_panic,
        "ignore": uncond_ignore,
        "cfgs": cfgs,
        "ignore_preds": ignore_preds,
    }


def generate_single_test_main(t: TestFn) -> str:
    """Minimal main: call one test (or exit 77 if cfg'd out). Exit code = result."""
    full = rust_path(t.module_path, t.name)
    ignore_preds = [p for a in t.attrs if (p := cfg_attr_ignore_pred(a))]
    cfgs = [
        a
        for a in t.attrs
        if is_cfg_attr(a)
        and not is_unconditional_ignore(a)
        and cfg_attr_ignore_pred(a) is None
    ]

    lines: list[str] = [
        "",
        OWN_MAIN_BEGIN,
        "// One test per binary. Orchestration lives in external-tester scripts.",
        "// Exit 0 = ran to completion; non-zero = panic/abort/fail; "
        f"{SKIP_EXIT} = skipped (cfg).",
        "fn main() {",
        "    // process_spawning re-execs this binary with a filter arg; do nothing.",
        "    if ::std::env::args().len() > 1 {",
        "        return;",
        "    }",
    ]

    body: list[str] = []
    if ignore_preds:
        if len(ignore_preds) == 1:
            any_pred = ignore_preds[0]
        else:
            any_pred = f"any({', '.join(ignore_preds)})"
        body += [
            f"    #[cfg({any_pred})]",
            "    {",
            f"        ::std::process::exit({SKIP_EXIT});",
            "    }",
            f"    #[cfg(not({any_pred}))]",
            "    {",
            f"        {full}();",
            "    }",
        ]
    else:
        body.append(f"    {full}();")

    if cfgs:
        for c in cfgs:
            lines.append(f"    #[{c}]")
        lines.append("    {")
        for ln in body:
            lines.append("    " + ln)
        lines.append("    }")
        # Build not(all(...)) or not(cfg) for skip
        preds = []
        for c in cfgs:
            # c is like 'cfg(unix)' — strip cfg(...) wrapper for not()
            if c.startswith("cfg(") and c.endswith(")"):
                preds.append(c[4:-1])
            else:
                preds.append(c)
        if len(preds) == 1:
            not_pred = f"not({preds[0]})"
        else:
            not_pred = f"not(all({', '.join(preds)}))"
        lines.append(f"    #[cfg({not_pred})]")
        lines.append("    {")
        lines.append(f"        ::std::process::exit({SKIP_EXIT});")
        lines.append("    }")
    else:
        lines.extend(body)

    lines += [
        "}",
        OWN_MAIN_END,
        "",
    ]
    return "\n".join(lines)


def strip_own_main_block(text: str) -> str:
    begin = text.find(OWN_MAIN_BEGIN)
    if begin < 0:
        return text
    before = text[:begin]
    cfg_at = before.rfind("#[cfg(")
    if cfg_at >= 0 and before[cfg_at:].count("\n") <= 2:
        begin = cfg_at
    end = text.find(OWN_MAIN_END, begin)
    if end < 0:
        return text[:begin].rstrip() + "\n"
    end = end + len(OWN_MAIN_END)
    while end < len(text) and text[end] in "\r\n":
        end += 1
    return text[:begin].rstrip() + "\n" + text[end:]


def set_main_for_index(root: Path, index: int) -> dict:
    mp = manifest_path_for(root)
    if not mp.is_file():
        raise SystemExit(f"error: missing manifest {mp} (run prepare first)")
    data = json.loads(mp.read_text(encoding="utf-8"))
    tests = data["tests"]
    if index < 0 or index >= len(tests):
        raise SystemExit(f"error: test index {index} out of range 0..{len(tests) - 1}")
    meta = tests[index]
    if meta.get("ignore"):
        raise SystemExit(f"error: test {index} is #[ignore]; do not compile")

    path_parts = meta["path"].split("::")
    if path_parts[0] == "crate":
        module_path = path_parts[1:-1]
        name = path_parts[-1]
    else:
        module_path = []
        name = meta["path"]
    attrs: list[str] = list(meta.get("cfgs") or [])
    for p in meta.get("ignore_preds") or []:
        attrs.append(f"cfg_attr({p}, ignore)")
    if meta.get("should_panic"):
        attrs.append("should_panic")
    t = TestFn(
        file=root,
        module_path=module_path,
        name=name,
        attrs=attrs,
        line_fn=0,
    )
    main_src = generate_single_test_main(t)

    text = root.read_text(encoding="utf-8", errors="replace")
    text = strip_own_main_block(text)
    crate_cfg = data.get("crate_cfg")
    if crate_cfg:
        stub = f"#[cfg({crate_cfg})]\nfn main() {{}}\n"
        text = text.replace(stub, "")
        dangling = f"#[cfg({crate_cfg})]"
        rs = text.rstrip()
        if rs.endswith(dangling):
            text = rs[: -len(dangling)].rstrip() + "\n"
        text = text.rstrip() + f"\n#[cfg({crate_cfg})]\n{main_src.lstrip()}"
    else:
        text = text.rstrip() + "\n" + main_src
    root.write_text(text, encoding="utf-8")
    return meta



def crate_has_main(root: Path) -> bool:
    text = root.read_text(encoding="utf-8", errors="replace")
    text = strip_own_main_block(text)
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
    parts.append(f"pub mod {wrap_mod};\n")
    parts.append(f"#[cfg(not({crate_cfg}))]\n")
    parts.append(f"fn main() {{ ::std::process::exit({SKIP_EXIT}); }}\n")
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
    """Strip every #[test] (including inside macro_rules) and make the fn pub.

    Used on the *original* sources after discovery-via-expand, so macro expansion
    at compile time yields callable pub items matching expanded paths.
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
                    out.append(f"{indent}pub {rest}")
                else:
                    out.append(fn_line)
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    text = "".join(out)
    # Macro templates that emit modules: `mod $name {` / `mod $name;` — not
    # matcher fragments like `mod $name:ident`.
    text = re.sub(r"\bmod\s+(\$\w+)\s*\{", r"pub mod \1 {", text)
    text = re.sub(r"\bmod\s+(\$\w+)\s*;", r"pub mod \1;", text)
    path.write_text(text, encoding="utf-8")


def inject_allow_dead_code(root: Path) -> None:
    """One-test-per-binary leaves sibling tests unused; neutralize deny(warnings)."""
    inject = "#![allow(dead_code, unused_imports, unused_variables)] // et_own_main_dead_code\n"
    for path in sorted(root.parent.rglob("*.rs")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "et_own_main_dead_code" in text:
            continue
        lines = text.splitlines(keepends=True)
        plain = [ln.rstrip("\n\r") for ln in lines]
        i = 0
        # Skip shebang / inner docs / inner attrs (including multi-line).
        while i < len(plain):
            s = plain[i].strip()
            if s == "" or s.startswith("//"):
                i += 1
                continue
            attr = parse_attr(plain, i)
            if attr is not None and attr.inner:
                i = attr.end
                continue
            if s.startswith("//!"):
                i += 1
                continue
            break
        out = lines[:i] + [inject] + lines[i:]
        path.write_text("".join(out), encoding="utf-8")


def prepare_sources_after_discovery(root: Path, tests: list[TestFn]) -> None:
    """Rewrite original crate so expanded #[test]s become callable pub items."""
    for path in sorted(root.parent.rglob("*.rs")):
        strip_all_tests_make_callable(path)
    file_to_mod = collect_mod_map(root)
    pubify_modules_for_tests(file_to_mod, tests)
    inject_allow_dead_code(root)


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


def cmd_prepare(root: Path) -> int:
    if crate_has_main(root):
        # Still write an empty manifest so the harness can treat this as a
        # single "binary already has main" case (e.g. pipe_subprocess).
        write_manifest(root, [], crate_cfg=None, how="has-main")
        print(f"own-main: skip prepare {root.name} (already has main)")
        return 0

    raw_lines = root.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    crate_cfg, body_lines = extract_crate_cfg(raw_lines)

    wrap_mod = "__et_tests"
    if crate_cfg is not None:
        root.write_text("".join(body_lines), encoding="utf-8")

    expanded_tests = expand_and_discover_tests(root)
    how = "source"
    all_tests: list[TestFn] = []
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
        for file, mod_path in file_to_mod.items():
            all_tests.extend(discover_tests_in_file(file, mod_path))
        if all_tests:
            by_file: dict[Path, list[TestFn]] = {}
            for t in all_tests:
                by_file.setdefault(t.file, []).append(t)
            for file, ts in by_file.items():
                transform_file(file, ts)
            pubify_modules_for_tests(file_to_mod, all_tests)
            inject_allow_dead_code(root)

    if not all_tests:
        if crate_cfg is not None:
            wrap_cfg_crate(
                root,
                crate_cfg,
                wrap_mod,
                "".join(body_lines),
                main_src=None,
            )
            write_manifest(root, [], crate_cfg=crate_cfg, how=how)
            print(f"own-main: no tests after cfg; stub main for {root.name}")
            return 0
        print(f"own-main: no #[test] functions found in {root}", file=sys.stderr)
        return 1

    if crate_cfg is not None:
        for t in all_tests:
            t.module_path = [wrap_mod] + t.module_path
        transformed = root.read_text(encoding="utf-8", errors="replace")
        wrap_cfg_crate(root, crate_cfg, wrap_mod, transformed, main_src=None)
    # else: sources prepared, no main yet — set-main adds it per test

    mp = write_manifest(root, all_tests, crate_cfg=crate_cfg, how=how)
    print(f"own-main: prepared {len(all_tests)} tests in {root.name} ({how})")
    print(f"own-main: manifest {mp.name}")
    return 0


def cmd_set_main(root: Path, index: int) -> int:
    meta = set_main_for_index(root, index)
    print(f"own-main: main -> {meta['path']}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(
            f"usage: {sys.argv[0]} prepare <crate-root.rs>\n"
            f"       {sys.argv[0]} set-main <crate-root.rs> <index>",
            file=sys.stderr,
        )
        return 2

    if sys.argv[1] not in ("prepare", "set-main") and not sys.argv[1].startswith("-"):
        cmd, args = "prepare", sys.argv[1:]
    else:
        cmd, args = sys.argv[1], sys.argv[2:]

    if cmd == "prepare":
        if len(args) != 1:
            print(f"usage: {sys.argv[0]} prepare <crate-root.rs>", file=sys.stderr)
            return 2
        root = Path(args[0]).resolve()
        if not root.is_file():
            print(f"error: not a file: {root}", file=sys.stderr)
            return 1
        return cmd_prepare(root)

    if cmd == "set-main":
        if len(args) != 2:
            print(f"usage: {sys.argv[0]} set-main <crate-root.rs> <index>", file=sys.stderr)
            return 2
        root = Path(args[0]).resolve()
        if not root.is_file():
            print(f"error: not a file: {root}", file=sys.stderr)
            return 1
        return cmd_set_main(root, int(args[1]))

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
