import unittest

from bambu_companion.profiles.bambu_settings import SETTINGS
from bambu_companion.profiles.bambu_values import (
    SettingValueError,
    looks_like_bambu_config,
    overlay,
    to_bambu_string,
    to_typed,
    typed_settings,
    validate,
    values_equal,
)

from .project_fixtures import REAL_STYLE_SETTINGS


class TestToTyped(unittest.TestCase):
    def test_real_file_formats(self):
        self.assertEqual(to_typed("wall_loops", "2"), 2)
        self.assertEqual(to_typed("sparse_infill_density", "15%"), 15)
        self.assertIs(to_typed("enable_support", "1"), True)
        self.assertIs(to_typed("enable_support", "0"), False)
        self.assertEqual(to_typed("layer_height", "0.28"), 0.28)
        self.assertEqual(to_typed("support_type", "normal(auto)"), "normal(auto)")

    def test_per_extruder_list_uses_first_element(self):
        self.assertEqual(to_typed("outer_wall_speed", ["200"]), 200)
        self.assertEqual(to_typed("outer_wall_speed", ["200", "150"]), 200)

    def test_already_typed_values_pass_through(self):
        self.assertEqual(to_typed("wall_loops", 3), 3)
        self.assertIs(to_typed("enable_support", True), True)

    def test_whole_floats_become_ints_fractions_stay(self):
        self.assertEqual(to_typed("brim_width", "5"), 5)
        self.assertIsInstance(to_typed("brim_width", "5.0"), int)
        self.assertEqual(to_typed("elefant_foot_compensation", "0.075"), 0.075)

    def test_unknown_key_and_garbage_are_returned_unchanged(self):
        self.assertEqual(to_typed("not_a_real_key", "abc"), "abc")
        self.assertEqual(to_typed("wall_loops", "lots"), "lots")
        self.assertIsNone(to_typed("wall_loops", None))


class TestToBambuString(unittest.TestCase):
    def test_formats_match_what_bambu_studio_stores(self):
        self.assertEqual(to_bambu_string("wall_loops", 4), "4")
        self.assertEqual(to_bambu_string("sparse_infill_density", 20), "20%")
        self.assertEqual(to_bambu_string("enable_support", True), "1")
        self.assertEqual(to_bambu_string("enable_support", False), "0")
        self.assertEqual(to_bambu_string("layer_height", 0.2), "0.2")
        self.assertEqual(to_bambu_string("layer_height", 0.16), "0.16")
        self.assertEqual(to_bambu_string("brim_width", 5.0), "5")
        self.assertEqual(to_bambu_string("support_type", "tree(auto)"), "tree(auto)")

    def test_round_trip_of_every_registry_key_in_the_real_style_fixture(self):
        for key in SETTINGS:
            if key not in REAL_STYLE_SETTINGS:
                continue
            raw = REAL_STYLE_SETTINGS[key]
            scalar = raw[0] if isinstance(raw, list) else raw
            self.assertEqual(to_bambu_string(key, to_typed(key, raw)), scalar, key)

    def test_wrong_type_raises(self):
        with self.assertRaises(SettingValueError):
            to_bambu_string("wall_loops", "lots")
        with self.assertRaises(SettingValueError):
            to_bambu_string("enable_support", "maybe")


class TestValidate(unittest.TestCase):
    def test_accepts_sane_values_and_returns_them_typed(self):
        self.assertEqual(validate("wall_loops", "4"), 4)
        self.assertEqual(validate("support_top_z_distance", 0.25), 0.25)
        self.assertEqual(validate("brim_type", "outer_only"), "outer_only")

    def test_rejects_keys_outside_the_registry(self):
        for key in ("machine_start_gcode", "nozzle_temperature", "printable_height", "made_up"):
            with self.assertRaises(SettingValueError, msg=key):
                validate(key, "1")

    def test_rejects_out_of_range_and_bad_choices(self):
        with self.assertRaises(SettingValueError):
            validate("wall_loops", 500)
        with self.assertRaises(SettingValueError):
            validate("layer_height", 5)
        with self.assertRaises(SettingValueError):
            validate("wall_loops", 2.5)
        with self.assertRaises(SettingValueError):
            validate("brim_type", "enormous")

    def test_no_gcode_or_machine_keys_in_the_registry(self):
        # The registry is the allowlist behind "no arbitrary G-code, no
        # safety-limit changes"; keep it that way.
        for key in SETTINGS:
            self.assertNotIn("gcode", key)
            self.assertFalse(key.startswith("machine_"), key)
            self.assertNotIn("temperature", key)


class TestValuesEqual(unittest.TestCase):
    def test_same_value_in_different_representations(self):
        self.assertTrue(values_equal("wall_loops", "3", 3))
        self.assertTrue(values_equal("sparse_infill_density", "15%", 15))
        self.assertTrue(values_equal("outer_wall_speed", ["200"], 200))
        self.assertTrue(values_equal("enable_support", "1", True))
        self.assertTrue(values_equal("layer_height", "0.20", 0.2))

    def test_different_values(self):
        self.assertFalse(values_equal("wall_loops", "2", 3))
        self.assertFalse(values_equal("enable_support", "0", True))
        self.assertFalse(values_equal("support_type", "tree(auto)", "normal(auto)"))


class TestOverlay(unittest.TestCase):
    def test_bambu_style_base_gets_bambu_style_values(self):
        merged = overlay(
            REAL_STYLE_SETTINGS,
            {"wall_loops": 4, "sparse_infill_density": 20, "outer_wall_speed": 120, "enable_support": False},
        )
        self.assertEqual(merged["wall_loops"], "4")
        self.assertEqual(merged["sparse_infill_density"], "20%")
        self.assertEqual(merged["outer_wall_speed"], ["120"])  # list shape kept
        self.assertEqual(merged["enable_support"], "0")

    def test_key_missing_from_a_bambu_style_base_is_written_as_a_string(self):
        self.assertNotIn("support_interface_spacing", REAL_STYLE_SETTINGS)
        merged = overlay(REAL_STYLE_SETTINGS, {"support_interface_spacing": 0.5})
        self.assertEqual(merged["support_interface_spacing"], "0.5")

    def test_plain_typed_base_stays_typed(self):
        merged = overlay({"wall_loops": 2, "layer_height": 0.2}, {"wall_loops": 4})
        self.assertEqual(merged, {"wall_loops": 4, "layer_height": 0.2})

    def test_base_is_not_mutated_and_other_keys_are_untouched(self):
        before = dict(REAL_STYLE_SETTINGS)
        merged = overlay(REAL_STYLE_SETTINGS, {"wall_loops": 4})
        self.assertEqual(REAL_STYLE_SETTINGS, before)
        for key, value in before.items():
            if key != "wall_loops":
                self.assertEqual(merged[key], value)

    def test_looks_like_bambu_config(self):
        self.assertTrue(looks_like_bambu_config(REAL_STYLE_SETTINGS))
        self.assertFalse(looks_like_bambu_config({"wall_loops": 2, "layer_height": 0.2}))
        self.assertFalse(looks_like_bambu_config({}))

    def test_typed_settings_only_returns_keys_present(self):
        typed = typed_settings(REAL_STYLE_SETTINGS)
        self.assertEqual(typed["wall_loops"], 2)
        self.assertNotIn("machine_start_gcode", typed)
        self.assertNotIn("support_interface_spacing", typed)



class TestValidateStrictness(unittest.TestCase):
    def test_minimum_is_enforced_as_well_as_maximum(self):
        with self.assertRaises(SettingValueError):
            validate("wall_loops", 0)
        with self.assertRaises(SettingValueError):
            validate("layer_height", 0.01)
        with self.assertRaises(SettingValueError):
            validate("support_threshold_angle", -1)
        self.assertEqual(validate("wall_loops", 1), 1)

    def test_true_is_not_a_number(self):
        with self.assertRaises(SettingValueError):
            validate("wall_loops", True)
        with self.assertRaises(SettingValueError):
            validate("layer_height", False)

    def test_non_finite_numbers_are_rejected(self):
        for bad in (float("nan"), float("inf"), "1e999", "nan"):
            with self.assertRaises(SettingValueError, msg=repr(bad)):
                validate("layer_height", bad)

    def test_containers_are_rejected(self):
        for bad in ({"a": 1}, ["1", "2"], [], b"4"):
            with self.assertRaises(SettingValueError, msg=repr(bad)):
                validate("wall_loops", bad)
        self.assertEqual(validate("outer_wall_speed", ["120"]), 120)  # Bambu's own one-element form

    def test_option_keys_without_a_pinned_list_still_need_an_option_shaped_string(self):
        self.assertEqual(validate("sparse_infill_pattern", "gyroid"), "gyroid")
        self.assertEqual(validate("support_style", "tree_hybrid"), "tree_hybrid")
        for bad in ("banana; anything", "", "a" * 60, "../x", 12345, {"a": 1}, True):
            with self.assertRaises(SettingValueError, msg=repr(bad)):
                validate("support_style", bad)

    def test_huge_number_in_a_file_is_read_without_raising(self):
        # to_typed is used to READ whatever a file contains; it never raises.
        self.assertEqual(to_typed("wall_loops", "1e999"), "1e999")


if __name__ == "__main__":
    unittest.main()
