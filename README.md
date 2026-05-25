# blender-km-macros

A small [Keyboard Maestro](https://www.keyboardmaestro.com/) macro manager for a
Blender clip-deletion workflow. It bundles the exported macros plus a shell
helper so the "Delete clips blender copy" macro can be triggered from anywhere
(terminal, scripts, other apps) with a numeric argument.

## Repository layout

```
blender-km-macros/
├── README.md
├── macros/            # exported .kmmacros files (add manually)
└── scripts/
    └── trigger.sh     # CLI wrapper around the osascript trigger
```

## Importing macros

The exported `.kmmacros` files live in `macros/`. To install them into
Keyboard Maestro, **double-click the `.kmmacros` file** — Keyboard Maestro will
open and import the macro into your macro library. (Alternatively, drag the file
onto the Keyboard Maestro Editor.)

## Using `trigger.sh`

`scripts/trigger.sh` triggers the **"Delete clips blender copy"** macro and
passes a number through as the macro's parameter.

```bash
./scripts/trigger.sh 5
```

- Requires exactly one argument: the number to pass to the macro.
- Exits with an error and a usage message if no argument is provided.
- The script is executable; if needed, restore the bit with
  `chmod +x scripts/trigger.sh`.

## Manual triggering with osascript

The same trigger without the wrapper script — substitute your number for `5`:

```bash
osascript -e 'tell application "Keyboard Maestro Engine" to do script "Delete clips blender copy" with parameter "5"'
```

The macro reads the passed value via the `%TriggerValue%` token.

## Two-macro architecture

This workflow is split into two cooperating macros:

- **Single cycle macro** — does one unit of work: it deletes a single clip (one
  pass of the delete cycle in Blender). It is intentionally small and
  parameter-driven so it can be invoked repeatedly.
- **Wrapper macro** ("Delete clips blender copy") — the entry point that
  `trigger.sh` / `osascript` calls. It takes the numeric parameter and drives the
  single cycle macro that many times (e.g. loop N times, calling the single
  cycle macro on each iteration).

Keeping the per-clip logic isolated in the single cycle macro means the deletion
behavior is defined in exactly one place, while the wrapper only handles how many
times to run it. This makes the cycle easy to test on its own and lets the
wrapper stay a thin, count-driven loop.
