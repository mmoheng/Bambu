# Bridge components

Everything in this folder is meant to run on the Windows PC next to the
printer, per the memory bank's Local Bridge Design. Honest status of
each file, since these need things this dev environment didn't have
(a real printer, Bambu Studio installed, package-index access, an
OpenAI developer setup):

| File | Status | What it needs to finish/verify |
|---|---|---|
| `job_history.py` | **Real, tested.** Stdlib-only JSON store. | Nothing — works as-is. |
| `studio_runner.py` | **Real logic, untested end-to-end.** Command-builder (`build_cli_args`) is pure and unit-tested. `run_slice_job` / `verify_settings_applied` are written against Bambu's documented CLI but have never actually been run against a real Bambu Studio install. | Run `<bambu-studio-exe> --help` on your machine and reconcile against `_KNOWN_FLAGS`; then do a real slice and check `verify_settings_applied` actually finds your settings in the exported 3MF metadata (it's a text-scan heuristic — tighten it once you've looked at a real file). |
| `printer_status.py` | **Real MQTT client code, unverified topic/payload shapes.** The parser (`parse_report_payload`) is unit-tested against synthetic payloads. | Confirm the real topic names and report JSON shape against your A1 in Developer Mode (e.g. `mosquitto_sub -h <printer-ip> -p 8883 -u bblp -P <access-code> -t 'device/+/report' --insecure`), then adjust `Topics` / `parse_report_payload` to match. Needs `pip install paho-mqtt`. |
| `server.py` | **Skeleton, not run.** Needs `fastapi`/`uvicorn` (not installable in the dev sandbox — no package-index access there). Endpoint handlers are thin wrappers around the already-tested analyzer/optimizer/job_history/temp_profile code. | `pip install -r requirements-bridge.txt`, run it, hit the endpoints. Also still needs the tunnel/relay component from the memory bank's "ChatGPT Connectivity" section before ChatGPT itself can reach it — that's a separate piece, not part of this file. |

## Why the split

`model_analyzer` and `optimizer` (one level up) are pure Python + numpy/
scipy/networkx with no hardware or network dependency, so they're fully
built and tested (see `tests/`). Everything in this folder inherently
needs the user's specific environment — the printer, Bambu Studio, or an
OpenAI developer account — to actually exercise, so it's written as
completely and honestly as it can be without that environment, with the
exact gap to close spelled out per file above rather than glossed over.
