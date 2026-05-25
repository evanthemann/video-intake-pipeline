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

A macro reads a passed parameter via the `%TriggerValue%` token. **Note:** as
currently exported, the wrapper macro does *not* reference `%TriggerValue%` — see
the caveat under "Two-macro architecture" below.

## Two-macro architecture

This workflow is split into two cooperating macros, both in the **Blender group**
(scoped to run only when Blender is the front app):

- **`Delete clips blender`** (the single cycle macro) — does one unit of work: a
  fixed sequence of simulated keystrokes with 1-second pauses between them that
  performs one clip-deletion cycle in Blender. Bound to its own hotkey.
- **`Delete clips blender copy`** (the wrapper macro) — the entry point that
  `trigger.sh` / `osascript` calls. It's a **Repeat** action that runs `Execute
  Macro → Delete clips blender` with a 1-second pause between iterations. Also
  bound to its own hotkey.

Keeping the per-clip logic isolated in `Delete clips blender` means the deletion
behavior is defined in exactly one place, while the wrapper only handles how many
times to run it.

### Caveat: the wrapper currently ignores the passed parameter

In the exported macro, the wrapper's Repeat count is the hard-coded literal `9`,
and its Execute Macro action has **Use Parameter** turned off. So passing a number
via `trigger.sh 5` (or the `osascript ... with parameter` call) has **no effect** —
the wrapper always runs exactly 9 cycles.

To make the argument actually control the cycle count, edit the wrapper macro in
Keyboard Maestro: set the **Repeat** action's count field to `%TriggerValue%`
(instead of `9`). Then re-export the macro over
`macros/Delete clips blender copy.kmmacros` to keep this repo in sync.
