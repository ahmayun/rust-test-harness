#!/usr/bin/env bash
# Shared helpers for the external-tester harness.

set -euo pipefail

ET_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ET_OUT="${ET_OUT:-$ET_ROOT/out}"
ET_TIERS="$ET_ROOT/tiers"
ET_SCRIPTS="$ET_ROOT/scripts"
ET_FIX_FEATURES="$ET_SCRIPTS/fix_features.py"

# Set by et_prepare_deps
ET_DEP_LFLAGS=()
ET_DEP_READY=0

# Counters (global; run.sh resets them)
ET_PASS=0
ET_FAIL=0
ET_COMPILE_FAIL=0
ET_SKIP=0

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
  echo 'pub use rand; pub use rand_xorshift;' >"$manifest_dir/lib.rs"

  echo "Preparing deps (rand, rand_xorshift) with host cargo + given rustc..."
  local cargo_args=(build --release)
  if [[ -n "$rand_src" && -n "$xor_src" ]]; then
    cargo_args+=(--offline)
  fi

  if ! (
    cd "$manifest_dir"
    CARGO_TARGET_DIR="$target_dir" \
      RUSTC="$ET_RUSTC" \
      RUSTC_BOOTSTRAP=1 \
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
      if ! (
        cd "$manifest_dir"
        CARGO_TARGET_DIR="$target_dir" \
          RUSTC="$ET_RUSTC" \
          RUSTC_BOOTSTRAP=1 \
          "$cargo" build --release
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

  local deps_lib="$target_dir/release/deps"
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
  echo "  deps ready: rand + rand_xorshift"
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

# Compile with iterative feature fixups on the staged source.
# Args: staged_src out_bin compile_log [--test]
et_compile_with_fixups() {
  local staged="$1"
  local bin="$2"
  local log="$3"
  shift 3
  local mode_args=("$@")

  local max_attempts=20
  local attempt
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    local cmd=(
      "$ET_RUSTC"
      --edition "${ET_EDITION:-2024}"
    )
    if ((${#mode_args[@]})); then
      cmd+=("${mode_args[@]}")
    fi
    cmd+=("$staged" -o "$bin")
    if ((ET_DEP_READY)); then
      cmd+=("${ET_DEP_LFLAGS[@]}")
    fi

    set +e
    RUSTC_BOOTSTRAP=1 "${cmd[@]}" >"$log" 2>&1
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

  echo "  RUN      $suite/$name"
  set +e
  ET_TEST_SUITE="$suite" \
  ET_TEST_NAME="$name" \
  ET_TEST_SRC="$src" \
  ET_TEST_STAGED="$staged" \
  ET_TEST_NO_HARNESS="$no_harness" \
  ET_RUSTC="$ET_RUSTC" \
    "$ET_RUNNER" "$bin" >"$run_log" 2>&1
  local rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    local summary=""
    if [[ $no_harness -eq 0 ]]; then
      summary="$(grep -E '^test result:' "$run_log" | tail -n1 || true)"
    else
      summary="$(tail -n1 "$run_log" | tr -d '\r' || true)"
    fi
    if [[ -n "$summary" ]]; then
      echo "  PASS     $suite/$name — $summary"
    else
      echo "  PASS     $suite/$name"
    fi
    ET_PASS=$((ET_PASS + 1))
  else
    echo "  FAIL     $suite/$name (exit $rc)"
    echo "    see $run_log"
    # Still show libtest summary on failure when present
    local summary
    summary="$(grep -E '^test result:' "$run_log" | tail -n1 || true)"
    [[ -n "$summary" ]] && echo "    $summary"
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
  echo
  echo "=== Summary ==="
  echo "  pass:         $ET_PASS"
  echo "  fail:         $ET_FAIL"
  echo "  compile-fail: $ET_COMPILE_FAIL"
  echo "  skip:         $ET_SKIP"
  local attempted=$((ET_PASS + ET_FAIL + ET_COMPILE_FAIL))
  echo "  attempted:    $attempted"
  if [[ $ET_FAIL -gt 0 || $ET_COMPILE_FAIL -gt 0 ]]; then
    return 1
  fi
  return 0
}
