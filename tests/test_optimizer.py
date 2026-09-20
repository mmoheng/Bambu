import unittest

from bambu_companion.optimizer import optimize
from bambu_companion.schemas import (
    AnalysisResult,
    BedFitResult,
    BoundingBox,
    BridgeSummary,
    OverhangSummary,
    PrintContext,
    PrintGoal,
    ThinWallSummary,
)


def make_analysis(
    *,
    size=(20.0, 20.0, 20.0),
    bed_contact_area_mm2=400.0,
    overhang_face_count=0,
    overhang_area_mm2=0.0,
    worst_face_tilt_deg=None,
    bridge_face_count=0,
    thin_sample_count=0,
    min_thickness_mm=5.0,
    fit_sensitive_features=None,
    is_watertight=True,
) -> AnalysisResult:
    return AnalysisResult(
        source_file="test.stl",
        bounding_box=BoundingBox(min=(0, 0, 0), max=size),
        bed_fit=BedFitResult(fits=True, build_volume_mm=(256, 256, 256), model_size_mm=size, margin_mm=(1, 1, 1)),
        bed_contact_area_mm2=bed_contact_area_mm2,
        overhangs=OverhangSummary(
            threshold_deg=45.0,
            overhang_area_mm2=overhang_area_mm2,
            overhang_face_count=overhang_face_count,
            worst_face_tilt_deg=worst_face_tilt_deg,
        ),
        bridges=BridgeSummary(bridge_face_count=bridge_face_count, bridge_area_mm2=0.0, longest_span_mm=0.0),
        thin_walls=ThinWallSummary(
            checked_samples=100, thin_sample_count=thin_sample_count, min_thickness_mm=min_thickness_mm
        ),
        small_features=[],
        fit_sensitive_features=fit_sensitive_features or [],
        orientation_candidates=[],
        warnings=[],
        body_count=1,
        is_watertight=is_watertight,
        face_count=12,
        vertex_count=8,
    )


def make_context(goal: PrintGoal, **overrides) -> PrintContext:
    defaults = dict(
        printer="Bambu Lab A1",
        nozzle_diameter_mm=0.4,
        material="PLA",
        goal=goal,
        current_settings={},
    )
    defaults.update(overrides)
    return PrintContext(**defaults)


class TestGoalBaselines(unittest.TestCase):
    def test_fast_print_uses_fewer_walls_than_strength(self):
        analysis = make_analysis()
        fast = optimize(analysis, make_context(PrintGoal.FAST_PRINT))
        strength = optimize(analysis, make_context(PrintGoal.STRENGTH))

        fast_walls = next(c for c in fast.changes if c.key == "wall_loops")
        strength_walls = next(c for c in strength.changes if c.key == "wall_loops")
        self.assertLess(fast_walls.recommended_value, strength_walls.recommended_value)

    def test_no_change_recommended_when_current_already_matches(self):
        analysis = make_analysis()
        ctx = make_context(PrintGoal.BALANCED, current_settings={"wall_loops": 3})
        result = optimize(analysis, ctx)
        keys = {c.key for c in result.changes}
        self.assertNotIn("wall_loops", keys)  # balanced baseline is 3, already matches

    def test_every_change_has_a_nonempty_reason(self):
        analysis = make_analysis()
        result = optimize(analysis, make_context(PrintGoal.STRENGTH))
        self.assertTrue(result.changes, "expected at least one recommended change")
        for c in result.changes:
            self.assertTrue(c.reason.strip())


class TestGeometryDrivenAdjustments(unittest.TestCase):
    def test_thin_wall_caps_wall_loops_below_goal_baseline(self):
        # Strength goal would normally want 4 loops, but the thinnest
        # sampled region is only 0.5mm — at a 0.4mm nozzle that's 1 loop.
        analysis = make_analysis(min_thickness_mm=0.5)
        ctx = make_context(PrintGoal.STRENGTH, nozzle_diameter_mm=0.4)
        result = optimize(analysis, ctx)
        walls = next(c for c in result.changes if c.key == "wall_loops")
        self.assertEqual(walls.recommended_value, 1)
        self.assertIn("0.50", walls.reason)

    def test_overhangs_trigger_support_recommendation(self):
        analysis = make_analysis(overhang_face_count=12, overhang_area_mm2=80.0, worst_face_tilt_deg=5.0)
        ctx = make_context(PrintGoal.BALANCED)
        result = optimize(analysis, ctx)
        keys = {c.key for c in result.changes}
        self.assertIn("enable_support", keys)
        self.assertIn("support_threshold_angle", keys)

    def test_no_overhangs_means_no_support_recommendation(self):
        analysis = make_analysis(overhang_face_count=0)
        ctx = make_context(PrintGoal.BALANCED)
        result = optimize(analysis, ctx)
        keys = {c.key for c in result.changes}
        self.assertNotIn("enable_support", keys)

    def test_low_bed_contact_triggers_brim(self):
        analysis = make_analysis(size=(10, 10, 50), bed_contact_area_mm2=20.0)
        ctx = make_context(PrintGoal.BALANCED)
        result = optimize(analysis, ctx)
        keys = {c.key for c in result.changes}
        self.assertIn("brim_width", keys)

    def test_dimensional_accuracy_goal_adds_compensation(self):
        analysis = make_analysis()
        ctx = make_context(PrintGoal.DIMENSIONAL_ACCURACY, material="PETG")
        result = optimize(analysis, ctx)
        keys = {c.key for c in result.changes}
        self.assertIn("elefant_foot_compensation", keys)

    def test_non_accuracy_goal_does_not_add_compensation(self):
        analysis = make_analysis()
        ctx = make_context(PrintGoal.FAST_PRINT)
        result = optimize(analysis, ctx)
        keys = {c.key for c in result.changes}
        self.assertNotIn("elefant_foot_compensation", keys)


class TestUncertainties(unittest.TestCase):
    def test_bridges_flagged_as_uncertainty_not_a_setting(self):
        analysis = make_analysis(bridge_face_count=5)
        ctx = make_context(PrintGoal.BALANCED)
        result = optimize(analysis, ctx)
        self.assertTrue(any("bridge" in note.lower() for note in result.uncertainties))

    def test_non_watertight_model_flagged(self):
        analysis = make_analysis(is_watertight=False)
        ctx = make_context(PrintGoal.BALANCED)
        result = optimize(analysis, ctx)
        self.assertTrue(any("watertight" in note.lower() for note in result.uncertainties))

    def test_unverified_keys_are_disclosed(self):
        analysis = make_analysis()
        ctx = make_context(PrintGoal.STRENGTH)
        result = optimize(analysis, ctx)
        # wall_loops is verified but layer_height/outer_wall_speed aren't;
        # the optimizer should say so rather than presenting everything
        # with equal confidence.
        self.assertTrue(any("weren't individually re-confirmed" in note for note in result.uncertainties))


if __name__ == "__main__":
    unittest.main()
