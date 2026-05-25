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
4. **Repeat the delete cycle `N` times.** A **Repeat** action whose count is the
   expression `%TriggerValue%` runs the fixed clip-deletion keystroke sequence
   (with 1-second pauses) `N` times. The count is driven entirely by the argument
   passed from `trigger.sh` / `osascript` — there is no hard-coded count.

## Testing it safely (the TEST macro)

`Delete clips Blender (TEST)` runs the exact same flow — gate, bring-to-front,
READY? prompt — but **replaces the delete keystrokes with a big confirmation
message** instead of actually deleting anything. Use it to confirm the flow works
end to end (including that the count is passed through correctly) before running
the real thing:

```bash
./scripts/trigger.sh --test 5
```

If it works, after you press Return you'll see a large **"✅ TEST PASSED"** window
that echoes the count it received (`5`). No clips are touched.

## Notes / things to verify on import

These macros were hand-authored as `.kmmacros` XML and validated as property
lists, but **were not run in a live Keyboard Maestro engine**. After importing,
glance over the macros in the editor and confirm:

- The **Blender group** is "available in all applications."
- The **If** action's condition reads "Blender is not running" (Application
  condition). If it looks blank, re-pick Blender.
- The **Activate** action targets Blender, and the **Prompt for User Input** has a
  default "Start" button (Return) and cancels on Escape.
- A bare hotkey is intentionally *not* set: the count comes from the passed
  parameter, so the macros are triggered by name (via `trigger.sh`, `osascript`,
  or an *Execute a Macro* action), not a keypress.

After any edit in Keyboard Maestro, re-export the macro over the matching file in
`macros/` to keep this repo in sync.
