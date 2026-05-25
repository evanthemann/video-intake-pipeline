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

osascript -e "tell application \"Keyboard Maestro Engine\" to do script \"${macro}\" with parameter \"${number}\""
