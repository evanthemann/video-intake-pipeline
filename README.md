# blender-km-macros

A small [Keyboard Maestro](https://www.keyboardmaestro.com/) macro manager for a
Blender clip-deletion workflow. It bundles the exported macro plus a shell helper
so the "Delete clips blender copy" macro can be triggered from anywhere (terminal,
scripts, other apps), running the clip-deletion cycle a number of times you pass
in.

## Repository layout

```
blender-km-macros/
├── README.md
├── macros/
│   └── Delete clips blender copy.kmmacros   # the self-contained macro
└── scripts/
    └── trigger.sh                           # CLI wrapper around the osascript trigger
```

## Importing the macro

To install the macro into Keyboard Maestro, **double-click
`macros/Delete clips blender copy.kmmacros`** — Keyboard Maestro will open and
import it into your macro library. (Alternatively, drag the file onto the Keyboard
Maestro Editor.) It imports into a **Blender group** that is active only while
Blender is the front application.

## Using `trigger.sh`

`scripts/trigger.sh` triggers the **"Delete clips blender copy"** macro and passes
a number through as the macro's parameter. That number is how many times the macro
repeats the clip-deletion cycle.

```bash
./scripts/trigger.sh 5    # run the deletion cycle 5 times
```

- Requires exactly one argument: the number of cycles to run.
- Exits with an error and a usage message if no argument is provided.
- The script is executable; if needed, restore the bit with
  `chmod +x scripts/trigger.sh`.
- Blender must be the front application, or the macro's group is inactive and
  nothing happens.

## Manual triggering with osascript

The same trigger without the wrapper script — substitute your count for `5`:

```bash
osascript -e 'tell application "Keyboard Maestro Engine" to do script "Delete clips blender copy" with parameter "5"'
```

The macro reads the passed value via the `%TriggerValue%` token (see below).

## How the macro works

This is a single, self-contained macro in the **Blender group** (scoped to run
only when Blender is the front app):

- **`Delete clips blender copy`** — a single **Repeat** action whose count is the
  expression `%TriggerValue%`. Inside the loop is the full clip-deletion cycle: a
  fixed sequence of simulated keystrokes with 1-second pauses between them. Running
  it with parameter `N` performs the cycle `N` times.

Because the loop count is `%TriggerValue%`, the cycle count is driven entirely by
the argument passed from `trigger.sh` / `osascript`. There is no hard-coded count
and no separate "single cycle" macro — the loop wraps all the actions in one file.

### Notes

- **No hotkey trigger.** Since the count comes from the passed parameter, a bare
  hotkey press would supply no value and the loop would run zero times. The macro
  is therefore triggered by name (via `trigger.sh`, `osascript`, or an *Execute a
  Macro* action) rather than a hotkey. To add manual use, add a trigger that also
  sets a count — e.g. a *Prompt for User Input* feeding the Repeat count, or a
  hotkey plus a default.
- **Editing the count source.** The Repeat action's count field is the calculation
  `%TriggerValue%`. After editing the macro in Keyboard Maestro, re-export it over
  `macros/Delete clips blender copy.kmmacros` to keep this repo in sync.
