import json
import types
import unittest
from unittest import mock

from bambu_companion.bridge import printer_status as ps
from bambu_companion.bridge.printer_status import (
    has_full_report,
    merge_report_delta,
    parse_report_payload,
    status_to_dict,
)

# Shapes below are drawn from a real 45-second capture against a Bambu Lab
# A1 (2026-09-20, idle, Developer/LAN Mode) — see bridge/printer_status.py's
# module docstring and bridge/README.md for the full findings. Hardware
# identifiers (chip_id, tray_uuid, tag_uid, ams_id, serial) are replaced
# with generic placeholders; every field NAME, nesting shape, and value
# TYPE matches what the real printer actually sent.

REAL_AMS_DELTA = {
    "ams": {
        "ams": [
            {
                "chip_id": "placeholder-chip-id",
                "ams_id": "placeholder-ams-id",
                "check": 1,
                "id": "0",
                "humidity": "1",
                "humidity_raw": "47",
                "temp": "24.7",
                "dry_time": 0,
                "info": "2003",
                "tray": [
                    {
                        "id": "0",
                        "state": 3,
                        "remain": 9,
                        "tray_type": "PLA",
                        "tray_sub_brands": "PLA Basic",
                        "tray_color": "000000FF",
                        "tray_weight": "1000",
                        "tray_diameter": "1.75",
                        "nozzle_temp_max": "230",
                        "nozzle_temp_min": "190",
                        "tray_uuid": "placeholder-uuid-0",
                    },
                    {
                        "id": "1",
                        "state": 3,
                        "remain": 44,
                        "tray_type": "PLA",
                        "tray_sub_brands": "PLA Basic",
                        "tray_color": "BECF00FF",
                        "tray_weight": "1000",
                        "tray_diameter": "1.75",
                        "nozzle_temp_max": "230",
                        "nozzle_temp_min": "190",
                        "tray_uuid": "placeholder-uuid-1",
                    },
                ],
            }
        ]
    },
    "command": "push_status",
    "msg": 1,
    "sequence_id": "25889",
}

REAL_BED_TEMP_DELTA = {
    "bed_temper": 23.0625,
    "command": "push_status",
    "msg": 1,
    "sequence_id": "25924",
}

REAL_WIFI_ONLY_DELTA = {
    "wifi_signal": "-43dBm",
    "command": "push_status",
    "msg": 1,
    "sequence_id": "25890",
}

# The job fields of the full report the same A1 sent during a live print
# on 2026-10-08. Names are exactly what it sent; the values are what the
# connector parsed from them at the time (their raw types were not
# captured, so the parser's int/float coercion is what is under test).
REAL_MID_PRINT_FIELDS = {
    "gcode_state": "RUNNING",
    "subtask_name": "top_layer_finish_test",
    "mc_percent": 13,
    "layer_num": 0,
    "total_layer_num": 8,
    "mc_remaining_time": 39,
    "nozzle_temper": 139.9375,
    "bed_temper": 64.875,
    "hms": [],
    "command": "push_status",
}


class TestMergeReportDelta(unittest.TestCase):
    def test_second_delta_adds_new_top_level_field(self):
        state = merge_report_delta({}, REAL_AMS_DELTA)
        state = merge_report_delta(state, REAL_BED_TEMP_DELTA)
        self.assertIn("ams", state)
        self.assertEqual(state["bed_temper"], 23.0625)

    def test_later_scalar_overwrites_earlier_value(self):
        state = merge_report_delta({}, REAL_BED_TEMP_DELTA)
        state = merge_report_delta(state, {"bed_temper": 24.5, "command": "push_status", "sequence_id": "x"})
        self.assertEqual(state["bed_temper"], 24.5)

    def test_nested_dict_merges_recursively_not_wholesale_replace(self):
        # A delta that only updates ams[0]'s outer fields, keeping the
        # top-level "ams" dict itself present in both — confirms the merge
        # descends into nested dicts rather than one clobbering the other.
        state = merge_report_delta({}, {"ams": {"other_field": 1}})
        state = merge_report_delta(state, {"ams": {"another_field": 2}})
        self.assertEqual(state["ams"], {"other_field": 1, "another_field": 2})

    def test_list_values_are_replaced_not_merged(self):
        # Confirmed against real captures: the AMS "ams" list (and each
        # unit's "tray" list) always arrives whole, never partially — so
        # replacement, not element-wise merging, is the correct behavior.
        state = merge_report_delta({}, {"ams": {"ams": [{"id": "0"}]}})
        state = merge_report_delta(state, {"ams": {"ams": [{"id": "0"}, {"id": "1"}]}})
        self.assertEqual(len(state["ams"]["ams"]), 2)

    def test_original_state_dict_not_mutated(self):
        state = {"bed_temper": 20.0}
        merge_report_delta(state, {"bed_temper": 99.0})
        self.assertEqual(state["bed_temper"], 20.0)

    def test_three_real_partial_deltas_accumulate_into_full_picture(self):
        # Mirrors the real capture: no single message had all three of
        # ams/bed_temper/wifi_signal, but all three showed up across a
        # handful of messages.
        state: dict = {}
        for delta in (REAL_AMS_DELTA, REAL_BED_TEMP_DELTA, REAL_WIFI_ONLY_DELTA):
            state = merge_report_delta(state, delta)
        self.assertIn("ams", state)
        self.assertIn("bed_temper", state)
        self.assertIn("wifi_signal", state)


class TestParseReportPayloadAgainstRealShapes(unittest.TestCase):
    def test_ams_only_delta_parses_two_trays(self):
        status = parse_report_payload(REAL_AMS_DELTA)
        self.assertEqual(len(status.ams_slots), 2)
        self.assertEqual(status.ams_slots[0].filament_type, "PLA")
        self.assertEqual(status.ams_slots[0].color, "000000FF")
        self.assertEqual(status.ams_slots[0].humidity, "1")

    def test_bed_temp_only_delta_parses_bed_temper_correctly(self):
        status = parse_report_payload(REAL_BED_TEMP_DELTA)
        self.assertAlmostEqual(status.bed_temp_c, 23.0625)

    def test_merged_state_produces_status_with_all_confirmed_fields(self):
        state: dict = {}
        for delta in (REAL_AMS_DELTA, REAL_BED_TEMP_DELTA):
            state = merge_report_delta(state, delta)
        status = parse_report_payload(state)
        self.assertEqual(len(status.ams_slots), 2)
        self.assertAlmostEqual(status.bed_temp_c, 23.0625)

    def test_wifi_only_delta_does_not_crash_and_leaves_other_fields_none(self):
        status = parse_report_payload(REAL_WIFI_ONLY_DELTA)
        self.assertIsNone(status.bed_temp_c)
        self.assertEqual(status.ams_slots, [])

    def test_mid_print_fields_parse_under_their_confirmed_names(self):
        status = parse_report_payload(REAL_MID_PRINT_FIELDS)
        self.assertEqual(status.print_name, "top_layer_finish_test")
        self.assertEqual(status.progress_percent, 13.0)
        self.assertEqual((status.current_layer, status.total_layers), (0, 8))
        self.assertEqual(status.remaining_time_min, 39)
        self.assertAlmostEqual(status.nozzle_temp_c, 139.9375)
        self.assertEqual(status.warnings, [])


class TestFullReport(unittest.TestCase):
    def test_small_updates_alone_are_not_a_full_report(self):
        state: dict = {}
        for delta in (REAL_BED_TEMP_DELTA, REAL_WIFI_ONLY_DELTA):
            state = merge_report_delta(state, delta)
        status = parse_report_payload(state)
        self.assertFalse(has_full_report(status))
        self.assertFalse(status_to_dict(status)["complete"])

    def test_stays_complete_once_the_full_report_has_arrived(self):
        state = merge_report_delta({}, REAL_MID_PRINT_FIELDS)
        state = merge_report_delta(state, REAL_BED_TEMP_DELTA)  # a later small update
        data = status_to_dict(parse_report_payload(state))
        self.assertTrue(data["complete"])
        self.assertEqual(data["state"], "RUNNING")
        self.assertNotIn("unconfirmed_fields", data)

    def test_wrapped_payload_is_handled_the_same(self):
        self.assertTrue(has_full_report(parse_report_payload({"print": {"gcode_state": "IDLE"}})))


class TestStatusToDict(unittest.TestCase):
    def test_keeps_parsed_fields_and_drops_hardware_identifiers(self):
        state = merge_report_delta({}, REAL_AMS_DELTA)
        state = merge_report_delta(state, {"bed_temper": 24.5, "gcode_state": "IDLE"})
        data = status_to_dict(parse_report_payload(state))
        self.assertEqual(data["bed_temp_c"], 24.5)
        self.assertEqual(data["state"], "IDLE")
        self.assertEqual(len(data["ams_slots"]), 2)
        self.assertEqual(data["ams_slots"][0]["filament_type"], "PLA")
        # Field NAMES only (whatever the printer sent), never their values.
        self.assertTrue({"ams", "bed_temper", "gcode_state"} <= set(data["fields_received"]))
        self.assertTrue(all(isinstance(name, str) for name in data["fields_received"]))
        flat = repr(data)
        for identifier in ("placeholder-chip-id", "placeholder-ams-id", "placeholder-uuid-0"):
            self.assertNotIn(identifier, flat)
        self.assertNotIn("raw", data)

    def test_wrapped_payload_is_handled_the_same(self):
        data = status_to_dict(parse_report_payload({"print": {"bed_temper": 60.0}}))
        self.assertEqual(data["bed_temp_c"], 60.0)
        self.assertEqual(data["fields_received"], ["bed_temper"])


class _FakePahoClient:
    def __init__(self, *args):
        self.args = args

    def username_pw_set(self, *a):
        pass

    def tls_set(self, **kw):
        pass

    def tls_insecure_set(self, value):
        pass


class TestPahoVersionCompatibility(unittest.TestCase):
    """paho-mqtt 2.x requires a callback API version; 1.x has none."""

    def _client_args(self, fake_module):
        from unittest import mock

        from bambu_companion.bridge import printer_status as ps

        with mock.patch.object(ps, "mqtt", fake_module):
            client = ps.PrinterStatusClient(host="h", serial="s", access_code="c", on_status=lambda s: None)
        return client._client.args

    def test_paho_2_gets_the_version_1_callback_api(self):
        import types

        fake = types.SimpleNamespace(
            Client=_FakePahoClient, CallbackAPIVersion=types.SimpleNamespace(VERSION1="V1", VERSION2="V2")
        )
        self.assertEqual(self._client_args(fake), ("V1",))

    def test_paho_1_is_constructed_with_no_arguments(self):
        import types

        self.assertEqual(self._client_args(types.SimpleNamespace(Client=_FakePahoClient)), ())


class _RecordingPahoClient(_FakePahoClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.subscribed: list[str] = []
        self.published: list[tuple[str, str]] = []

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class TestFullReportRequest(unittest.TestCase):
    """The client asks for the printer's full report when it connects,
    and that request is the only thing it ever sends the printer."""

    def setUp(self):
        with mock.patch.object(ps, "mqtt", types.SimpleNamespace(Client=_RecordingPahoClient)):
            self.client = ps.PrinterStatusClient(host="h", serial="SERIAL", access_code="c", on_status=lambda s: None)
        self.paho = self.client._client

    def _connect(self, rc=0):
        self.client._handle_connect(self.paho, None, {}, rc)

    def test_connecting_subscribes_and_asks_for_the_full_report(self):
        self._connect()
        self.assertEqual(self.paho.subscribed, ["device/SERIAL/report"])
        [(topic, payload)] = self.paho.published
        self.assertEqual(topic, "device/SERIAL/request")
        self.assertEqual(json.loads(payload), {"pushing": {"sequence_id": "0", "command": "pushall"}})

    def test_reconnecting_soon_after_does_not_ask_again(self):
        self._connect()
        self._connect()
        self.assertEqual(len(self.paho.subscribed), 2)  # the subscription is renewed
        self.assertEqual(len(self.paho.published), 1)

    def test_asks_again_once_the_minimum_interval_has_passed(self):
        self._connect()
        self.client._full_report_requested_at -= ps.FULL_REPORT_MIN_INTERVAL_S + 1
        self._connect()
        self.assertEqual(len(self.paho.published), 2)

    def test_a_refused_connection_sends_nothing(self):
        self._connect(rc=5)  # 5 = not authorised (wrong access code)
        self.assertEqual(self.paho.published, [])
        self._connect()  # and has not used up the one allowed request
        self.assertEqual(len(self.paho.published), 1)

    def test_incoming_reports_never_cause_anything_to_be_sent(self):
        message = types.SimpleNamespace(payload=json.dumps({"print": REAL_BED_TEMP_DELTA}).encode())
        self.client._handle_message(self.paho, None, message)
        self.assertEqual(self.paho.published, [])


if __name__ == "__main__":
    unittest.main()
