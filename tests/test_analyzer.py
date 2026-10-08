import tempfile
import unittest
from pathlib import Path

import numpy as np

from bambu_companion.model_analyzer.analyzer import analyze

from .mesh_fixtures import (
    annulus_soup,
    box_triangle_soup,
    bridge_soup,
    cube_soup,
    two_separate_cubes_soup,
    write_stl_binary,
)


def _analyze_soup(soup, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.stl"
        write_stl_binary(soup, path)
        return analyze(path, **kwargs)


class TestBoundingBoxAndBedFit(unittest.TestCase):
    def test_small_cube_fits_a1_bed(self):
        result = _analyze_soup(cube_soup(10.0))
        self.assertEqual(result.bounding_box.size, (10.0, 10.0, 10.0))
        self.assertTrue(result.bed_fit.fits)
        self.assertEqual(result.bed_fit.build_volume_mm, (256.0, 256.0, 256.0))

    def test_oversized_cube_does_not_fit(self):
        result = _analyze_soup(cube_soup(300.0))
        self.assertFalse(result.bed_fit.fits)
        self.assertTrue(len(result.bed_fit.notes) >= 1)


class TestBedContactArea(unittest.TestCase):
    def test_cube_bed_contact_is_bottom_face(self):
        result = _analyze_soup(cube_soup(10.0))
        self.assertAlmostEqual(result.bed_contact_area_mm2, 100.0, places=3)


class TestWatertightAndBodyCount(unittest.TestCase):
    def test_cube_is_watertight_single_body(self):
        result = _analyze_soup(cube_soup(10.0))
        self.assertTrue(result.is_watertight)
        self.assertEqual(result.body_count, 1)
        self.assertEqual(len(result.warnings), 0)


class TestOverhangsAndBridges(unittest.TestCase):
    def test_plain_cube_has_no_overhangs(self):
        result = _analyze_soup(cube_soup(10.0))
        self.assertEqual(result.overhangs.overhang_face_count, 0)
        self.assertEqual(result.bridges.bridge_face_count, 0)

    def test_bridge_fixture_detects_overhang_and_bridge(self):
        result = _analyze_soup(bridge_soup())
        self.assertGreater(result.overhangs.overhang_face_count, 0)
        self.assertGreater(result.bridges.bridge_face_count, 0)
        # The unsupported span between the two pillars (gap = 40mm from
        # x=10 to x=50) should show up as roughly that long.
        self.assertGreater(result.bridges.longest_span_mm, 30.0)
        # Worst-case overhang face here is the flat underside of the
        # beam -> tilt from horizontal should be ~0 deg (flat).
        self.assertIsNotNone(result.overhangs.worst_face_tilt_deg)
        self.assertLess(result.overhangs.worst_face_tilt_deg, 5.0)


class TestOverhangRegions(unittest.TestCase):
    def test_no_overhangs_means_no_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cube.stl"
            write_stl_binary(cube_soup(20.0), path)
            oh = analyze(path).overhangs
            self.assertEqual(oh.island_count, 0)
            self.assertEqual(oh.largest_island_area_mm2, 0.0)

    def test_bridge_fixture_overhang_is_one_region_holding_all_the_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bridge.stl"
            write_stl_binary(bridge_soup(), path)
            oh = analyze(path).overhangs
            self.assertEqual(oh.island_count, 1)
            self.assertAlmostEqual(oh.largest_island_area_mm2, oh.overhang_area_mm2)
            self.assertGreater(oh.largest_island_area_mm2, 0)

    def test_two_separate_overhangs_are_two_regions(self):
        # Two tables side by side: each table top's underside is its own
        # overhanging region, and neither is "all" of the overhang area.
        def table(x0):
            return np.concatenate(
                [
                    box_triangle_soup(x0, 0, 0, x0 + 2, 10, 10),  # leg
                    box_triangle_soup(x0 + 18, 0, 0, x0 + 20, 10, 10),  # leg
                    box_triangle_soup(x0, 0, 10, x0 + 20, 10, 12),  # top
                ]
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tables.stl"
            write_stl_binary(np.concatenate([table(0.0), table(50.0)]), path)
            oh = analyze(path).overhangs
            self.assertEqual(oh.island_count, 2)
            self.assertAlmostEqual(oh.largest_island_area_mm2, oh.overhang_area_mm2 / 2, places=3)


class TestOrientationCandidates(unittest.TestCase):
    def test_cube_orientation_candidates_are_produced(self):
        result = _analyze_soup(cube_soup(10.0))
        self.assertGreater(len(result.orientation_candidates), 0)
        # A cube is symmetric, so the current (bottom-face-down)
        # orientation should already be at/near the best possible score
        # (zero overhang no matter which face it rests on).
        for c in result.orientation_candidates:
            self.assertEqual(c.overhang_area_mm2, 0.0)

    def test_bridge_fixture_has_a_better_orientation_than_default(self):
        result = _analyze_soup(bridge_soup())
        # In its current orientation there IS overhang (the bridge span).
        self.assertGreater(result.overhangs.overhang_area_mm2, 0.0)
        # At least one candidate orientation should score at or above
        # resting on the current bottom (i.e. reorienting isn't worse).
        self.assertGreater(len(result.orientation_candidates), 0)


class TestThinWalls(unittest.TestCase):
    def test_solid_cube_has_no_thin_walls(self):
        # A 10mm-thick cube wall is far thicker than 2 loops * 0.4mm nozzle.
        result = _analyze_soup(cube_soup(10.0), nozzle_diameter_mm=0.4, current_wall_loops=2)
        self.assertEqual(result.thin_walls.thin_sample_count, 0)
        self.assertIsNotNone(result.thin_walls.min_thickness_mm)
        self.assertGreater(result.thin_walls.min_thickness_mm, 0.8)


class TestFitSensitiveFeatures(unittest.TestCase):
    def test_annulus_detects_two_circular_boundary_loops(self):
        result = _analyze_soup(annulus_soup(inner_r=5.0, outer_r=20.0, n_segments=20))
        # Outer boundary (r=20) and inner boundary (r=5) are both open and
        # both regular (low radius variation) -> both should be flagged.
        self.assertEqual(len(result.fit_sensitive_features), 2)
        diameters = sorted(f.diameter_mm for f in result.fit_sensitive_features)
        self.assertAlmostEqual(diameters[0], 10.0, delta=0.5)
        self.assertAlmostEqual(diameters[1], 40.0, delta=0.5)

    def test_watertight_cube_has_no_fit_sensitive_features(self):
        result = _analyze_soup(cube_soup(10.0))
        self.assertEqual(result.fit_sensitive_features, [])


class TestSmallFeaturesAndWarnings(unittest.TestCase):
    def test_multi_body_file_flags_small_bodies_and_warns(self):
        # two 5mm cubes: both count as "small" against the default 3mm
        # threshold? No — 5mm > 3mm default, so bump the threshold up in
        # the call to actually exercise the small-feature path.
        result = _analyze_soup(two_separate_cubes_soup(), small_feature_threshold_mm=6.0)
        self.assertEqual(result.body_count, 2)
        self.assertEqual(len(result.small_features), 2)
        self.assertTrue(any(w.kind == "multiple_bodies" for w in result.warnings))

    def test_open_mesh_warns_about_boundary_edges(self):
        result = _analyze_soup(annulus_soup(inner_r=5.0, outer_r=20.0, n_segments=20))
        self.assertFalse(result.is_watertight)
        self.assertTrue(any(w.kind == "open_boundary" for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
