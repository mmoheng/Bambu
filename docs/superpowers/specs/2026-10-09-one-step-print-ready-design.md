# One-step "Make print-ready file" — design

Date: 2026-10-09. Status: design agreed in chat, awaiting review of this document.

## What this is for

Put a model in, and get back a file that is ready to print well, plus a
plain list of what was changed and why. The geometry decides the
changes: slopes and overhangs, balance on the bed, thin walls, bridges.

Today the pieces exist but do not join up. The desktop window only
suggests changes against numbers typed in by hand and cannot make a
print file. The connector can slice, but only when driven step by step
from a chat. Three accuracy problems showed up on 2026-10-08 with
`Squirrel_Girl_Box.stl` and are fixed as part of this work.

## What stays the same

- Nothing is ever sent to the printer. The result is a new `.3mf` that
  is opened in Bambu Studio and printed from there.
- Only new files are written. The model and saved presets are never
  changed, and an existing file is never replaced.
- Only settings listed in `profiles/bambu_settings.py` are changed.
- The existing window features (type-in table, Analyze, Export) keep
  working. They move under an "Advanced" section.

## Out of scope

Drag and drop onto the window, sending a job to the printer, turning a
model to a better orientation automatically, printers other than the A1
with the 0.4 mm nozzle, and plates with several different models.

## How it is used

1. Pick a model (`.stl` or `.3mf`).
2. Pick a goal: best looks, strength, fast, balanced, or exact fit.
3. Pick the filament, from the Bambu Studio presets that fit the A1.
4. Click **Make print-ready file**.
5. The window shows: where the file was written, print time, filament
   used, whether supports are on, the plate type, every change with its
   reason, and any warnings.
6. To reject a change, untick it and click the button again. A new file
   is written without that change.
7. **Open in Bambu Studio** opens the file. Printing is done there.

## Design

### One operation, two front doors

A new service operation, `prepare_print`, does the whole job. The window
and the connector both call it, so they cannot drift apart.

```
window (gui.py)  ──┐
                   ├──> service.prepare_print ──> analyzer ──> optimizer
connector tool   ──┘                         └──> studio_runner (slice) ──> check result
(print_ready)
```

Inputs: model path, goal, optional filament preset name, optional list
of setting keys to leave out, optional output path. It runs as a
background task like `slice_model`, because slicing can outlast one
call.

Result: output path, base profile, filament, plate type, the changes
(key, label, from, to, reason, applied or left out), notes, warnings,
and per plate the print time, filament weight and whether support was
used.

### Step 1: start from Bambu's own profile for the goal

For a bare model, the base process profile is chosen by goal:

| Goal | Base profile |
|---|---|
| Best looks | 0.12mm High Quality @BBL A1 |
| Balanced | 0.20mm Standard @BBL A1 |
| Strength | 0.20mm Strength @BBL A1 |
| Fast | 0.24mm Draft @BBL A1 |
| Exact fit | 0.16mm Optimal @BBL A1 |

These profiles are tuned by Bambu and also set things this project does
not track, such as acceleration. That is why the profile is picked
instead of overriding the Standard profile's layer height and speed.

The profile's real values are read from the Bambu Studio install with
`PresetLibrary.load_full`, so the "from" value of every change is true
and nothing is typed in. If a profile in the table is not installed, the
configured default process is used and every rule applies as it does
today.

A Bambu Studio project file (`.3mf` with its own settings) keeps its own
profile. Its settings are the starting point, as `recommend` does now.

### Step 2: add only what the geometry calls for

When the base profile was chosen for the goal, the goal baselines are
already in it. So in this mode the optimizer:

- leaves layer height, outer wall speed, top and bottom shell layers,
  infill density and infill pattern to the profile;
- only ever raises wall loops, never lowers them (the Strength profile
  already has more than the rule's baseline);
- applies every geometry rule as now: supports, brim, wall loops for
  thin walls, fit compensation.

This is a new optional argument to `optimize`. Without it the optimizer
behaves exactly as before, so `recommend`, the CLI and the Advanced
section are unchanged.

### Step 3: slice and check

The model is sliced with the base profile, the chosen filament, and the
changes. The result is then read back and checked:

- every requested setting is present in the output (existing check);
- the plate type in the output is the configured one;
- the sliced height matches the model's height (existing orientation
  check).

A failed check is a warning in the result, in plain words, saying what
to fix in Bambu Studio. It never passes silently.

### Accuracy fix 1: shallow ledges are not ceilings

The analyzer groups overhanging faces into connected regions. For each
region it will now also estimate the narrow width: how far the region
sticks out from what holds it up. A region narrower than two line
widths (0.84 mm on the 0.4 mm nozzle) is a ledge. Ledges are reported
separately and no longer count toward the overhang or ceiling figures
that decide supports.

Why: the squirrel box has decorative grooves 0.4 mm deep along three
sides. Each was measured as a flat ceiling up to "84 mm across".

### Accuracy fix 2: narrow bridges

A flat ceiling region is a **narrow bridge** when it is at most 10 mm
wide and is held up at both ends, with open air along its long sides.
The top edge of a window is the typical case.

- A narrow bridge up to 60 mm long is printed without supports, with
  thick bridges switched on. The result carries a note naming the
  bridge and its length.
- A longer one gets supports, as now.
- Everything else is judged by the existing rules (a region of
  100 mm² or more, or a ceiling spanning over 20 mm, needs support).

**The 60 mm figure is not confirmed on this printer.** The existing
limits were calibrated on real deck-box prints, where the longest
ceiling that printed cleanly without support was about 12 mm. 60 mm is
the squirrel box's window top, chosen by judgment on 2026-10-08. It is a
named constant, marked unconfirmed in the code and in the note the user
sees, until that print's window edge has been looked at.

Why bridge at all: supporting a narrow window top means a tall, thin
support tower standing in the opening, which can topple late in a long
print.

### Accuracy fix 3: the plate type is applied

When slicing from presets, the configured plate type (Textured PEI) is
always passed to Bambu Studio. On 2026-10-08 it was not, and both files
came out as Cool Plate. The cause: the slice check remembers the first
way of calling Bambu Studio that works, and the plain call is tried
before the one that names the plate.

If Bambu Studio rejects the call with the plate named, the slice is
retried once without it. Either way the output's plate type is read
back, and a mismatch is a warning telling the user to set the plate in
Bambu Studio and slice again.

### Smaller fix: a remembered file that has moved

On opening, if the remembered model file no longer exists, the window
says so and asks for the file again, instead of showing a path that
fails later.

### The window

```
Model file: [..................................] [Browse...]
Recent:     [..................................v]
Goal: [Best looks v]   Filament: [Bambu PLA Basic @BBL A1 v]

[ Make print-ready file ]   [ Open in Bambu Studio ]   [ Show in folder ]

Print time 9h 27m · 146 g · supports off · Textured PEI Plate
Warnings and notes appear here.

Changes made                         (untick and click again to redo)
[x] Wall loops        2 -> 3      reason...
[x] Thick bridges     off -> on   reason...

> Advanced (type-in settings, Analyze, Export)
```

The filament list holds the filament presets whose own compatibility
list names the configured printer. The material used by the rules comes
from the chosen preset.

Slicing runs in the background. The window stays responsive, shows that
it is working, and polls the task until it is done.

`gui_logic.py` gains the display helpers (rows and summary line from a
`prepare_print` result) so they can be tested without a display.
`gui.py` only lays out widgets and calls them.

### The connector

One new tool, `print_ready`, with the same inputs. Its description says
that it writes a new file and sends nothing to the printer. The existing
tools are unchanged.

The existing tools say a recommendation must be shown and approved
before it is applied. `print_ready` applies the changes in one step on
purpose: that is the request. The safeguards are that it only writes a
new file, lists every change with its reason, and the user still
previews and prints from Bambu Studio.

## Errors

Every failure reaches the user as one plain sentence saying what to do:
file missing, not an STL or 3MF, Bambu Studio not found, filament preset
not found, slicing failed (with Bambu Studio's own message), output name
already taken. The window shows it in place; the connector returns it as
the tool's error text. This follows the existing `ServiceError` pattern.

## Testing

Automated, in the existing `unittest` style with the stand-in for Bambu
Studio:

- analyzer: a groove ring is a ledge; a strip held at both ends is a
  narrow bridge with the right length and width; a shelf held on one
  side is not; a closed pocket roof is not;
- rules: narrow bridge at or under the limit gives no support and thick
  bridges on; over the limit gives support; ledges alone give nothing;
- optimizer: in profile mode the goal settings are left alone and wall
  loops are never lowered; without the argument, results are unchanged;
- service: the profile follows the goal; real profile values are the
  "from" values; the plate type is passed; the retry without it works
  and warns; a wrong plate in the output warns; left-out keys are not
  applied; a project file keeps its own profile;
- connector: the new tool is listed, dispatchable, and takes no printer
  credentials;
- window helpers: rows and summary from a result; a missing remembered
  file is reported.

On the real PC, after the connector is restarted:

- `print_ready` on `Squirrel_Girl_Box.stl` with "best looks": expect
  0.12mm High Quality, 3 wall loops, thick bridges on, supports off,
  Textured PEI Plate;
- `print_ready` on one deck-box model that is known to need support;
- the window opened and run once on Windows. Tkinter is not available
  in the build sandbox, so this is the only real test of the layout.

## Not yet confirmed

- That Bambu Studio's command line accepts the plate type on this PC.
- The 60 mm narrow-bridge limit (see above).
- The new window layout on Windows.
