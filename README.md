# Bambu Companion

Code for the project described in the "Bambu Companion" memory bank
(Bambu Lab A1 + AMS 2 Pro, ChatGPT-connected print optimization via a
local Windows bridge and Bambu Studio). See `docs/ARCHITECTURE.md` for
how this code maps to that design, and `bambu_companion/bridge/README.md`
for the honest per-file status of everything that still needs your
printer/Bambu Studio/OpenAI setup to finish.

## What's implemented and tested right now

- **Model Analyzer** (`bambu_companion/model_analyzer/`): loads STL/3MF,
  computes bounding box, A1 bed-fit, bed-contact area, overhangs,
  bridges, sampled thin-wall thickness, small (multi-body) features,
  fit-sensitive open holes, candidate resting orientations, and
  manifold/watertight warnings. Pure Python + numpy/scipy/networkx, no
  printer or Bambu Studio needed.
- **Print Optimizer** (`bambu_companion/optimizer/`): turns an analysis +
  chosen goal into a Current → Recommended settings diff, every change
  tied to a specific reason referencing the model's actual geometry
  and/or the goal — never a fixed recipe. Anything it can't confidently
  turn into a setting value (bridge cooling, seam placement, exact
  compensation numbers) is reported as an uncertainty instead of guessed.
- **Temporary Profile logic** (`bambu_companion/profiles/temp_profile.py`):
  pure "apply only the approved changes to a copy of the settings" step —
  the code-level guarantee behind "never overwrite normal presets."
- **Job History** (`bambu_companion/bridge/job_history.py`): a real,
  working JSON-backed store for the memory bank's Job History section.

Run the test suite:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

Try it on a real file (no printer needed):

```bash
python -m bambu_companion.cli path/to/model.stl \
    --goal dimensional_accuracy --material PETG \
    --current wall_loops=2 --current sparse_infill_density=15
```

## What still needs your environment

Everything above was built and tested without a printer, without Bambu
Studio installed, and without package-index access (the dev sandbox this
was built in has none of those) — so the pieces that inherently depend on
them are written as completely as possible but not yet exercised for
real:

- **`bridge/studio_runner.py`** — real Bambu Studio CLI command-builder
  (unit tested) + subprocess wrapper (untested end-to-end). Needs Bambu
  Studio installed on your PC.
- **`bridge/printer_status.py`** — real MQTT client for the printer's
  local Developer Mode API. Topic names are a documented best guess (see
  its docstring) that need confirming against your actual A1. Needs
  `pip install paho-mqtt` and a printer with Developer Mode on.
- **`bridge/server.py`** — the HTTP layer ChatGPT would call through a
  tunnel/relay (see the memory bank's "ChatGPT Connectivity" section).
  Skeleton only; needs `pip install -r requirements-bridge.txt` plus the
  tunnel/relay component and an OpenAI developer/connector setup, neither
  of which exists in this repo yet.

See `bambu_companion/bridge/README.md` for the exact gap to close on each
file.

## Project layout

```
bambu_companion/
  schemas.py           # shared dataclasses used everywhere
  model_analyzer/       # STL/3MF loading + geometry analysis (real, tested)
  optimizer/            # goal + geometry -> settings diff (real, tested)
  profiles/              # setting-key registry + temp-profile logic (real, tested)
  bridge/                 # printer status, Bambu Studio runner, job history,
                           # ChatGPT-facing server (mixed — see status table above)
  cli.py                  # local testing entry point, no printer needed
tests/                    # unittest suite (stdlib unittest, no pytest needed)
docs/
  ARCHITECTURE.md         # maps this code back to the approved design
```

## Next steps (suggested order)

1. Install Bambu Studio on the target Windows PC, run `--help`, and
   reconcile `studio_runner._KNOWN_FLAGS` against your version.
2. Do one real slice through `studio_runner.run_slice_job` on a simple
   model and check `verify_settings_applied` actually finds your settings
   in the exported 3MF — tighten the heuristic once you've seen a real
   file.
3. Turn on Developer Mode on the A1, capture a real MQTT report payload,
   and reconcile `printer_status.Topics` / `parse_report_payload` against
   it.
4. Install `requirements-bridge.txt`, bring up `bridge/server.py`
   locally, and confirm the `/model/analyze` and `/optimize` endpoints
   work end-to-end against a real file.
5. Build the tunnel/relay component from the memory bank's "ChatGPT
   Connectivity" section, then set up the ChatGPT App/connector itself.
