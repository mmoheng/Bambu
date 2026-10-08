import unittest
from unittest import mock

from bambu_companion.optimizer import optimize
from bambu_companion.schemas import (
    AnalysisResult,
    BedFitResult,
    BoundingBox,
    BridgeSummary,
    OrientationCandidate,
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
    def test_thin_spot_never_lowers_the_wall_count(self):
        # This rule used to drop a strength-goal part to ONE wall because
        # its thinnest sampled spot was 0.5 mm. Real models report
        # thinnest spots of 0.01 mm at every chamfer tip; Bambu Studio
        # already prints fewer loops where they don't fit.
        for thinnest in (0.01, 0.5, 0.8):
            analysis = make_analysis(min_thickness_mm=thinnest)
            result = optimize(analysis, make_context(PrintGoal.STRENGTH))
            walls = next(c for c in result.changes if c.key == "wall_loops")
            self.assertEqual(walls.recommended_value, 4, thinnest)

    def test_thin_real_wall_raises_loops_so_it_prints_solid(self):
        # A 2.0 mm rail needs 3 loops per side at a 0.42 mm line to be
        # all perimeter; fast-print's baseline of 2 would leave a core.
        analysis = make_analysis(min_thickness_mm=2.0)
        result = optimize(analysis, make_context(PrintGoal.FAST_PRINT))
        walls = next(c for c in result.changes if c.key == "wall_loops")
        self.assertEqual(walls.recommended_value, 3)
        self.assertIn("2.00 mm", walls.reason)
        self.assertIn("crack", walls.reason)

    def test_thick_walls_leave_the_goal_baseline_alone(self):
        analysis = make_analysis(min_thickness_mm=5.0)
        result = optimize(analysis, make_context(PrintGoal.FAST_PRINT))
        walls = next(c for c in result.changes if c.key == "wall_loops")
        self.assertEqual(walls.recommended_value, 2)

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


class TestRealProfileValues(unittest.TestCase):
    """Current settings as a real Bambu Studio file stores them:
    strings, "15%", "1"/"0", one-element lists."""

    REAL = {
        "wall_loops": "3",
        "layer_height": "0.2",
        "outer_wall_speed": ["180"],
        "top_shell_layers": "4",
        "bottom_shell_layers": "4",
        "sparse_infill_density": "15%",
        "sparse_infill_pattern": "grid",
    }

    def test_values_that_already_match_are_not_reported_as_changes(self):
        result = optimize(make_analysis(), make_context(PrintGoal.BALANCED, current_settings=self.REAL))
        self.assertEqual(result.changes, [])

    def test_current_value_is_shown_as_a_plain_value(self):
        current = {**self.REAL, "wall_loops": "2", "sparse_infill_density": "10%"}
        result = optimize(make_analysis(), make_context(PrintGoal.BALANCED, current_settings=current))
        by_key = {c.key: c for c in result.changes}
        self.assertEqual(by_key["wall_loops"].current_value, 2)
        self.assertEqual(by_key["sparse_infill_density"].current_value, 10)
        self.assertEqual(by_key["sparse_infill_density"].recommended_value, 15)


def overhang_analysis(*, largest, total=None, regions=1, flat_span=0.0, flat_area=0.0, worst=0.0, **kw):
    """An analysis whose overhangs have region sizes computed, as the
    real analyzer produces."""
    base = make_analysis(**kw)
    base.overhangs = OverhangSummary(
        threshold_deg=45.0,
        overhang_area_mm2=total if total is not None else largest,
        overhang_face_count=10 * regions,
        worst_face_tilt_deg=worst,
        island_count=regions,
        largest_island_area_mm2=largest,
    )
    base.bridges = BridgeSummary(
        bridge_face_count=4 if flat_area else 0, bridge_area_mm2=flat_area, longest_span_mm=flat_span
    )
    return base


class TestSupportDecisions(unittest.TestCase):
    # Numbers below are from real deck-box models measured on 2026-10-07.

    def test_one_large_ceiling_needs_support(self):
        # Lid with a 110 x 73 mm pocket roof: failed in real life unsupported.
        analysis = overhang_analysis(largest=7006, total=7159, regions=34, flat_span=133, flat_area=7006)
        result = optimize(analysis, make_context(PrintGoal.BALANCED))
        by_key = {c.key: c for c in result.changes}
        self.assertIs(by_key["enable_support"].recommended_value, True)
        self.assertIn("7006 mm^2", by_key["enable_support"].reason)
        self.assertIn("133 mm across", by_key["enable_support"].reason)
        self.assertEqual(by_key["support_interface_top_layers"].recommended_value, 3)

    def test_many_small_overhangs_do_not_get_support(self):
        # Box body with forty drip undersides, largest 45 mm^2: prints clean without.
        analysis = overhang_analysis(largest=45, total=267, regions=40, flat_span=3.3, flat_area=27)
        result = optimize(analysis, make_context(PrintGoal.BALANCED))
        keys = {c.key for c in result.changes}
        self.assertFalse(keys & {"enable_support", "support_threshold_angle", "support_top_z_distance"})
        note = next(n for n in result.uncertainties if "small overhanging region" in n)
        self.assertIn("40 small", note)
        self.assertIn("largest 45 mm^2", note)

    def test_small_overhangs_with_support_already_on_suggests_turning_it_off(self):
        analysis = overhang_analysis(largest=45, total=267, regions=40, flat_span=3.3, flat_area=27)
        ctx = make_context(PrintGoal.BALANCED, current_settings={"enable_support": "1"})
        result = optimize(analysis, ctx)
        self.assertNotIn("enable_support", {c.key for c in result.changes})  # never auto-disabled
        self.assertTrue(any("currently ON" in n for n in result.uncertainties))

    def test_short_covered_pockets_bridge_without_support(self):
        # Six 52 mm^2 pockets, 11.6 mm across.
        analysis = overhang_analysis(largest=52, total=313, regions=6, flat_span=11.6, flat_area=313)
        result = optimize(analysis, make_context(PrintGoal.BALANCED))
        self.assertNotIn("enable_support", {c.key for c in result.changes})

    def test_petg_gets_a_looser_support_gap_than_pla(self):
        analysis = overhang_analysis(largest=842, flat_span=124, flat_area=615)
        gap = {}
        for material in ("PLA", "PETG"):
            result = optimize(analysis, make_context(PrintGoal.BALANCED, material=material))
            gap[material] = next(c for c in result.changes if c.key == "support_top_z_distance")
        self.assertEqual(gap["PLA"].recommended_value, 0.2)
        self.assertEqual(gap["PETG"].recommended_value, 0.25)
        self.assertIn("PETG welds", gap["PETG"].reason)

    def test_unknown_material_gets_a_note_instead_of_a_guessed_gap(self):
        analysis = overhang_analysis(largest=842, flat_span=124, flat_area=615)
        result = optimize(analysis, make_context(PrintGoal.BALANCED, material="TPU"))
        self.assertNotIn("support_top_z_distance", {c.key for c in result.changes})
        self.assertTrue(any("No tested support-gap value for material 'TPU'" in n for n in result.uncertainties))

    def test_working_threshold_is_not_lowered(self):
        # Current 30 deg already catches a 0 deg ceiling; lowering it to
        # 10 could strip support from steeper regions.
        analysis = overhang_analysis(largest=7006, flat_span=133, flat_area=7006, worst=0.0)
        ctx = make_context(PrintGoal.BALANCED, current_settings={"support_threshold_angle": "30"})
        self.assertNotIn("support_threshold_angle", {c.key for c in optimize(analysis, ctx).changes})

    def test_threshold_too_low_to_catch_the_overhang_is_raised(self):
        analysis = overhang_analysis(largest=500, worst=25.0)
        ctx = make_context(PrintGoal.BALANCED, current_settings={"support_threshold_angle": "20"})
        change = next(c for c in optimize(analysis, ctx).changes if c.key == "support_threshold_angle")
        self.assertEqual(change.recommended_value, 35)

    def test_tree_support_under_a_flat_ceiling_is_switched_to_normal(self):
        analysis = overhang_analysis(largest=7006, flat_span=133, flat_area=7006)
        ctx = make_context(PrintGoal.BALANCED, current_settings={"support_type": "tree(auto)"})
        change = next(c for c in optimize(analysis, ctx).changes if c.key == "support_type")
        self.assertEqual(change.recommended_value, "normal(auto)")
        # A manual mode is the user's deliberate choice and is left alone.
        ctx = make_context(PrintGoal.BALANCED, current_settings={"support_type": "tree(manual)"})
        self.assertNotIn("support_type", {c.key for c in optimize(analysis, ctx).changes})

    def test_enough_interface_layers_are_left_alone(self):
        analysis = overhang_analysis(largest=7006, flat_span=133, flat_area=7006)
        ctx = make_context(PrintGoal.BALANCED, current_settings={"support_interface_top_layers": "4"})
        self.assertNotIn("support_interface_top_layers", {c.key for c in optimize(analysis, ctx).changes})

    def test_build_plate_only_is_flagged_when_support_is_needed(self):
        analysis = overhang_analysis(largest=7006, flat_span=133, flat_area=7006)
        ctx = make_context(PrintGoal.BALANCED, current_settings={"support_on_build_plate_only": "1"})
        self.assertTrue(any("build plate only" in n for n in optimize(analysis, ctx).uncertainties))


class TestBrim(unittest.TestCase):
    LOW_CONTACT = dict(size=(100, 100, 50), bed_contact_area_mm2=700.0)

    def test_never_shrinks_a_wider_brim(self):
        ctx = make_context(PrintGoal.BALANCED, current_settings={"brim_type": "outer_only", "brim_width": "5"})
        self.assertNotIn("brim_width", {c.key for c in optimize(make_analysis(**self.LOW_CONTACT), ctx).changes})

    def test_auto_brim_is_left_to_bambu_studio(self):
        ctx = make_context(PrintGoal.BALANCED, current_settings={"brim_type": "auto_brim", "brim_width": "0"})
        keys = {c.key for c in optimize(make_analysis(**self.LOW_CONTACT), ctx).changes}
        self.assertFalse(keys & {"brim_width", "brim_type"})

    def test_unknown_brim_type_is_set_so_the_width_takes_effect(self):
        # Bambu's default brim type is Auto, which ignores the width. A
        # width on its own would change nothing.
        by_key = {c.key: c for c in optimize(make_analysis(**self.LOW_CONTACT), make_context(PrintGoal.BALANCED)).changes}
        self.assertEqual(by_key["brim_type"].recommended_value, "outer_only")
        self.assertEqual(by_key["brim_width"].recommended_value, 3.0)

    def test_brim_switched_off_is_switched_on(self):
        ctx = make_context(PrintGoal.BALANCED, current_settings={"brim_type": "no_brim", "brim_width": "0"})
        by_key = {c.key: c for c in optimize(make_analysis(**self.LOW_CONTACT), ctx).changes}
        self.assertEqual(by_key["brim_type"].recommended_value, "outer_only")
        self.assertEqual(by_key["brim_width"].recommended_value, 3.0)


class TestUncertainties(unittest.TestCase):
    def test_non_watertight_model_flagged(self):
        analysis = make_analysis(is_watertight=False)
        ctx = make_context(PrintGoal.BALANCED)
        result = optimize(analysis, ctx)
        self.assertTrue(any("watertight" in note.lower() for note in result.uncertainties))

    def test_many_thin_samples_are_flagged_without_claiming_to_know_why(self):
        # Countersunk discs: 50 of 250 samples "thin", thinnest 0.01 mm.
        analysis = make_analysis(thin_sample_count=50, min_thickness_mm=0.01)
        result = optimize(analysis, make_context(PrintGoal.STRENGTH))
        note = next(n for n in result.uncertainties if "sampled surface points" in n)
        self.assertIn("50 of 100", note)
        self.assertIn("can't tell apart", note)

    def test_a_few_thin_samples_are_not_worth_a_note(self):
        analysis = make_analysis(thin_sample_count=2, min_thickness_mm=0.5)
        result = optimize(analysis, make_context(PrintGoal.STRENGTH))
        self.assertFalse(any("sampled surface points" in n for n in result.uncertainties))

    def test_visual_goal_leaves_seam_position_to_the_user(self):
        result = optimize(make_analysis(), make_context(PrintGoal.VISUAL_QUALITY))
        self.assertNotIn("seam_position", {c.key for c in result.changes})
        self.assertTrue(any("Seam position" in n for n in result.uncertainties))

    def test_all_recommended_keys_are_verified_so_no_unverified_note(self):
        result = optimize(make_analysis(), make_context(PrintGoal.STRENGTH))
        self.assertFalse(any("weren't individually re-confirmed" in n for n in result.uncertainties))

    def test_an_unverified_key_would_still_be_disclosed(self):
        with mock.patch("bambu_companion.optimizer.optimizer.is_verified", return_value=False):
            result = optimize(make_analysis(), make_context(PrintGoal.STRENGTH))
        self.assertTrue(any("weren't individually re-confirmed" in n for n in result.uncertainties))

    def test_better_orientation_is_mentioned_only_when_it_halves_the_overhang(self):
        def candidate(overhang):
            return OrientationCandidate(
                rotation_matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                description="Rest on facet with area 9000.0 mm^2",
                rest_face_area_mm2=9000.0,
                overhang_area_mm2=overhang,
                bed_contact_area_mm2=9000.0,
                score=0.0,
            )

        analysis = overhang_analysis(largest=7006, total=7159, flat_span=133, flat_area=7006)
        analysis.orientation_candidates = [candidate(300.0)]
        notes = optimize(analysis, make_context(PrintGoal.BALANCED)).uncertainties
        self.assertTrue(any("different resting face would cut that to 300 mm^2" in n for n in notes))

        analysis.orientation_candidates = [candidate(6000.0)]
        notes = optimize(analysis, make_context(PrintGoal.BALANCED)).uncertainties
        self.assertFalse(any("different resting face" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
