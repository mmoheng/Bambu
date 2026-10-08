"""Printer + AMS status reader, over the printer's local "Developer Mode"
MQTT API (see the project's Local Bridge Design / ChatGPT Connectivity
notes — this only works with Developer Mode enabled and the bridge on
the same LAN as the printer; no cloud dependency).

CONFIDENCE NOTE — updated 2026-09-20 against a real A1 (serial redacted,
host 192.168.1.x, a 45-second idle-printer capture, 17 messages):

CONFIRMED for real: the topic `device/{serial}/report` (note: the
broker's ACL rejects a wildcard subscribe like `device/+/report` and
silently disconnects/reconnects the client instead of erroring clearly —
you must know the real serial ahead of time); the `bblp`/access-code
auth; the AMS shape (`print.ams.ams[].tray[]` with `tray_type`,
`tray_color`, `id`, and unit-level `humidity` — matches this module's
parsing exactly); the `bed_temper` field name and that it's a plain
float in °C.

CONFIRMED, and a real fix applied because of it: Bambu's `push_status`
messages are PARTIAL DELTAS, not full state snapshots — across the 17
captured messages, one had only `ams`, another only `bed_temper`,
another only `wifi_signal`, never all fields at once. `parse_report_payload`
now expects to be called with accumulated state (merged across messages
via `merge_report_delta`), which is what `PrinterStatusClient` does
internally — calling it directly with a single raw delta message will
under-report almost everything, by design of the real protocol, not a
bug in this code.

CONFIRMED 2026-10-08 during a live print on the same A1 (through the
connector's `printer_status`, which reports the field names it
received): `nozzle_temper`, `gcode_state`, `mc_percent`, `layer_num`,
`total_layer_num`, `subtask_name`, `gcode_file` and `mc_remaining_time`
all arrive under exactly these names, and parsed to the job that was
really running (state RUNNING, its name, percent, layers, minutes left).

Also seen that day: those fields come only in the printer's FULL
report. The small in-between updates carry temperatures, fans and wifi
and nothing else, and left alone the printer sends the full report
only on its own schedule — two checks half a minute after connecting
had neither job state nor AMS contents; one a couple of minutes later
had everything. So the client now asks for it once on connecting (a
`pushall` request, see `FULL_REPORT_REQUEST`), and `has_full_report`
says whether it has arrived. CONFIRMED the same day on the real A1:
with the request in place, the first check after a connector restart
came back complete (job state and AMS contents) inside the default
8-second wait.

STILL UNCONFIRMED: the shape of an `hms` (warning) entry. The field
arrives under that name, but it was empty in every report seen so far.

This module requires `paho-mqtt` (`pip install paho-mqtt`) on the
machine running the bridge.

V1 is read-only: this module never publishes a command that changes
printer state (no start/pause/stop/temperature-override). The single
message it does publish, `FULL_REPORT_REQUEST`, only asks the printer
to report its status (the owner asked for this on 2026-10-08; until
then the module published nothing). See the project's Safety Rules.
"""

from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

try:
    import paho.mqtt.client as mqtt  # type: ignore
except ImportError:  # pragma: no cover - exercised on the bridge machine, not in CI
    mqtt = None  # Import lazily-checked in PrinterStatusClient.connect()


class PrinterConnectionError(RuntimeError):
    pass


class Topics:
    """MQTT topic templates. `{serial}` is the printer's serial number,
    shown in Bambu Studio / Bambu Handy device info. Confirm these
    against your printer before relying on them (see module docstring).
    """

    REPORT = "device/{serial}/report"  # printer publishes status here
    REQUEST = "device/{serial}/request"  # only FULL_REPORT_REQUEST is ever published here


# The one message this module publishes: it asks the printer to send its
# full status report, and changes nothing on the printer.
FULL_REPORT_REQUEST = {"pushing": {"sequence_id": "0", "command": "pushall"}}
# The community protocol notes (OpenBambuAPI) advise not sending pushall
# more often than every 5 minutes on the P1-class boards, which lag
# under it. A reconnect loop (see PrinterStatusClient's NOTE) must not
# turn into a stream of requests.
FULL_REPORT_MIN_INTERVAL_S = 300.0


@dataclass(frozen=True)
class AmsSlotStatus:
    slot_index: int
    filament_type: Optional[str]
    color: Optional[str]
    humidity: Optional[str] = None


@dataclass(frozen=True)
class PrinterStatus:
    connected: bool
    print_name: Optional[str]
    progress_percent: Optional[float]
    current_layer: Optional[int]
    total_layers: Optional[int]
    remaining_time_min: Optional[int]
    nozzle_temp_c: Optional[float]
    bed_temp_c: Optional[float]
    warnings: list[str]
    ams_slots: list[AmsSlotStatus]
    raw: dict[str, Any]


def merge_report_delta(state: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Recursively merges one MQTT report delta into an accumulated state
    dict, returning a NEW dict (does not mutate `state`). Confirmed
    necessary against a real A1 (see module docstring): push_status
    messages are partial deltas, so the caller must keep one running
    `state` dict per connection and merge every incoming delta into it
    before building a PrinterStatus — that's what `PrinterStatusClient`
    does internally.

    Dict values are merged recursively, key by key. Any other value
    (including lists, e.g. the AMS `tray` array) is replaced outright by
    the newer delta's value — confirmed every captured `ams` delta
    carried its full tray array, never a partial one, so list merging
    isn't needed and would risk stitching together stale + fresh trays
    incorrectly if it were.
    """
    merged = dict(state)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_report_delta(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_report_payload(payload: dict[str, Any]) -> PrinterStatus:
    """Pure parser: dict in, PrinterStatus out. Kept separate from the
    MQTT plumbing so it's unit-testable with a synthetic payload — no
    printer or network required. The field names below are confirmed
    against a real A1, idle and mid-print (see module docstring); only
    the shape of an `hms` entry is still a guess.
    """
    print_data = payload.get("print", payload)

    ams_slots: list[AmsSlotStatus] = []
    ams_block = print_data.get("ams", {})
    for unit in ams_block.get("ams", []) if isinstance(ams_block, dict) else []:
        for tray in unit.get("tray", []):
            try:
                idx = int(tray.get("id", -1))
            except (TypeError, ValueError):
                idx = -1
            ams_slots.append(
                AmsSlotStatus(
                    slot_index=idx,
                    filament_type=tray.get("tray_type"),
                    color=tray.get("tray_color"),
                    humidity=unit.get("humidity"),
                )
            )

    return PrinterStatus(
        connected=True,
        print_name=print_data.get("subtask_name") or print_data.get("gcode_file"),
        progress_percent=_as_float(print_data.get("mc_percent")),
        current_layer=_as_int(print_data.get("layer_num")),
        total_layers=_as_int(print_data.get("total_layer_num")),
        remaining_time_min=_as_int(print_data.get("mc_remaining_time")),
        nozzle_temp_c=_as_float(print_data.get("nozzle_temper")),
        bed_temp_c=_as_float(print_data.get("bed_temper")),
        warnings=list(print_data.get("hms", []) or []),
        ams_slots=ams_slots,
        raw=payload,
    )


# Only the printer's full report carries the job state; the small
# in-between updates (temperatures, fans, wifi) never do.
FULL_REPORT_FIELD = "gcode_state"


def has_full_report(status: PrinterStatus) -> bool:
    """Whether the accumulated state includes a full report yet. Until
    it does, job state, progress and AMS contents may simply be missing
    (see module docstring) — that is not the same as an idle printer."""
    print_data = status.raw.get("print", status.raw) if isinstance(status.raw, dict) else {}
    return FULL_REPORT_FIELD in print_data


def status_to_dict(status: PrinterStatus) -> dict[str, Any]:
    """PrinterStatus as plain JSON-friendly data for a caller outside
    this PC (the connector, the HTTP bridge).

    Deliberately leaves out `raw` — the accumulated MQTT state carries
    hardware identifiers (serial, tray UUIDs, chip IDs) that have no
    business leaving the machine. `fields_received` lists only the
    *names* of the top-level fields seen so far, without the values:
    enough to check the parser against what the printer really sends.
    `complete` is False until a full report has arrived (see
    `has_full_report`).
    """
    print_data = status.raw.get("print", status.raw) if isinstance(status.raw, dict) else {}
    return {
        "connected": status.connected,
        "print_name": status.print_name,
        "state": print_data.get("gcode_state"),
        "progress_percent": status.progress_percent,
        "current_layer": status.current_layer,
        "total_layers": status.total_layers,
        "remaining_time_min": status.remaining_time_min,
        "nozzle_temp_c": status.nozzle_temp_c,
        "bed_temp_c": status.bed_temp_c,
        "warnings": status.warnings,
        "ams_slots": [
            {
                "slot_index": s.slot_index,
                "filament_type": s.filament_type,
                "color": s.color,
                "humidity": s.humidity,
            }
            for s in status.ams_slots
        ],
        "fields_received": sorted(str(k) for k in print_data),
        "complete": has_full_report(status),
    }


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class PrinterStatusClient:
    """Thin MQTT wrapper. Connects, subscribes to the report topic, merges
    each incoming delta into a running state dict (see `merge_report_delta`
    — confirmed necessary against a real printer, whose push_status
    messages are partial, not full snapshots), and hands the resulting
    PrinterStatus to `on_status`. On connecting it asks the printer for
    its full report (`FULL_REPORT_REQUEST`); it publishes nothing else
    (V1 read-only scope).

    Usage (on the bridge machine, with paho-mqtt installed):

        client = PrinterStatusClient(
            host="192.168.1.50", serial="...", access_code="...",
            on_status=lambda s: print(s),
        )
        client.connect()
        client.loop_forever()  # or loop_start() for a background thread

    NOTE: confirmed against a real A1 that the broker's ACL rejects a
    wildcard topic subscribe (`device/+/report`) — it silently
    disconnects/reconnects instead of raising a clear subscribe error —
    so `serial` must be the printer's real serial number, not a guess.
    """

    def __init__(
        self,
        *,
        host: str,
        serial: str,
        access_code: str,
        on_status: Callable[[PrinterStatus], None],
        port: int = 8883,
    ):
        if mqtt is None:
            raise PrinterConnectionError(
                "paho-mqtt is not installed. Run `pip install paho-mqtt` on the bridge machine."
            )
        self.host = host
        self.port = port
        self.serial = serial
        self.access_code = access_code
        self.on_status = on_status
        self._state: dict[str, Any] = {}
        self._full_report_requested_at: Optional[float] = None
        # paho-mqtt 2.x made the callback API version an explicit
        # argument; the callbacks below use the 1.x signatures, which
        # VERSION1 keeps. paho-mqtt 1.x has no such argument.
        if hasattr(mqtt, "CallbackAPIVersion"):
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        else:
            self._client = mqtt.Client()
        self._client.username_pw_set("bblp", access_code)
        # Bambu's local MQTT broker uses a self-signed cert in LAN/Developer
        # Mode; this disables hostname/cert verification for the LOCAL
        # connection only. Do not reuse this TLS config for anything
        # internet-facing (e.g. the tunnel/relay endpoint) — that needs
        # real certificate validation.
        self._client.tls_set(cert_reqs=ssl.CERT_NONE)
        self._client.tls_insecure_set(True)
        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message

    def connect(self, timeout_s: float = 10.0) -> None:
        try:
            self._client.connect(self.host, self.port, keepalive=30)
        except OSError as exc:
            # No address in the message: it is returned to callers
            # outside this PC (the HTTP bridge, the connector). The OS
            # error is kept as the exception's cause for local debugging.
            raise PrinterConnectionError(
                "Could not reach the printer at its saved address — is it powered on, on "
                f"the same LAN, and in Developer/LAN Mode? ({type(exc).__name__})"
            ) from exc

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def disconnect(self) -> None:
        self._client.disconnect()

    def reset_state(self) -> None:
        """Clears accumulated state. Call this if you want to discard
        stale values (e.g. a previous print's leftover progress) rather
        than carrying them forward until fresh deltas overwrite them —
        not called automatically on reconnect, since the printer
        typically re-sends current values again shortly after a
        reconnect anyway."""
        self._state = {}

    def _handle_connect(self, client, userdata, flags, rc):  # noqa: ANN001 - paho callback signature
        client.subscribe(Topics.REPORT.format(serial=self.serial))
        if rc == 0:  # connection accepted
            self._request_full_report(client)

    def _request_full_report(self, client) -> None:  # noqa: ANN001 - paho client
        now = time.monotonic()
        last = self._full_report_requested_at
        if last is not None and now - last < FULL_REPORT_MIN_INTERVAL_S:
            return
        self._full_report_requested_at = now
        client.publish(Topics.REQUEST.format(serial=self.serial), json.dumps(FULL_REPORT_REQUEST))

    def _handle_message(self, client, userdata, msg):  # noqa: ANN001 - paho callback signature
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        delta = payload.get("print", payload)
        self._state = merge_report_delta(self._state, delta)
        self.on_status(parse_report_payload(self._state))
