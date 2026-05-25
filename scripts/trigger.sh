#!/bin/bash
#
# trigger.sh — fire a Blender clip-deletion Keyboard Maestro macro, passing a
# numeric argument through as the macro parameter (how many delete cycles to run).
#
# Usage:
#   ./trigger.sh <number>          # run the real macro:  "Delete clips Blender"
#   ./trigger.sh -t <number>       # run the test macro:  "Delete clips Blender (TEST)"
#   ./trigger.sh --test <number>   # same as -t

set -euo pipefail

macro="Delete clips Blender"

if [ "${1:-}" = "-t" ] || [ "${1:-}" = "--test" ]; then
  macro="Delete clips Blender (TEST)"
  shift
fi

if [ "$#" -lt 1 ]; then
  echo "Error: missing argument." >&2
  echo "Usage: $0 [-t|--test] <number>" >&2
  exit 1
fi

number="$1"

# Bail out with feedback if Blender isn't running — the macro needs it open, and
# the KM gate would otherwise just cancel silently.
if ! pgrep -x "Blender" >/dev/null 2>&1; then
  echo "Blender isn't open — not triggering \"${macro}\"." >&2
  osascript -e 'display notification "Blender isn’t open — macro not triggered." with title "Delete clips Blender"' >/dev/null 2>&1 || true
  exit 1
fi

osascript -e "tell application \"Keyboard Maestro Engine\" to do script \"${macro}\" with parameter \"${number}\""
