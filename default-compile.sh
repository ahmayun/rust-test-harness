#!/usr/bin/env bash
# Default compiler for external-tester: rustc --test (or plain rustc) with
# optional feature-gate fixup retries. Writes the binary to the given path.
#
# Invoked by run.sh / lib.sh as:
#   COMPILE_SCRIPT <staged_src> <out_bin> <compile_log> [--test]
#
# Environment set by the harness:
#   ET_RUSTC              rustc to invoke (required)
#   ET_EDITION            edition (default: 2024)
#   ET_TARGET             cross triple (empty = host)
#   ET_DEP_READY          1 if rand rlibs are available
#   ET_DEP_RAND_RLIB      path to librand-*.rlib
#   ET_DEP_RAND_XORSHIFT_RLIB  path to librand_xorshift-*.rlib
#   ET_DEP_LIBDIR         -L dependency= directory
#   ET_FIX_FEATURES       path to fix_features.py (optional; default beside scripts/)
#   ET_COMPILE_MAX_ATTEMPTS  feature-fixup retries (default: 20)
#   RUSTFLAGS             honored by rustc automatically
#
# Exit 0 on success (binary produced), non-zero on failure.
# Compiler stdout/stderr is written to <compile_log>.

set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <staged_src> <out_bin> <compile_log> [--test]" >&2
  exit 2
fi

staged="$1"
bin="$2"
log="$3"
shift 3

mode_args=()
while [[ $# -gt 0 ]]; do
  mode_args+=("$1")
  shift
done

if [[ -z "${ET_RUSTC:-}" || ! -x "${ET_RUSTC}" ]]; then
  echo "error: ET_RUSTC must be set to an executable rustc" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX_FEATURES="${ET_FIX_FEATURES:-$SCRIPT_DIR/scripts/fix_features.py}"
MAX_ATTEMPTS="${ET_COMPILE_MAX_ATTEMPTS:-20}"
EDITION="${ET_EDITION:-2024}"

build_cmd() {
  local cmd=(
    "$ET_RUSTC"
    --edition "$EDITION"
  )
  if [[ -n "${ET_TARGET:-}" ]]; then
    cmd+=(--target "$ET_TARGET")
  fi
  if ((${#mode_args[@]})); then
    cmd+=("${mode_args[@]}")
  fi
  cmd+=("$staged" -o "$bin")
  if [[ "${ET_DEP_READY:-0}" == "1" ]]; then
    if [[ -n "${ET_DEP_RAND_RLIB:-}" && -n "${ET_DEP_RAND_XORSHIFT_RLIB:-}" && -n "${ET_DEP_LIBDIR:-}" ]]; then
      cmd+=(
        --extern "rand=$ET_DEP_RAND_RLIB"
        --extern "rand_xorshift=$ET_DEP_RAND_XORSHIFT_RLIB"
        -L "dependency=$ET_DEP_LIBDIR"
      )
    fi
  fi
  printf '%s\0' "${cmd[@]}"
}

attempt=1
while (( attempt <= MAX_ATTEMPTS )); do
  mapfile -d '' -t cmd < <(build_cmd)

  set +e
  RUSTC_BOOTSTRAP=1 "${cmd[@]}" >"$log" 2>&1
  rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    exit 0
  fi

  if [[ ! -f "$FIX_FEATURES" ]]; then
    exit 1
  fi

  if ! python3 "$FIX_FEATURES" "$staged" "$log" >"${log}.fix" 2>&1; then
    exit 1
  fi
  {
    echo "----- feature fixup attempt $attempt -----"
    cat "${log}.fix"
  } >>"$log"

  attempt=$((attempt + 1))
done

exit 1
