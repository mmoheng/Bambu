import unittest

from bambu_companion.profiles.temp_profile import ApprovalError, apply_changes, diff_from_base
from bambu_companion.schemas import SettingChange


def make_change(key, current, recommended):
    return SettingChange(key=key, label=key, current_value=current, recommended_value=recommended, reason="test")


class TestApplyChanges(unittest.TestCase):
    def test_applies_only_approved_keys(self):
        base = {"wall_loops": 2, "layer_height": 0.2}
        proposed = [make_change("wall_loops", 2, 4), make_change("layer_height", 0.2, 0.12)]
        result = apply_changes(base, proposed, approved_keys={"wall_loops"})

        self.assertEqual(result["wall_loops"], 4)
        self.assertEqual(result["layer_height"], 0.2)  # untouched, not approved

    def test_does_not_mutate_base_settings(self):
        base = {"wall_loops": 2}
        proposed = [make_change("wall_loops", 2, 4)]
        result = apply_changes(base, proposed, approved_keys={"wall_loops"})

        self.assertEqual(base["wall_loops"], 2, "base settings must be left untouched")
        self.assertEqual(result["wall_loops"], 4)

    def test_approving_unproposed_key_raises(self):
        base = {"wall_loops": 2}
        proposed = [make_change("wall_loops", 2, 4)]
        with self.assertRaises(ApprovalError):
            apply_changes(base, proposed, approved_keys={"layer_height"})

    def test_empty_approval_leaves_settings_unchanged(self):
        base = {"wall_loops": 2}
        proposed = [make_change("wall_loops", 2, 4)]
        result = apply_changes(base, proposed, approved_keys=set())
        self.assertEqual(result, base)
        self.assertIsNot(result, base)


class TestDiffFromBase(unittest.TestCase):
    def test_reports_only_differing_keys(self):
        base = {"wall_loops": 2, "layer_height": 0.2}
        temp = {"wall_loops": 4, "layer_height": 0.2}
        diff = diff_from_base(base, temp)
        self.assertEqual(diff, {"wall_loops": (2, 4)})


if __name__ == "__main__":
    unittest.main()
