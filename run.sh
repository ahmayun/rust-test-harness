#!/usr/bin/env bash
# Run Rust library integration tests against a prebuilt rustc sysroot (no cargo
# on the target toolchain). Host cargo is used only to build rand deps.
#
# Usage:
#   ./run.sh /path/to/rustc [options]
#
# Options (defaults match the previous environment-variable behavior):
#   --tier N
#   --library-src DIR     (default: ../rust/library)
#   --edition EDITION     (default: 2024)
#   --out DIR             (default: ./out)
#   --cargo-bin PATH      (default: cargo on PATH or ~/.cargo/bin/cargo)
#   --target TRIPLE       (default: host)
#   --runner-script PATH  (default: ./default-runner.sh)
#   --compile-script PATH (default: ./default-compile.sh)
#
# Still honored from the environment when set (e.g. multi-flag linker setup):
#   RUSTFLAGS, CARGO_TARGET_<TRIPLE>_LINKER

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  cat <<'EOF'
Usage: ./run.sh /path/to/rustc [options]

Options:
  --tier N                 Run tiers 0..N inclusive (default: 4)
  --library-src DIR        rust library/ tree (default: ../rust/library)
  --edition EDITION        rustc edition (default: 2024)
  --out DIR                build output directory (default: ./out)
  --cargo-bin PATH         host cargo for rand deps (default: auto-detect)
  --target TRIPLE          cross-compile target (default: host)
  --runner-script PATH     execute each test binary
                           (default: ./default-runner.sh)
  --compile-script PATH    rustc wrapper: <log> -- <rustc-args...>
                           (default: ./default-compile.sh; harness builds flags)
  --own-main               generate a lightweight main that calls each #[test]
                           (no libtest / rustc --test); better for small RTOS
  -h, --help               show this help

Also from environment (not CLI):
  RUSTFLAGS, CARGO_TARGET_<TRIPLE>_LINKER
EOF
}

MAX_TIER=4
REQUESTED_TIER=""
ARG_LIBRARY_SRC=""
ARG_EDITION=""
ARG_OUT=""
ARG_CARGO=""
ARG_TARGET=""
ARG_RUNNER=""
ARG_COMPILE=""
ARG_OWN_MAIN=0

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
esac

ET_RUSTC="$(et_resolve_rustc "$1")"
shift

need_arg() {
  local flag="$1"
  [[ $# -ge 2 ]] || et_die "$flag requires an argument"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier)
      need_arg "$@"
      REQUESTED_TIER="$2"
      shift 2
      ;;
    --tier=*)
      REQUESTED_TIER="${1#--tier=}"
      shift
      ;;
    --library-src)
      need_arg "$@"
      ARG_LIBRARY_SRC="$2"
      shift 2
      ;;
    --library-src=*)
      ARG_LIBRARY_SRC="${1#--library-src=}"
      shift
      ;;
    --edition)
      need_arg "$@"
      ARG_EDITION="$2"
      shift 2
      ;;
    --edition=*)
      ARG_EDITION="${1#--edition=}"
      shift
      ;;
    --out)
      need_arg "$@"
      ARG_OUT="$2"
      shift 2
      ;;
    --out=*)
      ARG_OUT="${1#--out=}"
      shift
      ;;
    --cargo-bin)
      need_arg "$@"
      ARG_CARGO="$2"
      shift 2
      ;;
    --cargo-bin=*)
      ARG_CARGO="${1#--cargo-bin=}"
      shift
      ;;
    --target)
      need_arg "$@"
      ARG_TARGET="$2"
      shift 2
      ;;
    --target=*)
      ARG_TARGET="${1#--target=}"
      shift
      ;;
    --runner-script)
      need_arg "$@"
      ARG_RUNNER="$2"
      shift 2
      ;;
    --runner-script=*)
      ARG_RUNNER="${1#--runner-script=}"
      shift
      ;;
    --compile-script)
      need_arg "$@"
      ARG_COMPILE="$2"
      shift 2
      ;;
    --compile-script=*)
      ARG_COMPILE="${1#--compile-script=}"
      shift
      ;;
    --own-main)
      ARG_OWN_MAIN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      et_die "unknown argument: $1"
      ;;
  esac
done

if [[ -n "$REQUESTED_TIER" ]]; then
  [[ "$REQUESTED_TIER" =~ ^[0-9]+$ ]] || et_die "--tier must be an integer"
  if (( REQUESTED_TIER > MAX_TIER )); then
    et_die "--tier max is $MAX_TIER"
  fi
else
  REQUESTED_TIER=4
fi

# Apply CLI over previous env defaults (same defaults as before).
if [[ -n "$ARG_LIBRARY_SRC" ]]; then
  RUST_LIBRARY_SRC="$ARG_LIBRARY_SRC"
fi
ET_EDITION="${ARG_EDITION:-${ET_EDITION:-2024}}"
ET_OUT="${ARG_OUT:-${ET_OUT:-$ET_ROOT/out}}"
if [[ -n "$ARG_CARGO" ]]; then
  ET_CARGO="$ARG_CARGO"
fi
ET_TARGET="${ARG_TARGET:-${ET_TARGET:-}}"
ET_OWN_MAIN="$ARG_OWN_MAIN"

ET_LIBRARY="$(et_resolve_library_src)"
mkdir -p "$ET_OUT"

if [[ -n "$ARG_RUNNER" ]]; then
  ET_RUNNER="$ARG_RUNNER"
elif [[ -n "${RUNNER_SCRIPT:-}" ]]; then
  ET_RUNNER="$RUNNER_SCRIPT"
else
  ET_RUNNER="$ET_ROOT/default-runner.sh"
fi
if [[ ! -x "$ET_RUNNER" && -f "$ET_RUNNER" ]]; then
  chmod +x "$ET_RUNNER" || true
fi
if [[ ! -f "$ET_RUNNER" ]]; then
  et_die "runner script not found: $ET_RUNNER"
fi

if [[ -n "$ARG_COMPILE" ]]; then
  ET_COMPILER="$ARG_COMPILE"
elif [[ -n "${COMPILE_SCRIPT:-}" ]]; then
  ET_COMPILER="$COMPILE_SCRIPT"
else
  ET_COMPILER="$ET_ROOT/default-compile.sh"
fi
if [[ ! -x "$ET_COMPILER" && -f "$ET_COMPILER" ]]; then
  chmod +x "$ET_COMPILER" || true
fi
if [[ ! -f "$ET_COMPILER" ]]; then
  et_die "compile script not found: $ET_COMPILER"
fi

if command -v realpath >/dev/null 2>&1; then
  ET_RUNNER="$(realpath "$ET_RUNNER")"
  ET_COMPILER="$(realpath "$ET_COMPILER")"
fi

echo "rustc:    $ET_RUSTC"
echo "version:  $($ET_RUSTC --version 2>/dev/null || echo '?')"
echo "sysroot:  $($ET_RUSTC --print sysroot 2>/dev/null || echo '?')"
echo "library:  $ET_LIBRARY"
echo "out:      $ET_OUT"
echo "edition:  $ET_EDITION"
echo "compile:  $ET_COMPILER"
echo "runner:   $ET_RUNNER"
if [[ -n "${ET_TARGET:-}" ]]; then
  echo "target:   $ET_TARGET (cross)"
else
  echo "target:   host"
fi
if [[ "${ET_OWN_MAIN:-0}" == "1" ]]; then
  echo "harness:  own-main (no libtest)"
else
  echo "harness:  rustc --test (libtest)"
fi
echo "tiers:    0..$REQUESTED_TIER"
echo

et_prepare_deps
echo

ET_PASS=0
ET_FAIL=0
ET_COMPILE_FAIL=0
ET_SKIP=0

run_tier() {
  local tier="$1"
  local files
  # shellcheck disable=SC2206
  files=($ET_TIERS/${tier}-*.txt)
  if [[ ${#files[@]} -eq 0 || ! -f "${files[0]}" ]]; then
    echo "=== Tier $tier (no allowlist) ==="
    return 0
  fi
  local allowlist="${files[0]}"
  local label
  label="$(basename "$allowlist" .txt)"

  echo "=== Tier $tier ($label) ==="

  local rel
  local count=0
  while IFS= read -r rel || [[ -n "${rel:-}" ]]; do
    [[ -z "${rel:-}" ]] && continue
    count=$((count + 1))
    local src
    if [[ "$tier" -eq 0 ]]; then
      src="$ET_ROOT/$rel"
    else
      src="$ET_LIBRARY/$rel"
    fi
    et_run_one "tier${tier}" "$src"
  done < <(et_read_tier_list "$allowlist")

  if [[ $count -eq 0 ]]; then
    echo "  (empty allowlist — nothing to run)"
  fi
  echo
}

for ((t = 0; t <= REQUESTED_TIER; t++)); do
  run_tier "$t"
done

if et_print_summary; then
  exit 0
else
  exit 1
fi
