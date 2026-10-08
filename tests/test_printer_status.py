import unittest

from bambu_companion.bridge.printer_status import (
    merge_report_delta,
    parse_report_payload,
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


if __name__ == "__main__":
    unittest.main()
