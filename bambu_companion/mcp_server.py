"""Claude connector for Bambu Companion — a local MCP server.

The HTTP bridge (`bridge/server.py`) is built for ChatGPT: a web server
on this PC reached from the internet through a tunnel. This is the same
functionality offered the other way round, for Claude: the Claude
desktop app starts this program on your PC and talks to it over
stdin/stdout. Nothing listens on a network port, there is no tunnel and
no API key, and the printer's credentials stay in
`%APPDATA%\\BambuCompanion\\` exactly as before — no tool takes them as
an argument, and tool results are built from parsed status fields and
fixed error messages, never from the saved address, serial or code.

Run by Claude Desktop (see README, "Claude connector"), or by hand to
check it starts:

    python -m bambu_companion.mcp_server

Protocol: Model Context Protocol over stdio — one JSON-RPC 2.0 message
per line. Implemented directly on the standard library (about 150 lines
of plumbing below) rather than with the `mcp` package, so the connector
needs nothing installed beyond what the analyzer already uses.

Safety: every tool is a thin call into `service.CompanionService`; read
that module's docstring for what it will and won't do. In short — it
reads, it writes NEW files, and it cannot start a print.

HONEST STATUS: tested by driving it as a real process over pipes with a
test client (tests/test_mcp_server.py). Not yet run under Claude Desktop
itself.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any, BinaryIO, Callable

from . import __version__
from .schemas import PrintGoal
from .service import USER_ERRORS, CompanionService

SERVER_NAME = "bambu-companion"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Tools for preparing prints for a Bambu Lab A1 with Bambu Studio on this PC. "
    "Workflow: analyze_model / read_settings to understand the job; recommend_settings to "
    "get proposed changes with reasons; SHOW THEM TO THE USER AND WAIT FOR APPROVAL; then "
    "apply_settings (writes a new project file to open in Bambu Studio) or slice_model "
    "(slices headless and reports time/filament/support). Nothing here can start a print or "
    "change a saved preset, and output files are always new files. Headless slicing is "
    "only trustworthy once slice_check has passed on this machine — check studio_info first. "
    "File paths are full paths on the user's PC."
)

_PATH = {"type": "string", "description": "Full path on this PC."}
_CHANGES = {
    "type": "object",
    "description": (
        "Explicit setting changes as {key: value}, e.g. {\"wall_loops\": 4, "
        "\"support_top_z_distance\": 0.25}. Only keys from list_settings are accepted."
    ),
    "additionalProperties": True,
}
_APPROVED = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Setting keys from that job's recommendation which the user approved.",
}
_JOB = {"type": "string", "description": "job_id returned by recommend_settings."}
_WAIT = {
    "type": "number",
    "description": "Seconds to wait before returning a still-running task (default 40, at most 50).",
}
_PLATE = {
    "type": "integer",
    "minimum": 1,
    "description": "For a multi-plate project: which plate (default 1).",
}


def _tool(name: str, description: str, properties: dict, required: list[str] = (), read_only: bool = False) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "analyze_model",
        "Analyze an .stl or .3mf: size, A1 bed fit, bed contact, overhang regions, flat "
        "ceilings, thin walls, holes, alternative orientations, mesh warnings.",
        {"model_path": _PATH, "plate": _PLATE},
        ["model_path"],
        read_only=True,
    ),
    _tool(
        "read_settings",
        "Read the settings stored inside a Bambu Studio project or sliced .3mf: printer, "
        "process preset, filaments, every setting this project tracks, and — if it was "
        "sliced — print time, filament used, whether support was used, slicer warnings.",
        {"path": _PATH},
        ["path"],
        read_only=True,
    ),
    _tool(
        "recommend_settings",
        "Recommend setting changes for a model and a goal, each with a reason tied to the "
        "model's geometry. Current settings are read from the model if it is a Bambu Studio "
        "project, or from settings_from. Returns a job_id. Recommendations must be shown to "
        "the user and approved before apply_settings / slice_model.",
        {
            "model_path": _PATH,
            "goal": {"type": "string", "enum": [g.value for g in PrintGoal]},
            "material": {"type": "string", "description": "e.g. PLA or PETG. Default: read from the project, else PLA."},
            "nozzle_diameter_mm": {"type": "number"},
            "plate": _PLATE,
            "settings_from": {
                "type": "string",
                "description": "A Bambu Studio project .3mf to read the current settings from, when model_path is a bare STL.",
            },
            "current_settings": {
                "type": "object",
                "description": "Overrides for individual current values, {key: value}.",
                "additionalProperties": True,
            },
        },
        ["model_path", "goal"],
    ),
    _tool(
        "list_settings",
        "List the settings Bambu Companion is allowed to change, with type, range and choices.",
        {},
        read_only=True,
    ),
    _tool(
        "apply_settings",
        "Write a NEW Bambu Studio project .3mf: a copy of the source project with only the "
        "approved settings changed (opens in Bambu Studio as the same preset, modified). The "
        "source is never modified and no file is overwritten. Use after the user approved.",
        {
            "job_id": _JOB,
            "approved_keys": _APPROVED,
            "changes": _CHANGES,
            "source_project": {
                "type": "string",
                "description": "Project .3mf to copy. Default: the job's model, if it is a project.",
            },
            "output_path": {"type": "string", "description": "Default: <source>_companion.3mf next to the source."},
        },
    ),
    _tool(
        "slice_model",
        "Slice with Bambu Studio's command line and report print time, filament and support "
        "use. A project .3mf is sliced with its own settings plus the approved changes; a "
        "bare .stl is sliced with full presets (machine/process/filament names, defaults from "
        "studio_info). Writes a new file; sends nothing to the printer. May return a running "
        "task — poll task_status.",
        {
            "model_path": _PATH,
            "job_id": _JOB,
            "approved_keys": _APPROVED,
            "changes": _CHANGES,
            "output_path": {"type": "string"},
            "machine": {"type": "string", "description": "Machine preset name (see list_presets)."},
            "process": {"type": "string", "description": "Process preset name (see list_presets)."},
            "filament": {"type": "string", "description": "Filament preset name (see list_presets)."},
            "wait_s": _WAIT,
        },
    ),
    _tool(
        "slice_check",
        "Diagnose headless slicing on this PC: tries each documented way of calling the Bambu "
        "Studio command line on a 20 mm cube (or model_path), reports exactly what Bambu "
        "Studio answered, and remembers the first way that works. Run this once before "
        "relying on slice_model. May return a running task — poll task_status.",
        {"model_path": {"type": "string", "description": "Optional .stl or project .3mf to test with."}, "wait_s": _WAIT},
    ),
    _tool(
        "task_status",
        "Status/result of a slice_model or slice_check task that was still running.",
        {"task_id": {"type": "string"}},
        ["task_id"],
        read_only=True,
    ),
    _tool(
        "studio_info",
        "Where Bambu Studio is installed, which preset folders were found, the default "
        "presets, and whether headless slicing has been confirmed working on this PC.",
        {},
        read_only=True,
    ),
    _tool(
        "list_presets",
        "List Bambu Studio preset names of one type, optionally filtered by text.",
        {
            "preset_type": {"type": "string", "enum": ["machine", "process", "filament"]},
            "contains": {"type": "string", "description": "Case-insensitive filter, e.g. 'A1' or 'PETG'."},
        },
        ["preset_type"],
        read_only=True,
    ),
    _tool(
        "printer_status",
        "Read-only printer and AMS status over the local network: what is loaded in each AMS "
        "slot, temperatures, and print progress. Listens only — it cannot control the printer.",
        {"wait_s": {"type": "number", "description": "Seconds to wait for the first report (default 8)."}},
        read_only=True,
    ),
    _tool(
        "job_history",
        "Recent recommendation/slice jobs with what was approved and how they turned out.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        read_only=True,
    ),
    _tool(
        "record_outcome",
        "Save the user's own description of how a print turned out against a job, so good "
        "results can be reused as references.",
        {"job_id": {"type": "string"}, "note": {"type": "string"}},
        ["job_id", "note"],
    ),
]


def _dispatch(service: CompanionService, name: str, a: dict[str, Any]) -> Any:
    if name == "analyze_model":
        return service.analyze_model(a.get("model_path"), a.get("plate"))
    if name == "read_settings":
        return service.read_settings(a.get("path"))
    if name == "recommend_settings":
        return service.recommend(
            a.get("model_path"),
            a.get("goal"),
            material=a.get("material"),
            nozzle_diameter_mm=a.get("nozzle_diameter_mm"),
            current_settings=a.get("current_settings"),
            settings_from=a.get("settings_from"),
            plate=a.get("plate"),
        )
    if name == "list_settings":
        return service.list_settings()
    if name == "apply_settings":
        return service.apply_settings(
            job_id=a.get("job_id"),
            approved_keys=a.get("approved_keys"),
            changes=a.get("changes"),
            source_project=a.get("source_project"),
            output_path=a.get("output_path"),
        )
    if name == "slice_model":
        return service.slice_model(
            model_path=a.get("model_path"),
            job_id=a.get("job_id"),
            approved_keys=a.get("approved_keys"),
            changes=a.get("changes"),
            output_path=a.get("output_path"),
            machine=a.get("machine"),
            process=a.get("process"),
            filament=a.get("filament"),
            wait_s=a.get("wait_s"),
        )
    if name == "slice_check":
        return service.slice_check(a.get("model_path"), wait_s=a.get("wait_s"))
    if name == "task_status":
        return service.task_status(a.get("task_id"))
    if name == "studio_info":
        return service.studio_info()
    if name == "list_presets":
        return service.list_presets(a.get("preset_type"), a.get("contains"))
    if name == "printer_status":
        return service.printer_status(a.get("wait_s"))
    if name == "job_history":
        return service.job_history(a.get("limit", 10))
    if name == "record_outcome":
        return service.record_outcome(a.get("job_id"), a.get("note"))
    raise KeyError(name)


def call_tool(service: CompanionService, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Runs one tool and returns an MCP `tools/call` result. Failures a
    person can act on come back as a readable message with isError set;
    they are never raised into the protocol layer."""
    if not isinstance(name, str) or name not in {t["name"] for t in TOOLS}:
        return _text_result(f"Unknown tool {name!r}.", is_error=True)
    args = arguments if isinstance(arguments, dict) else {}
    try:
        payload = _dispatch(service, name, args)
    except USER_ERRORS as exc:
        text = str(exc)
        details = getattr(exc, "details", None)
        if details:
            shown = {k: details[k] for k in ("command", "returncode", "result_json") if details.get(k) is not None}
            if shown:
                text += "\n\n" + json.dumps(shown, indent=1, default=str)
        return _text_result(text, is_error=True)
    except Exception as exc:  # noqa: BLE001 - a tool bug must not take the connector down
        traceback.print_exc(file=sys.stderr)
        return _text_result(f"Internal error in {name}: {type(exc).__name__}: {exc}", is_error=True)
    return _text_result(json.dumps(payload, indent=1, default=str, ensure_ascii=False))


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    """Minimal MCP stdio server: initialize, ping, tools/list, tools/call."""

    def __init__(self, service: CompanionService, reader: BinaryIO, writer: BinaryIO):
        self.service = service
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def _send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._write_lock:
            self._writer.write(data)
            self._writer.flush()

    def _reply(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def handle(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._error(None, -32600, "Invalid JSON-RPC request.")
            return
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method is None:
            return  # a response to something we never asked; ignore
        if "id" not in message:
            return  # notification (initialized, cancelled, ...): nothing to answer

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            self._reply(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        elif method == "ping":
            self._reply(request_id, {})
        elif method == "tools/list":
            self._reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            # Off the reader thread, so a slow slice never blocks pings
            # or other calls.
            thread = threading.Thread(
                target=self._run_tool, args=(request_id, params.get("name"), params.get("arguments")), daemon=True
            )
            self._threads.append(thread)
            thread.start()
        elif method in ("resources/list", "prompts/list"):
            self._reply(request_id, {method.split("/")[0]: []})
        else:
            self._error(request_id, -32601, f"Method not found: {method}")

    def _run_tool(self, request_id: Any, name: Any, arguments: Any) -> None:
        """Worker-thread body for one tools/call. Whatever happens, the
        request gets exactly one reply — a call that is never answered
        leaves the host waiting until its own timeout."""
        try:
            result = call_tool(self.service, name, arguments)
        except BaseException as exc:  # noqa: BLE001 - last line of defence on a worker thread
            traceback.print_exc(file=sys.stderr)
            result = _text_result(f"Internal error: {type(exc).__name__}: {exc}", is_error=True)
        self._reply(request_id, result)

    def serve_forever(self) -> None:
        while True:
            line = self._reader.readline()
            if not line:
                break  # the host closed stdin: we're done
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._error(None, -32700, "Parse error.")
                continue
            if isinstance(message, list):  # JSON-RPC batch (older protocol versions)
                for item in message:
                    self.handle(item)
            else:
                self.handle(message)
        for thread in self._threads:
            thread.join(timeout=5)


def main(make_service: Callable[[], CompanionService] = CompanionService) -> int:
    # stdout carries the protocol and nothing else. Anything that prints
    # (a library warning, a stray debug line) must go to stderr, where
    # Claude Desktop collects it in the connector's log.
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr
    service = make_service()
    try:
        McpServer(service, sys.stdin.buffer, protocol_out).serve_forever()
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
