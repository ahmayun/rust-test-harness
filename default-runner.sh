#!/usr/bin/env bash
# Default test runner for external-tester: execute the compiled test binary
# on the host.
#
# Invoked by run.sh / lib.sh as:
#   RUNNER_SCRIPT <binary>
#
# Environment set by the harness (optional for custom runners):
#   ET_TEST_SUITE      tier label (e.g. tier3)
#   ET_TEST_NAME       binary/test name (e.g. sync_lib)
#   ET_TEST_SRC        original source path in the library tree
#   ET_TEST_STAGED     staged (possibly feature-fixed) source used to compile
#   ET_TEST_NO_HARNESS 1 if compiled without --test (plain binary)
#   ET_TEST_TARGET     target triple when cross-compiling (empty = host)
#   ET_RUSTC           rustc used to compile the binary
#
# Extra CLI args after the binary are forwarded (unused by default).
# Exit status of the binary is preserved.
#
# For cross builds, replace this script (RUNNER_SCRIPT) with one that runs the
# binary on the target (QEMU, device, etc.). Do not exec a foreign binary here.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <binary> [args...]" >&2
  exit 2
fi

bin="$1"
shift

exec "$bin" "$@"
