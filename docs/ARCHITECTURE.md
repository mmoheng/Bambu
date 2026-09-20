# Architecture mapping

How this codebase maps back to the memory bank's approved Core
Architecture diagram (updated 2026-09-20 to add the ChatGPT Connectivity
component — see the memory bank's Review Notes).

```
ChatGPT
    |
    v
Bambu Plugin/App (cloud-hosted)         <- not built yet; lives outside this repo
    |
    v
Cloud Relay / Tunnel                     <- not built yet (see memory bank's
    |                                        "ChatGPT Connectivity" section)
    v
Bambu Bridge (bambu_companion/bridge/)   <- runs on the Windows PC
    |
    +--> Model Analyzer          -> bambu_companion/model_analyzer/   [done, tested]
    |
    +--> Print Optimizer         -> bambu_companion/optimizer/        [done, tested]
    |
    +--> Bambu Studio Runner     -> bambu_companion/bridge/studio_runner.py
    |                                [command-builder tested; execution untested —
    |                                 needs Bambu Studio installed]
    |
    +--> Bambu A1 + AMS 2 Pro    -> bambu_companion/bridge/printer_status.py
                                     [MQTT client written; topic/payload shape
                                      unverified — needs a real printer]
```

Supporting pieces that cut across the diagram:

- `bambu_companion/schemas.py` — the shapes every box above passes to the
  next one (`AnalysisResult` out of Model Analyzer, `OptimizationResult`
  out of Print Optimizer, etc.), so they compose without needing the same
  process/framework.
- `bambu_companion/profiles/temp_profile.py` — the Temporary Profile Rule
  from the memory bank, implemented as a pure function between Print
  Optimizer's output and Bambu Studio Runner's input.
- `bambu_companion/bridge/job_history.py` — the Job History section,
  written independently of everything else above (it only needs a
  filesystem).
- `bambu_companion/bridge/server.py` — where all of the above gets wired
  into HTTP endpoints for the (not-yet-built) ChatGPT Plugin/App to call
  through the (not-yet-built) tunnel/relay.

## Design decisions worth knowing about

- **No trimesh/numpy-stl.** The dev environment this was built in has no
  package-index access, so `model_analyzer/mesh_io.py` and `geometry.py`
  are hand-rolled on top of numpy/scipy/networkx (all three came
  preinstalled). This is a real, if unusual, engineering constraint —
  not a stylistic choice — but it also means the Windows bridge doesn't
  need those heavier packages either, which is arguably a nice side
  effect for something meant to be lightweight to install.
- **Two confidence tiers for setting keys.** `profiles/bambu_settings.py`
  marks each setting key `verified` (confirmed against Bambu's own
  sources during the design review) or not (standard, likely-correct,
  but not individually re-checked this session). The optimizer discloses
  this in its `uncertainties` output rather than presenting every
  recommendation with equal confidence.
- **Geometric heuristics are labeled as heuristics.** Overhang/bridge
  detection, thin-wall thickness, hole detection, and orientation ranking
  are all documented in their docstrings as approximations of what a real
  slicer computes, with the known gaps spelled out (e.g. hole detection
  only finds OPEN boundary loops, not closed cylindrical bores in a
  watertight solid). This matches the memory bank's own Failure Behavior
  rule — flag uncertainty instead of quietly overclaiming precision.
