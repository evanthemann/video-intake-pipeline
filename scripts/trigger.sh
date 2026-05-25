#!/bin/bash
#
# trigger.sh — fire the "Delete clips blender copy" Keyboard Maestro macro,
# passing a numeric argument through as the macro parameter.
#
# Usage: ./trigger.sh <number>

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Error: missing argument." >&2
  echo "Usage: $0 <number>" >&2
  exit 1
fi

number="$1"

osascript -e "tell application \"Keyboard Maestro Engine\" to do script \"Delete clips blender copy\" with parameter \"${number}\""
