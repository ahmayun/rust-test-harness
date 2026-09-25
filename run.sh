#!/usr/bin/env bash
# Run Rust library integration tests against a prebuilt rustc sysroot (no cargo
# on the target toolchain). Host cargo is used only to build rand deps.
#
# Usage:
#   ./run.sh /path/to/rustc [--tier N]
#
# Environment:
#   RUST_LIBRARY_SRC  Path to rust library/ tree (default: ../rust/library)
#   ET_EDITION        Rust edition for rustc (default: 2024)
#   ET_OUT            Build output directory (default: ./out)
#   ET_CARGO          Host cargo binary (default: cargo or ~/.cargo/bin/cargo)
#   RUNNER_SCRIPT     Script used to execute each compiled test binary
#                     (default: <external-tester>/default-runner.sh)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  cat <<'EOF'
Usage: ./run.sh /path/to/rustc [--tier N]

  --tier N   Run tiers 0..N inclusive (default: 4).

Environment:
  RUST_LIBRARY_SRC   rust library/ directory (default: ../rust/library)
  ET_EDITION         edition passed to rustc (default: 2024)
  ET_OUT             output directory (default: ./out)
  ET_CARGO           host cargo used only to build rand deps
  RUNNER_SCRIPT      how to execute each test binary
                     (default: ./default-runner.sh next to run.sh)
EOF
}
MAX_TIER=4
REQUESTED_TIER=""

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier)
      [[ $# -ge 2 ]] || et_die "--tier requires a number"
      REQUESTED_TIER="$2"
      shift 2
      ;;
    --tier=*)
      REQUESTED_TIER="${1#--tier=}"
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

ET_LIBRARY="$(et_resolve_library_src)"
mkdir -p "$ET_OUT"

# Resolve runner: RUNNER_SCRIPT or default-runner.sh beside run.sh.
if [[ -n "${RUNNER_SCRIPT:-}" ]]; then
  ET_RUNNER="$RUNNER_SCRIPT"
else
  ET_RUNNER="$ET_ROOT/default-runner.sh"
fi
if [[ ! -x "$ET_RUNNER" && -f "$ET_RUNNER" ]]; then
  chmod +x "$ET_RUNNER" || true
fi
if [[ ! -f "$ET_RUNNER" ]]; then
  et_die "runner script not found: $ET_RUNNER (set RUNNER_SCRIPT)"
fi
# Prefer absolute path for child processes.
if command -v realpath >/dev/null 2>&1; then
  ET_RUNNER="$(realpath "$ET_RUNNER")"
fi

echo "rustc:    $ET_RUSTC"
echo "version:  $($ET_RUSTC --version 2>/dev/null || echo '?')"
echo "sysroot:  $($ET_RUSTC --print sysroot 2>/dev/null || echo '?')"
echo "library:  $ET_LIBRARY"
echo "out:      $ET_OUT"
echo "runner:   $ET_RUNNER"
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
