# blender-km-macros

A small [Keyboard Maestro](https://www.keyboardmaestro.com/) macro manager for a
Blender clip-deletion workflow. It bundles the exported macros plus a shell helper
so the "Delete clips Blender" macro can be triggered from anywhere (terminal,
scripts, other apps), running the clip-deletion cycle a number of times you pass
in.

## Repository layout

```
blender-km-macros/
├── README.md
├── macros/
│   ├── Delete clips Blender.kmmacros          # the real macro
│   └── Delete clips Blender (TEST).kmmacros   # dry-run version (no deletions)
└── scripts/
    └── trigger.sh                             # CLI wrapper around the osascript trigger
```

## Importing the macros

**Double-click each `.kmmacros` file** to import it into Keyboard Maestro (or drag
it onto the Keyboard Maestro Editor). Both import into a macro group named
**Blender group**.

> **Group availability:** the exported group is set to be available in **all
> applications** (not restricted to Blender). This is required — the macro brings
> Blender to the front itself, so it must be allowed to run even when Blender
> isn't the front app. After importing, confirm the Blender group shows
> "available in all applications."

## Using `trigger.sh`

`scripts/trigger.sh` triggers a macro and passes a number through as the macro's
parameter. That number is how many times the macro repeats the clip-deletion
cycle.

```bash
./scripts/trigger.sh 5           # run the REAL macro, 5 delete cycles
./scripts/trigger.sh --test 5    # run the TEST macro (no deletions), see below
./scripts/trigger.sh -t 5        # same as --test
```

- Requires a number argument (after the optional `-t`/`--test` flag).
- Exits with an error and a usage message if the number is missing.
- **If Blender isn't running, it does not trigger the macro** — it prints a
  message to stderr, posts a macOS notification, and exits `1`. (The KM gate is
  still a backstop for other trigger paths.)
- The script is executable; if needed, restore the bit with
  `chmod +x scripts/trigger.sh`.

## Manual triggering with osascript

The same triggers without the wrapper script — substitute your count for `5`:

```bash
osascript -e 'tell application "Keyboard Maestro Engine" to do script "Delete clips Blender" with parameter "5"'
osascript -e 'tell application "Keyboard Maestro Engine" to do script "Delete clips Blender (TEST)" with parameter "5"'
```

The macros read the passed value via the `%TriggerValue%` token.

## How the macro works

`Delete clips Blender` is a single, self-contained macro in the **Blender group**.
When triggered with parameter `N`, it runs this sequence:

1. **Gate — only if Blender is open.** If Blender is *not* running, it shows a
   message and cancels. Nothing else happens.
2. **Bring Blender to the front.** Activates Blender so the simulated keystrokes
   are guaranteed to land in Blender and not whatever app you triggered from.
3. **READY? prompt.** A prompt tells you to move the mouse over the Blender VSE
   timeline (required — those shortcuts only work when the mouse is over the
   timeline), then **press Return to start** or **Escape to cancel**.
4. **Capture the count.** A **Set Variable to Text** action stores the passed
   parameter (`%TriggerValue%`) into the variable `BlenderDeleteCount`.
5. **Repeat the delete cycle `N` times.** A **Repeat** action whose count is the
   calculation `BlenderDeleteCount` runs the fixed clip-deletion keystroke sequence
   (with 1-second pauses) `N` times. The count is driven entirely by the argument
   passed from `trigger.sh` / `osascript` — there is no hard-coded count.

   > The Repeat count must be the **bare variable name** `BlenderDeleteCount` (no
   > `%` signs) — that field is a calculation, not text. Putting `%TriggerValue%`
   > or `%BlenderDeleteCount%` there shows "invalid." The editor may briefly show
   > "invalid" until the variable has a value; that's just a preview and it works
   > at run time (the Set Variable action runs first).

## Testing it safely (the TEST macro)

`Delete clips Blender (TEST)` runs the same front-half flow — gate, bring-to-front,
READY? prompt — but **replaces the delete keystrokes with a confirmation dialog**
instead of actually deleting anything. Use it to confirm the flow works end to end
(including that the count is passed through correctly) before running the real
thing:

```bash
./scripts/trigger.sh --test 5
```

After you press Return (Start) at the READY? prompt, you'll get a **"✅ TEST
PASSED"** dialog that echoes the count it received (`5`), with an OK button. No
clips are touched.

## Notes

- These files are **exported from a working Keyboard Maestro setup** (with one
  action — the TEST macro's "✅ TEST PASSED" confirmation — appended afterward), so
  they should import cleanly. The TEST macro's gate cancels silently if Blender
  isn't open (its "Blender isn't open" message did not survive an earlier import);
  add a message there in the editor if you want one.
- A bare hotkey is intentionally *not* set: the count comes from the passed
  parameter, so the macros are triggered by name (via `trigger.sh`, `osascript`,
  or an *Execute a Macro* action), not a keypress.
- The **Blender group** is available in all applications (required so the macro can
  bring Blender to the front from anywhere).

After any edit in Keyboard Maestro, **re-export the macro over the matching file in
`macros/`** to keep this repo in sync — that round-trip is the source of truth, not
hand-edited XML.
