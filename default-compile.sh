#!/usr/bin/env bash
# Thin rustc invocation wrapper for external-tester.
#
# The harness (lib.sh) builds the full rustc command line (edition, target,
# --test, -o, --extern, etc.). This script only runs rustc and captures output.
#
# Usage:
#   COMPILE_SCRIPT <compile_log> -- <rustc-args...>
#
# Environment:
#   ET_RUSTC   rustc binary (required)
#
# Exit status is rustc's. Stdout/stderr go to <compile_log>.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <compile_log> -- <rustc-args...>" >&2
  exit 2
fi

log="$1"
shift
if [[ "${1:-}" == "--" ]]; then
  shift
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <compile_log> -- <rustc-args...>" >&2
  exit 2
fi

if [[ -z "${ET_RUSTC:-}" || ! -x "${ET_RUSTC}" ]]; then
  echo "error: ET_RUSTC must be set to an executable rustc" >&2
  exit 2
fi

set +e
RUSTC_BOOTSTRAP=1 "$ET_RUSTC" "$@" >"$log" 2>&1
exit $?
