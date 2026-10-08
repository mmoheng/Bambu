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

A second front door, added 2026-10-07, reaches the same boxes without
the cloud relay:

```
Claude Desktop (on the same PC)
    |  stdin/stdout, Model Context Protocol
    v
bambu_companion/mcp_server.py        <- protocol only, stdlib, no logic
    |
    v
bambu_companion/service.py           <- every operation as plain Python
    |
    +--> Model Analyzer, Print Optimizer, Job History   (as above)
    +--> profiles/project_3mf.py     read / copy-with-changes a project file
    +--> bridge/studio_runner.py     slice (after bridge/slice_check.py passes)
    +--> bridge/printer_status.py    read-only status
```

`service.py` is deliberately framework-free so the HTTP bridge can call
the same methods once slicing is confirmed, instead of the two front
doors growing separate logic.

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
- **The settings registry is also the allowlist.**
  `profiles/bambu_settings.py` lists every setting this project may
  read, recommend or write, with its type and a sane range; nothing
  outside it is ever written into a profile or project file. That is
  how "no arbitrary G-code, no safety-limit changes" is enforced in
  code rather than by convention. Each key also carries a `verified`
  flag (seen in a real Bambu Studio file or not); the optimizer
  discloses any recommendation whose key isn't.
- **Typed values inside, Bambu's strings at the edge.**
  `profiles/bambu_values.py` converts between `"15%"` / `["200"]` /
  `"1"` and `15` / `200` / `True`, and writes changes back in whatever
  shape the target file already uses.
- **Outputs never replace a file.** `write_project_copy` and the
  runner's `place_exclusive` create their output with exclusive mode, so
  a name that is already taken — even one taken *during* a long slice —
  is an error or leads to the next free name, never a silent
  replacement. (This covers the service, the connector and the runner.
  The desktop GUI's "Export recommendations" is older code and still
  rewrites its own previous export files.)
- **Geometric heuristics are labeled as heuristics.** Overhang/bridge
  detection, thin-wall thickness, hole detection, and orientation ranking
  are all documented in their docstrings as approximations of what a real
  slicer computes, with the known gaps spelled out (e.g. hole detection
  only finds OPEN boundary loops, not closed cylindrical bores in a
  watertight solid). This matches the memory bank's own Failure Behavior
  rule — flag uncertainty instead of quietly overclaiming precision.
