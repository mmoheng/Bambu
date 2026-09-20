# Bambu Companion Project Memory Bank

Saved: 2026-09-20
Last reviewed: 2026-09-20 (see Review Notes at bottom — four gaps found and fixed)

## Project Goal

Build a ChatGPT-connected Bambu Companion for a Bambu A1 + AMS 2 Pro that can:
- Read printer and AMS status.
- Analyze STL and 3MF files.
- Inspect model geometry before slicing.
- Ask the user what the goal of each print is.
- Recommend exact Bambu Studio settings for that specific model.
- Explain every recommended change.
- Wait for approval before applying changes.
- Apply settings only to a temporary per-job profile.
- Slice/export an optimized 3MF using Bambu Studio.
- Keep normal Bambu Studio presets untouched.

The long-term goal is to remove guesswork around settings such as wall loops,
infill, layer height, supports, cooling, brim, seam placement, dimensional
compensation, and related slicer settings.

## User Hardware / Initial Scope

Printer:
- Bambu Lab A1

Material system:
- AMS 2 Pro
- AMS 2 Pro is supported on the A1, but only via a separate **AMS Hub**
  accessory (sold separately) plus a printed bracket and PTFE tubing — the
  A1's stock extruder cannot drive AMS 2 Pro's assisted-feeding motors on
  its own. Requires A1 firmware in the v1.05.0.0+ range and the AMS 2
  Pro/AMS HT 6-pin cable (an original-AMS-generation cable can cause
  communication issues). Confirm hub + firmware are in place before
  assuming AMS status queries will work.

Nozzle:
- Primary workflow assumes a 0.4 mm nozzle unless the job provides another nozzle.

Initial materials:
- PLA
- PETG

Operating environment:
- Windows PC on the same local network as the printer.

## Approved Product Direction

V1 is safe-first.

V1 includes:
- Printer and AMS read-only information.
- Model analysis.
- Slicing and job preparation.
- Per-print optimization.
- Review and approval before any settings are applied.

V1 does not include:
- Automatically starting prints.
- Arbitrary G-code execution.
- Firmware controls.
- Machine safety-limit changes.
- Automatic profile overwriting.
- Unattended printer control.

## Per-Print Optimization Workflow

For every print, Bambu Companion asks the user to choose the goal:
1. Visual quality
2. Strength
3. Fast print
4. Balanced
5. Dimensional accuracy / fit

The optimizer evaluates the model and print context instead of using one universal profile.

## Approval Workflow

Recommendations are shown before they are applied.

Example:

Current -> Recommended
Wall loops: 2 -> 4
Infill: 15% Grid -> 18% Gyroid
Layer height: 0.20 mm -> 0.16 mm
Outer wall speed: 200 mm/s -> 120 mm/s
Brim: Off -> 5 mm

Every change gets a reason tied to:
- model geometry
- material
- printer/nozzle
- selected print goal
- current slicer settings

The user approves or rejects the recommendation before it is applied.

## Core Architecture

ChatGPT
    |
    v
Bambu Plugin/App (cloud-hosted; ChatGPT cannot call a local-network
    |               address directly — every tool call needs a public
    |               HTTPS endpoint)
    v
Cloud Relay / Tunnel (required component, not optional)
    |   - The Windows bridge opens an outbound connection to the relay
    |     (e.g. a tunnel client) so no inbound port is exposed on the
    |     home network.
    |   - Alternative: a lightweight cloud relay service the bridge
    |     polls or maintains a persistent connection to.
    |   - Whichever is chosen, the public-facing endpoint needs its own
    |     authentication — it is now internet-reachable even though the
    |     printer itself never is.
    v
Bambu Bridge running on the Windows PC
    |
    +--> Model Analyzer
    |      - reads STL / 3MF
    |      - dimensions / bounding box
    |      - thin walls
    |      - overhangs
    |      - bridges
    |      - bed-contact area
    |      - small details
    |      - fit-sensitive geometry
    |      - orientation candidates
    |
    +--> Print Optimizer
    |      - compares current profile to model needs
    |      - uses chosen print goal
    |      - proposes exact setting changes
    |      - explains every change
    |
    +--> Bambu Studio Runner
    |      - works from a temporary job profile
    |      - applies approved settings
    |      - orients / arranges where appropriate
    |      - slices
    |      - exports optimized 3MF
    |
    +--> Bambu A1 + AMS 2 Pro (via AMS Hub)
           - read-only printer status in V1
           - AMS status
           - temperatures
           - current job / progress
           - errors and warnings

## ChatGPT Connectivity

ChatGPT's Apps/Connector layer rejects local addresses outright (bare
`localhost`/LAN URLs are blocked as "unsafe"), so the Windows bridge cannot
simply listen on the LAN and expect ChatGPT to reach it.

Two workable patterns:
1. A tunnel client running on the Windows PC that opens an outbound
   connection out to a relay, so the bridge stays un-exposed to inbound
   traffic. This is the pattern OpenAI documents for local MCP servers.
2. A small always-on cloud relay/queue that the bridge polls or maintains
   a persistent connection to.

Either way this is a real component to design and secure, not just a
network detail — the printer's local API becomes reachable from the
bridge's tunnel/relay identity, so that identity needs its own auth
(the goal stated below, that printer credentials never leave the local
machine, still holds; it's the relay endpoint that needs its own
independent authentication).

## Local Bridge Design

A small Windows application runs on the same network as the Bambu printer.

Responsibilities:
- Keep printer IP and access credentials local.
- Communicate with the printer over the local network.
- Query printer and AMS status.
- Run local model analysis.
- Invoke Bambu Studio for slicing.
- Maintain per-job logs.
- Expose only approved, safe operations to the ChatGPT-facing layer.
- Maintain the outbound tunnel/relay connection described above.

Printer credentials must not be stored in the ChatGPT plugin itself.

Local network access depends on Bambu Lab's LAN-only "Developer Mode"
(MQTT/live-stream/FTP left open, no cloud dependency). Bambu Lab does not
provide support for this mode and could change it in a future firmware
update. The bridge should detect a broken local connection and report it
clearly (see Failure Behavior) rather than assuming the API will always
behave the same way.

## Model Analyzer

Supported input in V1:
- STL
- 3MF

Analysis targets:
- Overall X/Y/Z dimensions.
- Bounding box.
- A1 bed-fit check.
- Thin-wall regions.
- Overhangs.
- Bridges.
- Bed-contact area.
- Small/fine features.
- Fit-sensitive geometry.
- Candidate orientations.
- Non-manifold or invalid geometry warnings when detectable.

## Print Optimizer

Inputs:
- Model-analysis results.
- Printer: Bambu A1.
- Nozzle size.
- Filament/material.
- AMS slot/material information when available.
- Current Bambu Studio job/profile settings.
- User-selected print goal.

Settings it may recommend changing:
- Layer height.
- Wall loops.
- Top shell layers.
- Bottom shell layers.
- Infill density.
- Infill pattern.
- Outer wall speed.
- Inner wall speed.
- Top surface speed.
- General speed choices relevant to the model.
- Support enable/disable.
- Support threshold angle.
- Support style/settings where safely applicable.
- Bridge/overhang behavior.
- Cooling.
- Seam placement.
- Brim.
- Elephant-foot compensation.
- XY contour compensation.
- XY hole compensation.
- Dimensional/tolerance-related options.
- PLA/PETG-specific process choices.

Rule:
No setting is changed solely because a fixed recipe says so.
Recommendations should be connected to model geometry and the selected goal.

## Temporary Profile Rule

The optimizer must not overwrite normal Bambu Studio presets.

For every job:
1. Read the starting settings.
2. Create a temporary job/profile copy.
3. Apply only user-approved recommendations to that copy.
4. Slice/export from the temporary copy.
5. Leave normal saved Bambu presets unchanged.

## Bambu Studio Integration

Bambu Studio is the slicing engine.

The runner should:
- Locate the installed Bambu Studio CLI/executable.
- Load the model.
- Load printer/process/filament settings.
- Apply approved temporary overrides.
- Orient and arrange when appropriate.
- Slice the model.
- Export an optimized 3MF / sliced job.
- Capture CLI errors and surface the real error message.
- Verify that approved settings actually took effect (see note below)
  before reporting success.

The CLI is real (documented on Bambu's own GitHub wiki: `--slice`,
`--load-settings`, `--load-filaments`, `--export-3mf`, etc.) but it is
undocumented/unpromoted and has known reliability gaps: `--load-settings`
and `--load-filaments` have been reported to silently fall back to
defaults instead of erroring, and some builds fail headless without a
display. The runner must not assume a clean CLI exit means the requested
overrides were honored — it should confirm applied settings against the
exported 3MF/gcode metadata (or an equivalent check) before telling the
user the job succeeded. Plan an early spike to validate this against the
actual installed Bambu Studio version before building the rest of the
runner on top of it.

Known Bambu Studio setting keys confirmed during research include (spot-
checked against current Bambu Studio profiles, Sept 2026):
- wall_loops
- top_shell_layers
- bottom_shell_layers
- sparse_infill_density
- sparse_infill_pattern
- support_threshold_angle
- elefant_foot_compensation (this spelling is correct — it's the actual
  internal key name, inherited from the Slic3r/PrusaSlicer codebase
  Bambu Studio is built on, not a typo in this document)
- xy_contour_compensation
- xy_hole_compensation

## Printer / AMS Status for V1

Printer status:
- Connection state.
- Current print name.
- Progress percentage.
- Current layer / total layers when available.
- Estimated remaining time.
- Nozzle temperature.
- Bed temperature.
- Printer warnings/errors.

AMS status:
- Slot contents.
- Filament type.
- Filament color.
- AMS status.
- Humidity/status information when exposed by the printer interface.

V1 is read-only for physical printer operations.

## Safety Rules

V1 does not expose:
- Raw arbitrary G-code.
- Firmware operations.
- Machine safety-limit overrides.
- Automatic start-print commands.
- Automatic profile replacement.
- Temperature override commands outside normal slicing profiles.
- Unattended physical control.

Future pause/resume/cancel/send-to-printer actions should be separate opt-in capabilities with explicit confirmation.

## Failure Behavior

Printer offline:
- Report unavailable.
- Do not retry forever.

Local API / Developer Mode unreachable:
- Report that local access is unavailable and why (network vs. firmware
  vs. tunnel/relay down) rather than a generic error.
- Do not retry forever.

Unknown filament profile:
- Stop and ask for material/profile.

Invalid/non-manifold model:
- Warn before slicing.

Bambu Studio CLI failure:
- Return the actual error.
- Do not claim success.

CLI silently ignored settings:
- Verify recommended settings actually landed in the exported 3MF/gcode
  metadata before reporting success.
- If a setting did not apply, treat it as a failure and surface it —
  never report success on exit code alone.

Optimizer uncertainty:
- Flag uncertainty.
- Ask for review instead of inventing a value.

Model does not fit the A1 build volume:
- Stop before slicing as a normal single-part job.
- Explain the dimensional conflict.

## Job History

Each optimization job should record:
- Model/file name.
- Date/time.
- Printer.
- Nozzle.
- Material.
- AMS slot if relevant.
- Selected optimization goal.
- Starting settings.
- Recommended settings.
- User-approved settings.
- Geometry warnings.
- Slice warnings.
- Slice result.
- Output-file path.
- Errors.

Long-term benefit:
A successful job can later become a reference:
"This print came out perfectly; use those choices again."

## V1 User Experience

Example request:
"This is a PETG card divider. I care about fit and clean edges."

Expected sequence:
1. User provides STL/3MF.
2. Bambu Companion analyzes the model.
3. Companion verifies printer/nozzle/material context.
4. User selects or confirms the optimization goal.
5. Optimizer compares the model to current Bambu settings.
6. Companion displays an exact settings diff.
7. Companion explains why each change is recommended.
8. User approves or rejects changes.
9. Approved changes go into a temporary per-job profile.
10. Bambu Studio slices the model.
11. Optimized 3MF/sliced job is produced.
12. Job and settings are logged.

## V1 Success Criteria

V1 is successful when a real STL or 3MF can be provided and the software can:
- Inspect it correctly.
- Recognize important printability/geometry characteristics.
- Read the relevant print context.
- Recommend concrete Bambu Studio settings.
- Explain those recommendations.
- Wait for user approval.
- Apply only approved changes.
- Slice successfully.
- Produce an optimized output job.
- Preserve normal Bambu presets.
- Record exactly what happened.

## Explicitly Deferred to Later Versions

- Automatic print start.
- Pause/resume/cancel commands.
- Full remote printer control.
- Raw G-code.
- Firmware control.
- Safety-limit modification.
- Cloud-only printer access.
- Automatic optimization without approval.
- Permanent profile rewriting.
- TPU optimization.
- ABS/ASA optimization.
- Additional Bambu printer families beyond the A1.
- Automatic learning from print-result photos.
- Closed-loop optimization from prior print failures.
- Full slicer-preview visual analysis.

## Research Findings

The feasibility investigation found:
- Bambu provides a Developer Mode path intended for third-party
  integrations, but Bambu Lab does not officially support this mode and
  could change it in a future firmware update — treat it as best-effort,
  not guaranteed.
- Local network integration is preferable for printer credentials and
  control.
- Community tooling demonstrates practical Bambu LAN integration patterns.
- Bambu Studio supports command-line workflows suitable for automated
  slicing, via a real but undocumented/unpromoted CLI that has known
  reliability gaps around applying external settings — validate early.
- ChatGPT apps/plugins use tool interfaces that must be reachable over a
  public HTTPS endpoint; ChatGPT cannot call a local-network address
  directly, so a tunnel or relay component is required between the
  cloud-hosted plugin and the Windows bridge, and that endpoint needs its
  own authentication.
- AMS 2 Pro requires the separate AMS Hub accessory to work with the A1;
  it is not a direct plug-in replacement for the A1's original AMS.

## Current Project Status

Completed:
- Feasibility spike.
- Architecture selection.
- Optimizer behavior approved.
- Review-before-apply behavior approved.
- Safety/data-flow design approved.
- V1 feature scope approved.
- Written design specification created previously.
- Design reviewed and corrected for hardware, connectivity, and CLI
  reliability gaps (2026-09-20).

Not yet implemented:
- Local Windows bridge.
- Tunnel/relay component for ChatGPT connectivity.
- Printer/AMS connection (confirm AMS Hub + firmware first).
- Model analyzer.
- Optimizer engine.
- Bambu Studio runner (spike the CLI settings-application issue early).
- ChatGPT app/plugin interface.
- Job history.
- Tests.
- Installer/setup wizard.

Next step:
- Finish the implementation plan, including the tunnel/relay design and
  an early Bambu Studio CLI validation spike.
- Then implement task-by-task using the approved design.

## Working Name

Bambu Companion

## Review Notes (2026-09-20)

A review of this spec found four gaps, all fixed above:
1. Core Architecture had no way for cloud-hosted ChatGPT to reach the
   local Windows bridge — added a Cloud Relay/Tunnel component and a new
   "ChatGPT Connectivity" section.
2. AMS 2 Pro was listed as compatible with the A1 with no mention of the
   required AMS Hub hardware, bracket, tubing, and firmware version —
   added under User Hardware / Initial Scope.
3. The local printer API (LAN "Developer Mode") was assumed stable —
   added a note that Bambu Lab doesn't officially support it, plus a
   dedicated Failure Behavior case for it going unreachable.
4. The Bambu Studio CLI was assumed to reliably apply settings — added
   known reliability gaps and a requirement to verify applied settings
   against the exported file rather than trusting a clean exit code, plus
   a Failure Behavior case for silent setting failures.

Everything else in the original spec (safety scope, approval workflow,
temporary-profile rule, setting key names) checked out as accurate and
was left unchanged.
