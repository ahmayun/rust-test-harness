#!/usr/bin/env bash
# Shared helpers for the external-tester harness.

set -euo pipefail

ET_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ET_OUT="${ET_OUT:-$ET_ROOT/out}"
ET_TIERS="$ET_ROOT/tiers"
ET_SCRIPTS="$ET_ROOT/scripts"
ET_FIX_FEATURES="$ET_SCRIPTS/fix_features.py"
ET_OWN_MAIN_PY="$ET_SCRIPTS/own_main.py"

# Set by et_prepare_deps
ET_DEP_LFLAGS=()
ET_DEP_READY=0

# Cross-compile target triple (empty = host). Set via --target or ET_TARGET.
ET_TARGET="${ET_TARGET:-}"
# When 1, generate a hand-written main instead of using rustc --test / libtest.
ET_OWN_MAIN="${ET_OWN_MAIN:-0}"

# Counters (global; run.sh resets them)
# Files = allowlisted crate roots. Tests = individual #[test] cases from harness output.
ET_COMPILE_OK=0
ET_COMPILE_FAIL=0
ET_RUN_OK=0
ET_RUN_FAIL=0
ET_SKIP=0
ET_TEST_PASS=0
ET_TEST_FAIL=0
ET_TEST_IGNORE=0
# Back-compat aliases used in older messages (files that ran ok / failed)
ET_PASS=0
ET_FAIL=0

et_die() {
  echo "error: $*" >&2
  exit 1
}

et_resolve_rustc() {
  local rustc="$1"
  if [[ ! -x "$rustc" ]]; then
    et_die "rustc is not executable: $rustc"
  fi
  if command -v realpath >/dev/null 2>&1; then
    realpath "$rustc"
  else
    cd "$(dirname "$rustc")" && echo "$(pwd)/$(basename "$rustc")"
  fi
}

et_default_library_src() {
  echo "$ET_ROOT/../rust/library"
}

et_resolve_library_src() {
  local src="${RUST_LIBRARY_SRC:-$(et_default_library_src)}"
  if [[ ! -d "$src" ]]; then
    et_die "library source not found: $src (set RUST_LIBRARY_SRC)"
  fi
  if command -v realpath >/dev/null 2>&1; then
    realpath "$src"
  else
    (cd "$src" && pwd)
  fi
}

et_find_cargo() {
  if [[ -n "${ET_CARGO:-}" && -x "${ET_CARGO}" ]]; then
    echo "$ET_CARGO"
    return 0
  fi
  if command -v cargo >/dev/null 2>&1; then
    command -v cargo
    return 0
  fi
  if [[ -x "$HOME/.cargo/bin/cargo" ]]; then
    echo "$HOME/.cargo/bin/cargo"
    return 0
  fi
  return 1
}

# Directory under cargo target dir where release deps land.
et_cargo_deps_libdir() {
  local target_dir="$1"
  if [[ -n "${ET_TARGET:-}" ]]; then
    echo "$target_dir/$ET_TARGET/release/deps"
  else
    echo "$target_dir/release/deps"
  fi
}

# Build rand / rand_xorshift with host cargo + the given rustc (offline when possible).
et_prepare_deps() {
  local cargo
  if ! cargo="$(et_find_cargo)"; then
    echo "warning: no host cargo found; tests needing rand will fail to compile" >&2
    ET_DEP_READY=0
    ET_DEP_LFLAGS=()
    return 0
  fi

  local reg_root="${CARGO_HOME:-$HOME/.cargo}/registry/src"
  local rand_src xor_src
  rand_src="$(find "$reg_root" -maxdepth 2 -type d -name 'rand-0.9.*' 2>/dev/null | sort -V | tail -n1 || true)"
  xor_src="$(find "$reg_root" -maxdepth 2 -type d -name 'rand_xorshift-0.4.*' 2>/dev/null | sort -V | tail -n1 || true)"

  local deps_dir="$ET_OUT/deps"
  local manifest_dir="$deps_dir/crate"
  local target_dir="$deps_dir/target"
  mkdir -p "$manifest_dir"

  if [[ -n "$rand_src" && -n "$xor_src" ]]; then
    cat >"$manifest_dir/Cargo.toml" <<EOF
[package]
name = "et-deps"
version = "0.0.0"
edition = "2021"
publish = false

[lib]
path = "lib.rs"

[dependencies]
rand = { path = "$rand_src", default-features = false, features = ["alloc"] }
rand_xorshift = { path = "$xor_src" }
EOF
  else
    cat >"$manifest_dir/Cargo.toml" <<'EOF'
[package]
name = "et-deps"
version = "0.0.0"
edition = "2021"
publish = false

[lib]
path = "lib.rs"

[dependencies]
rand = { version = "0.9.0", default-features = false, features = ["alloc"] }
rand_xorshift = "0.4.0"
EOF
  fi
  echo '#![feature(restricted_std)]' >"$manifest_dir/lib.rs"
  echo 'pub use rand; pub use rand_xorshift;' >>"$manifest_dir/lib.rs"

  echo "Preparing deps (rand, rand_xorshift) with host cargo + given rustc..."
  if [[ -n "${ET_TARGET:-}" ]]; then
    echo "  cross target: $ET_TARGET"
  fi
  # Targets whose std is built with cfg(restricted_std) (e.g. hexagon-unknown-qurt)
  # require #![feature(restricted_std)] on every crate that uses std — including
  # crates.io deps. Inject it via -Zcrate-attr so rand/rand_xorshift get it too.
  local dep_rustflags="${RUSTFLAGS:-} -Zcrate-attr=feature(restricted_std)"
  local cargo_args=(build --release)
  if [[ -n "${ET_TARGET:-}" ]]; then
    cargo_args+=(--target "$ET_TARGET")
  fi
  if [[ -n "$rand_src" && -n "$xor_src" ]]; then
    cargo_args+=(--offline)
  fi

  if ! (
    cd "$manifest_dir"
    CARGO_TARGET_DIR="$target_dir" \
      RUSTC="$ET_RUSTC" \
      RUSTC_BOOTSTRAP=1 \
      RUSTFLAGS="$dep_rustflags" \
      "$cargo" "${cargo_args[@]}"
  ) >"$deps_dir/build.log" 2>&1; then
    echo "warning: deps build failed; see $deps_dir/build.log" >&2
    # Retry online if offline path build failed
    if [[ -n "$rand_src" ]]; then
      echo "warning: retrying deps build without --offline..." >&2
      cat >"$manifest_dir/Cargo.toml" <<'EOF'
[package]
name = "et-deps"
version = "0.0.0"
edition = "2021"
publish = false
[lib]
path = "lib.rs"
[dependencies]
rand = { version = "0.9.0", default-features = false, features = ["alloc"] }
rand_xorshift = "0.4.0"
EOF
      echo '#![feature(restricted_std)]' >"$manifest_dir/lib.rs"
      echo 'pub use rand; pub use rand_xorshift;' >>"$manifest_dir/lib.rs"
      local retry_args=(build --release)
      if [[ -n "${ET_TARGET:-}" ]]; then
        retry_args+=(--target "$ET_TARGET")
      fi
      if ! (
        cd "$manifest_dir"
        CARGO_TARGET_DIR="$target_dir" \
          RUSTC="$ET_RUSTC" \
          RUSTC_BOOTSTRAP=1 \
          RUSTFLAGS="$dep_rustflags" \
          "$cargo" "${retry_args[@]}"
      ) >"$deps_dir/build.log" 2>&1; then
        echo "warning: deps build failed again; continuing without rand" >&2
        ET_DEP_READY=0
        ET_DEP_LFLAGS=()
        return 0
      fi
    else
      ET_DEP_READY=0
      ET_DEP_LFLAGS=()
      return 0
    fi
  fi

  local deps_lib
  deps_lib="$(et_cargo_deps_libdir "$target_dir")"
  local rand_rlib xor_rlib
  rand_rlib="$(ls "$deps_lib"/librand-*.rlib 2>/dev/null | head -n1 || true)"
  xor_rlib="$(ls "$deps_lib"/librand_xorshift-*.rlib 2>/dev/null | head -n1 || true)"
  if [[ -z "$rand_rlib" || -z "$xor_rlib" ]]; then
    echo "warning: built deps but could not find rlibs under $deps_lib" >&2
    ET_DEP_READY=0
    ET_DEP_LFLAGS=()
    return 0
  fi

  ET_DEP_LFLAGS=(
    --extern "rand=$rand_rlib"
    --extern "rand_xorshift=$xor_rlib"
    -L "dependency=$deps_lib"
  )
  ET_DEP_READY=1
  echo "  deps ready: rand + rand_xorshift ($deps_lib)"
}

et_bin_name() {
  local src="$1"
  local name parent
  name="$(basename "$src" .rs)"
  parent="$(basename "$(dirname "$src")")"
  if [[ "$name" == "lib" || "$name" == "mod" ]]; then
    echo "${parent}_${name}"
  else
    echo "$name"
  fi
}

# True if this should be compiled as a binary (fn main), not --test.
et_is_no_harness() {
  local src="$1"
  # Explicit known case + heuristic: has main, no #[test]
  if [[ "$(basename "$src")" == "pipe_subprocess.rs" ]]; then
    return 0
  fi
  if grep -qE '^\s*fn main\s*\(' "$src" 2>/dev/null \
    && ! grep -qE '^\s*#\[test\]' "$src" 2>/dev/null; then
    return 0
  fi
  return 1
}

# Stage a mutable working copy of a test crate root. Prints staged path.
# Never mutates the original library tree.
et_stage_src() {
  local src="$1"
  local name
  name="$(et_bin_name "$src")"
  local work="$ET_OUT/stage/$name"
  rm -rf "$work"
  mkdir -p "$work"

  local abs
  abs="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"
  local staged=""

  # alloctests main harness needs the whole package (tests/testing → ../../testing).
  if [[ "$abs" == */alloctests/tests/lib.rs ]]; then
    local pkg
    pkg="$(cd "$(dirname "$abs")/.." && pwd)"
    cp -a "$pkg" "$work/alloctests"
    staged="$work/alloctests/tests/lib.rs"
  # sync multi-file + sibling common/
  elif [[ "$abs" == */std/tests/sync/lib.rs ]]; then
    local tests_dir
    tests_dir="$(cd "$(dirname "$abs")/.." && pwd)"
    cp -a "$tests_dir/sync" "$work/sync"
    cp -a "$tests_dir/common" "$work/common"
    staged="$work/sync/lib.rs"
  # thread_local multi-file
  elif [[ "$abs" == */std/tests/thread_local/lib.rs ]]; then
    cp -a "$(dirname "$abs")" "$work/thread_local"
    staged="$work/thread_local/lib.rs"
  else
    # Single-file (possibly with mod common)
    local src_dir
    src_dir="$(dirname "$abs")"
    cp -a "$abs" "$work/$(basename "$abs")"
    if grep -qE '^\s*mod common\s*;' "$abs" 2>/dev/null; then
      if [[ -d "$src_dir/common" ]]; then
        cp -a "$src_dir/common" "$work/common"
      fi
    fi
    staged="$work/$(basename "$abs")"
  fi

  # Optional ephemeral #[ignore] patches for known sysroot skew.
  if [[ -f "$ET_TIERS/ignore-tests.txt" ]]; then
    python3 "$ET_SCRIPTS/apply_ignores.py" "$work" "$ET_TIERS/ignore-tests.txt" \
      "external-tester: sysroot/library skew" >/dev/null || true
  fi

  echo "$staged"
}

# Prepare staged crate for --own-main: strip #[test], write et_own_tests.json.
# Does not emit a multi-test orchestrator — scripts compile one binary per test.
et_apply_own_main() {
  local staged="$1"
  if [[ "${ET_OWN_MAIN:-0}" != "1" ]]; then
    return 0
  fi
  local expand_flags=()
  if ((ET_DEP_READY)); then
    expand_flags+=("${ET_DEP_LFLAGS[@]}")
  fi
  if [[ -n "${ET_TARGET:-}" ]]; then
    expand_flags+=('-Zcrate-attr=feature(restricted_std)')
  fi
  # shellcheck disable=SC2086
  ET_RUSTC="$ET_RUSTC" \
  ET_EDITION="${ET_EDITION:-2024}" \
  ET_TARGET="${ET_TARGET:-}" \
  ET_EXPAND_FLAGS="${expand_flags[*]}" \
  RUSTC_BOOTSTRAP="${RUSTC_BOOTSTRAP:-1}" \
    python3 "$ET_OWN_MAIN_PY" prepare "$staged"
}

# Exit codes from a single-test own-main binary.
ET_OWN_SKIP_EXIT=77

# Compile+run every test in an own-main manifest (one binary per test).
# Updates ET_TEST_* counters. Sets ET_LAST_SUMMARY. Returns 0 if no failures.
et_run_own_main_tests() {
  local suite="$1"
  local name="$2"
  local src="$3"
  local staged="$4"
  local suite_dir="$5"

  local manifest
  manifest="$(dirname "$staged")/et_own_tests.json"
  if [[ ! -f "$manifest" ]]; then
    echo "    missing own-main manifest: $manifest" >&2
    return 1
  fi

  local n
  n="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["tests"]))' "$manifest")"
  local skip_exit
  skip_exit="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("skip_exit", 77))' "$manifest")"

  local passed=0 failed=0 ignored=0
  local i meta path should_panic ignore
  local bin compile_log run_log rc
  local any_compile_fail=0

  # Incremental dir shared across per-test rebuilds of the same crate.
  local incr_dir="$suite_dir/.incr-$name"
  mkdir -p "$incr_dir"

  if [[ "$n" -eq 0 ]]; then
    # No #[test]s (or pre-existing main): compile/run the staged root once.
    bin="$suite_dir/$name"
    compile_log="$suite_dir/${name}.compile.log"
    run_log="$suite_dir/${name}.run.log"
    echo "  COMPILE  $suite/$name"
    if ! RUSTFLAGS="${RUSTFLAGS:-} -C incremental=$incr_dir" \
        et_compile_with_fixups "$staged" "$bin" "$compile_log"; then
      echo "  COMPILE-FAIL  $suite/$name"
      echo "    see $compile_log"
      ET_COMPILE_FAIL=$((ET_COMPILE_FAIL + 1))
      return 1
    fi
    ET_COMPILE_OK=$((ET_COMPILE_OK + 1))
    echo "  RUN      $suite/$name"
    set +e
    ET_TEST_SUITE="$suite" \
    ET_TEST_NAME="$name" \
    ET_TEST_SRC="$src" \
    ET_TEST_STAGED="$staged" \
    ET_TEST_NO_HARNESS=1 \
    ET_TEST_TARGET="${ET_TARGET:-}" \
    ET_RUSTC="$ET_RUSTC" \
      "$ET_RUNNER" "$bin" >"$run_log" 2>&1
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      passed=1
      ET_RUN_OK=$((ET_RUN_OK + 1))
      ET_PASS=$((ET_PASS + 1))
    else
      failed=1
      ET_RUN_FAIL=$((ET_RUN_FAIL + 1))
      ET_FAIL=$((ET_FAIL + 1))
    fi
    ET_LAST_SUMMARY="own-main result: ${passed} passed; ${failed} failed; ${ignored} ignored"
    ET_TEST_PASS=$((ET_TEST_PASS + passed))
    ET_TEST_FAIL=$((ET_TEST_FAIL + failed))
    ET_TEST_IGNORE=$((ET_TEST_IGNORE + ignored))
    if [[ $failed -eq 0 ]]; then
      return 0
    fi
    return 1
  fi

  echo "  COMPILE+RUN  $suite/$name ($n tests, one binary each)"
  local first_ok=0
  local done=0
  local total_run=$n

  for ((i = 0; i < n; i++)); do
    meta="$(python3 -c 'import json,sys; t=json.load(open(sys.argv[1]))["tests"][int(sys.argv[2])]; import json as J; print(J.dumps(t))' "$manifest" "$i")"
    path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["path"])' "$meta")"
    should_panic="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1]).get("should_panic") else "0")' "$meta")"
    ignore="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1]).get("ignore") else "0")' "$meta")"
    done=$((i + 1))
    local prog="[$done/$total_run]"

    if [[ "$ignore" == "1" ]]; then
      ignored=$((ignored + 1))
      echo "    $prog ignored  $path"
      continue
    fi

    if ! python3 "$ET_OWN_MAIN_PY" set-main "$staged" "$i" >/dev/null; then
      echo "    $prog FAIL     set-main $path"
      failed=$((failed + 1))
      any_compile_fail=1
      continue
    fi

    bin="$suite_dir/${name}__$i"
    compile_log="$suite_dir/${name}__$i.compile.log"
    run_log="$suite_dir/${name}__$i.run.log"

    if ! RUSTFLAGS="${RUSTFLAGS:-} -C incremental=$incr_dir" \
        et_compile_with_fixups "$staged" "$bin" "$compile_log"; then
      echo "    $prog COMPILE-FAIL  $path"
      echo "      see $compile_log"
      failed=$((failed + 1))
      any_compile_fail=1
      continue
    fi
    first_ok=1

    set +e
    ET_TEST_SUITE="$suite" \
    ET_TEST_NAME="${name}__$i" \
    ET_TEST_SRC="$src" \
    ET_TEST_STAGED="$staged" \
    ET_TEST_NO_HARNESS=1 \
    ET_TEST_PATH="$path" \
    ET_TEST_TARGET="${ET_TARGET:-}" \
    ET_RUSTC="$ET_RUSTC" \
      "$ET_RUNNER" "$bin" >"$run_log" 2>&1
    rc=$?
    set -e

    if [[ $rc -eq "$skip_exit" ]]; then
      ignored=$((ignored + 1))
      echo "    $prog skipped  $path (cfg)"
      continue
    fi

    if [[ "$should_panic" == "1" ]]; then
      if [[ $rc -ne 0 ]]; then
        passed=$((passed + 1))
        echo "    $prog ok       $path (should_panic)"
      else
        echo "    $prog FAIL     $path (expected panic/abort)"
        failed=$((failed + 1))
      fi
    else
      if [[ $rc -eq 0 ]]; then
        passed=$((passed + 1))
        echo "    $prog ok       $path"
      else
        echo "    $prog FAIL     $path (exit $rc)"
        echo "      see $run_log"
        failed=$((failed + 1))
      fi
    fi
  done

  if [[ $first_ok -eq 1 && $any_compile_fail -eq 0 ]]; then
    ET_COMPILE_OK=$((ET_COMPILE_OK + 1))
  elif [[ $any_compile_fail -eq 1 || $first_ok -eq 0 ]]; then
    ET_COMPILE_FAIL=$((ET_COMPILE_FAIL + 1))
  fi

  ET_LAST_SUMMARY="own-main result: ${passed} passed; ${failed} failed; ${ignored} ignored"
  ET_TEST_PASS=$((ET_TEST_PASS + passed))
  ET_TEST_FAIL=$((ET_TEST_FAIL + failed))
  ET_TEST_IGNORE=$((ET_TEST_IGNORE + ignored))

  if [[ $failed -eq 0 ]]; then
    ET_RUN_OK=$((ET_RUN_OK + 1))
    ET_PASS=$((ET_PASS + 1))
    return 0
  fi
  ET_RUN_FAIL=$((ET_RUN_FAIL + 1))
  ET_FAIL=$((ET_FAIL + 1))
  return 1
}

# Build rustc argv and invoke COMPILE_SCRIPT; retry with feature fixups on failure.
# Args: staged_src out_bin compile_log [--test]
et_compile_with_fixups() {
  local staged="$1"
  local bin="$2"
  local log="$3"
  shift 3
  local mode_args=("$@")

  if [[ -z "${ET_COMPILER:-}" ]]; then
    et_die "ET_COMPILER is not set (internal error)"
  fi

  local max_attempts="${ET_COMPILE_MAX_ATTEMPTS:-20}"
  local attempt
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    local cmd=(
      --edition "${ET_EDITION:-2024}"
    )
    if [[ -n "${ET_TARGET:-}" ]]; then
      cmd+=(--target "$ET_TARGET")
      # Same gate as et_prepare_deps: restricted_std sysroots need this on every crate.
      cmd+=('-Zcrate-attr=feature(restricted_std)')
    fi
    if ((${#mode_args[@]})); then
      cmd+=("${mode_args[@]}")
    fi
    cmd+=("$staged" -o "$bin")
    if ((ET_DEP_READY)); then
      cmd+=("${ET_DEP_LFLAGS[@]}")
    fi

    set +e
    ET_RUSTC="$ET_RUSTC" "$ET_COMPILER" "$log" -- "${cmd[@]}"
    local rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      return 0
    fi

    if ! python3 "$ET_FIX_FEATURES" "$staged" "$log" >"${log}.fix" 2>&1; then
      return 1
    fi
    {
      echo "----- feature fixup attempt $attempt -----"
      cat "${log}.fix"
    } >>"$log"
  done
  return 1
}

# Parse "N passed; M failed; K ignored" from a libtest / own-main summary line.
# Sets _et_tp, _et_tf, _et_ti. Returns 0 if parsed.
et_extract_test_counts() {
  local line="$1"
  _et_tp=0
  _et_tf=0
  _et_ti=0
  if [[ "$line" =~ ([0-9]+)[[:space:]]+passed\;[[:space:]]+([0-9]+)[[:space:]]+failed\;[[:space:]]+([0-9]+)[[:space:]]+ignored ]]; then
    _et_tp="${BASH_REMATCH[1]}"
    _et_tf="${BASH_REMATCH[2]}"
    _et_ti="${BASH_REMATCH[3]}"
    return 0
  fi
  return 1
}

# Read harness summary from a run log; accumulate ET_TEST_*; set ET_LAST_SUMMARY.
et_accumulate_from_run_log() {
  local run_log="$1"
  local no_harness="$2"
  ET_LAST_SUMMARY=""
  local line=""
  if [[ "$no_harness" -eq 0 ]]; then
    line="$(grep -E '^test result:' "$run_log" 2>/dev/null | tail -n1 || true)"
  else
    line="$(grep -E '^own-main result:' "$run_log" 2>/dev/null | tail -n1 || true)"
  fi
  ET_LAST_SUMMARY="$line"
  if [[ -n "$line" ]] && et_extract_test_counts "$line"; then
    ET_TEST_PASS=$((ET_TEST_PASS + _et_tp))
    ET_TEST_FAIL=$((ET_TEST_FAIL + _et_tf))
    ET_TEST_IGNORE=$((ET_TEST_IGNORE + _et_ti))
  fi
}

# Compile and run one test crate root (path in the original library tree).
et_run_one() {
  local suite="$1"
  local src="$2"

  if [[ ! -f "$src" ]]; then
    echo "  SKIP  $suite: missing $src"
    ET_SKIP=$((ET_SKIP + 1))
    return 0
  fi

  local name
  name="$(et_bin_name "$src")"
  local suite_dir="$ET_OUT/$suite"
  mkdir -p "$suite_dir"
  local bin="$suite_dir/$name"
  local compile_log="$suite_dir/${name}.compile.log"
  local run_log="$suite_dir/${name}.run.log"

  local staged
  staged="$(et_stage_src "$src")"

  local mode_args=()
  local no_harness=0
  if et_is_no_harness "$src"; then
    no_harness=1
  elif [[ "${ET_OWN_MAIN:-0}" == "1" ]]; then
    no_harness=1
    if ! et_apply_own_main "$staged"; then
      echo "  COMPILE-FAIL  $suite/$name (own-main prepare)"
      ET_COMPILE_FAIL=$((ET_COMPILE_FAIL + 1))
      return 0
    fi
    # One binary per test; scripts drive compile/run/aggregation.
    set +e
    et_run_own_main_tests "$suite" "$name" "$src" "$staged" "$suite_dir"
    local own_rc=$?
    set -e
    if [[ $own_rc -eq 0 ]]; then
      echo "  PASS     $suite/$name — $ET_LAST_SUMMARY"
    else
      echo "  FAIL     $suite/$name"
      echo "    $ET_LAST_SUMMARY"
    fi
    return 0
  else
    mode_args=(--test)
  fi

  echo "  COMPILE  $suite/$name"
  if ! et_compile_with_fixups "$staged" "$bin" "$compile_log" "${mode_args[@]+"${mode_args[@]}"}"; then
    echo "  COMPILE-FAIL  $suite/$name"
    echo "    see $compile_log"
    ET_COMPILE_FAIL=$((ET_COMPILE_FAIL + 1))
    return 0
  fi
  ET_COMPILE_OK=$((ET_COMPILE_OK + 1))

  echo "  RUN      $suite/$name"
  set +e
  ET_TEST_SUITE="$suite" \
  ET_TEST_NAME="$name" \
  ET_TEST_SRC="$src" \
  ET_TEST_STAGED="$staged" \
  ET_TEST_NO_HARNESS="$no_harness" \
  ET_TEST_TARGET="${ET_TARGET:-}" \
  ET_RUSTC="$ET_RUSTC" \
    "$ET_RUNNER" "$bin" >"$run_log" 2>&1
  local rc=$?
  set -e

  et_accumulate_from_run_log "$run_log" "$no_harness"
  local summary="$ET_LAST_SUMMARY"
  # Non-harness / stub-main binaries often have no count line — show last line.
  if [[ -z "$summary" && "$no_harness" -eq 1 ]]; then
    summary="$(tail -n1 "$run_log" 2>/dev/null | tr -d '\r' || true)"
  fi

  if [[ $rc -eq 0 ]]; then
    if [[ -n "$summary" ]]; then
      echo "  PASS     $suite/$name — $summary"
    else
      echo "  PASS     $suite/$name"
    fi
    ET_RUN_OK=$((ET_RUN_OK + 1))
    ET_PASS=$((ET_PASS + 1))
  else
    echo "  FAIL     $suite/$name (exit $rc)"
    echo "    see $run_log"
    [[ -n "$ET_LAST_SUMMARY" ]] && echo "    $ET_LAST_SUMMARY"
    ET_RUN_FAIL=$((ET_RUN_FAIL + 1))
    ET_FAIL=$((ET_FAIL + 1))
  fi
}

et_read_tier_list() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$line" ]] && continue
    printf '%s\n' "$line"
  done <"$file"
}

et_print_summary() {
  local compile_total=$((ET_COMPILE_OK + ET_COMPILE_FAIL))
  local run_total=$((ET_RUN_OK + ET_RUN_FAIL))
  local test_exec=$((ET_TEST_PASS + ET_TEST_FAIL))

  echo
  echo "=== Summary ==="
  echo "  (files = crate roots from allowlists; tests = individual #[test] cases)"
  echo "  compiles-successful: ${ET_COMPILE_OK}/${compile_total}"
  echo "  runs-successful:     ${ET_RUN_OK}/${run_total}"
  echo "  tests-successful:    ${ET_TEST_PASS}/${test_exec}"
  echo "  tests-ignored:       ${ET_TEST_IGNORE}"
  echo "  files-skipped:       ${ET_SKIP}"
  if [[ $ET_RUN_FAIL -gt 0 || $ET_COMPILE_FAIL -gt 0 || $ET_TEST_FAIL -gt 0 ]]; then
    return 1
  fi
  return 0
}
