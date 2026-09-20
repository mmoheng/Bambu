import unittest

import numpy as np

from bambu_companion.model_analyzer import geometry as geo
from bambu_companion.model_analyzer.mesh_io import Mesh

from .mesh_fixtures import (
    bridge_soup,
    cube_soup,
    two_separate_cubes_soup,
)


def _mesh_from_soup(soup: np.ndarray) -> Mesh:
    """Weld a raw triangle soup into a Mesh without going through disk
    I/O — used where a test only cares about geometry math, not the STL
    loader itself (that's covered separately in test_mesh_io.py).
    """
    from bambu_companion.model_analyzer.mesh_io import _weld_vertices

    raw = soup.reshape(-1, 3)
    faces = np.arange(raw.shape[0]).reshape(-1, 3)
    return _weld_vertices(raw, faces)


class TestFaceNormalsAndAreas(unittest.TestCase):
    def test_cube_face_areas_and_unit_normals(self):
        mesh = _mesh_from_soup(cube_soup(10.0))
        normals, areas = geo.face_normals_and_areas(mesh)
        # 12 triangles, each half of a 10x10 face = 50 mm^2
        np.testing.assert_allclose(areas, 50.0, atol=1e-6)
        norms = np.linalg.norm(normals, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)
        # total surface area of a 10mm cube = 6 * 100 = 600
        self.assertAlmostEqual(float(areas.sum()), 600.0, places=4)

    def test_face_tilt_from_horizontal(self):
        mesh = _mesh_from_soup(cube_soup(10.0))
        normals, _ = geo.face_normals_and_areas(mesh)
        tilts = geo.face_tilt_from_horizontal_deg(normals)
        # top/bottom faces (normal +-Z) -> 0 deg tilt from horizontal
        # side faces (normal +-X/+-Y) -> 90 deg tilt from horizontal
        vertical_normal_mask = np.abs(normals[:, 2]) > 0.99
        horizontal_normal_mask = np.abs(normals[:, 2]) < 0.01
        np.testing.assert_allclose(tilts[vertical_normal_mask], 0.0, atol=1e-4)
        np.testing.assert_allclose(tilts[horizontal_normal_mask], 90.0, atol=1e-4)


class TestEdgeTopology(unittest.TestCase):
    def test_cube_is_watertight(self):
        mesh = _mesh_from_soup(cube_soup(10.0))
        topo = geo.edge_topology(mesh)
        self.assertTrue(topo["is_watertight"])
        self.assertEqual(topo["boundary_edge_count"], 0)
        self.assertEqual(topo["nonmanifold_edge_count"], 0)

    def test_open_triangle_has_boundary_edges(self):
        raw = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float64)
        mesh = Mesh(vertices=raw, faces=np.array([[0, 1, 2]]))
        topo = geo.edge_topology(mesh)
        self.assertFalse(topo["is_watertight"])
        self.assertEqual(topo["boundary_edge_count"], 3)


class TestConnectedComponents(unittest.TestCase):
    def test_single_cube_is_one_component(self):
        mesh = _mesh_from_soup(cube_soup(10.0))
        comps = geo.connected_components(mesh)
        self.assertEqual(len(comps), 1)
        self.assertEqual(sum(len(c) for c in comps), mesh.face_count)

    def test_two_separate_cubes_are_two_components(self):
        mesh = _mesh_from_soup(two_separate_cubes_soup())
        comps = geo.connected_components(mesh)
        self.assertEqual(len(comps), 2)

    def test_bridge_fixture_is_one_connected_component(self):
        # Pillars + beam weld together where the beam ends sit exactly on
        # the pillar tops, so despite being 3 separate boxes in the soup,
        # topologically it should be one connected body.
        mesh = _mesh_from_soup(bridge_soup())
        comps = geo.connected_components(mesh)
        self.assertEqual(len(comps), 1)


class TestRayMeshIntersection(unittest.TestCase):
    def test_ray_through_cube_hits_far_wall(self):
        mesh = _mesh_from_soup(cube_soup(10.0))
        origin = np.array([[5.0, 5.0, 5.0]])  # center of the cube
        direction = np.array([[1.0, 0.0, 0.0]])
        dist = geo.ray_mesh_first_hit_distance(origin, direction, mesh)
        # From the center to the +X face (x=10) is 5mm away.
        self.assertAlmostEqual(float(dist[0]), 5.0, places=3)

    def test_ray_missing_mesh_returns_inf(self):
        mesh = _mesh_from_soup(cube_soup(10.0))
        origin = np.array([[500.0, 500.0, 500.0]])
        direction = np.array([[1.0, 0.0, 0.0]])
        dist = geo.ray_mesh_first_hit_distance(origin, direction, mesh)
        self.assertTrue(np.isinf(dist[0]))


if __name__ == "__main__":
    unittest.main()
