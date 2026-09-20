"""Printer + AMS status reader, over the printer's local "Developer Mode"
MQTT API (see the project's Local Bridge Design / ChatGPT Connectivity
notes — this only works with Developer Mode enabled and the bridge on
the same LAN as the printer; no cloud dependency).

CONFIDENCE NOTE: the MQTT topic names and report-message shape below
follow the pattern used by community Bambu LAN integrations (this is the
same "exploited MQTT protocol" class of access the project's Research
Findings referenced) — they were NOT independently re-verified against a
live printer or Bambu's own protocol docs this session. Treat the
constants in `Topics` as a documented starting point to confirm against
your actual A1 (a local MQTT client like `mosquitto_sub` pointed at the
printer's IP with Developer Mode on will show you the real topic/payload
shape), not as verified fact. This module requires `paho-mqtt`
(`pip install paho-mqtt`) on the machine running the bridge — not
available in the sandbox this was developed in, so it has NOT been
exercised against a real broker; only the parsing/dataclass logic below
is unit-tested (with synthetic payloads).

V1 is read-only: this module never publishes a command that changes
printer state (no start/pause/stop/temperature-override). See the
project's Safety Rules.
"""

from __future__ import annotations

import json
import ssl
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
    REQUEST = "device/{serial}/request"  # read-only client should NOT publish here in V1


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


def parse_report_payload(payload: dict[str, Any]) -> PrinterStatus:
    """Pure parser: dict in, PrinterStatus out. Kept separate from the
    MQTT plumbing so it's unit-testable with a synthetic payload — no
    printer or network required. Field paths below are a best-effort
    guess at the report shape based on common community-client fields
    (`gcode_state`, `mc_percent`, `layer_num`, `total_layer_num`,
    `nozzle_temper`, `bed_temper`, `ams`); confirm/adjust field names
    against a captured real payload from your printer (see module
    docstring) before trusting this in production.
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
    """Thin MQTT wrapper. Connects, subscribes to the report topic, and
    hands each parsed PrinterStatus to `on_status`. Never publishes
    anything (V1 read-only scope).

    Usage (on the bridge machine, with paho-mqtt installed):

        client = PrinterStatusClient(
            host="192.168.1.50", serial="...", access_code="...",
            on_status=lambda s: print(s),
        )
        client.connect()
        client.loop_forever()  # or loop_start() for a background thread
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
            raise PrinterConnectionError(
                f"Could not reach printer at {self.host}:{self.port} — is it powered on, on "
                "the same LAN, and in Developer/LAN Mode? ({exc})"
            ) from exc

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def disconnect(self) -> None:
        self._client.disconnect()

    def _handle_connect(self, client, userdata, flags, rc):  # noqa: ANN001 - paho callback signature
        client.subscribe(Topics.REPORT.format(serial=self.serial))

    def _handle_message(self, client, userdata, msg):  # noqa: ANN001 - paho callback signature
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        self.on_status(parse_report_payload(payload))
