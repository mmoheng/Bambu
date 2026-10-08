import tempfile
import unittest
from pathlib import Path

from bambu_companion.model_analyzer.mesh_io import MeshLoadError, load_mesh

from .mesh_fixtures import cube_soup, write_stl_binary
from .project_fixtures import component_model_parts, write_project_3mf


class TestStlBinary(unittest.TestCase):
    def test_loads_cube_with_correct_counts_and_bbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cube.stl"
            write_stl_binary(cube_soup(10.0), path)
            mesh = load_mesh(path)

            self.assertEqual(mesh.face_count, 12)
            # A cube has 8 unique corners; welding should collapse the
            # 36 raw (12 tri * 3) STL vertices down to 8.
            self.assertEqual(mesh.vertex_count, 8)
            self.assertTrue((mesh.vertices.min(axis=0) == 0).all())
            self.assertTrue((mesh.vertices.max(axis=0) == 10).all())

    def test_ascii_stl_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tri.stl"
            text = (
                "solid test\n"
                "facet normal 0 0 1\n"
                "outer loop\n"
                "vertex 0 0 0\n"
                "vertex 1 0 0\n"
                "vertex 0 1 0\n"
                "endloop\n"
                "endfacet\n"
                "endsolid test\n"
            )
            path.write_text(text)
            mesh = load_mesh(path)
            self.assertEqual(mesh.face_count, 1)
            self.assertEqual(mesh.vertex_count, 3)

    def test_truncated_binary_stl_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.stl"
            write_stl_binary(cube_soup(10.0), path)
            data = path.read_bytes()
            path.write_bytes(data[:100])  # chop it short
            with self.assertRaises(MeshLoadError):
                load_mesh(path)

    def test_unsupported_extension_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.obj"
            path.write_text("not a real obj")
            with self.assertRaises(MeshLoadError):
                load_mesh(path)


class Test3mf(unittest.TestCase):
    def test_loads_minimal_3mf(self):
        import zipfile

        model_xml = """<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/>
          <vertex x="0" y="10" z="0"/>
          <vertex x="0" y="0" z="10"/>
          <vertex x="10" y="0" z="10"/>
          <vertex x="10" y="10" z="10"/>
          <vertex x="0" y="10" z="10"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="2" v3="3"/>
          <triangle v1="4" v2="6" v3="5"/>
          <triangle v1="4" v2="7" v3="6"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1"/>
  </build>
</model>
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cube.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("3D/3dmodel.model", model_xml)
            mesh = load_mesh(path)
            self.assertEqual(mesh.face_count, 4)
            self.assertEqual(mesh.vertex_count, 8)

    def test_bambu_studio_project_layout_with_component_references(self):
        # Bambu Studio keeps the triangles in 3D/Objects/object_N.model
        # and only references them from 3D/3dmodel.model. Before this was
        # supported, every real project file loaded as "no usable mesh
        # geometry".
        with tempfile.TemporaryDirectory() as tmp:
            path = write_project_3mf(Path(tmp) / "project.3mf")
            mesh = load_mesh(path)
            self.assertEqual(mesh.face_count, 12)
            self.assertEqual(mesh.vertex_count, 8)
            # 10 mm cube, moved by the build item's transform to (100, 120, 0).
            self.assertEqual(mesh.vertices.min(axis=0).tolist(), [100.0, 120.0, 0.0])
            self.assertEqual(mesh.vertices.max(axis=0).tolist(), [110.0, 130.0, 10.0])

    def test_component_transform_is_applied_before_the_build_transform(self):
        import zipfile

        parts = component_model_parts(translate=(100.0, 0.0, 0.0))
        parts["3D/3dmodel.model"] = parts["3D/3dmodel.model"].replace(
            'objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"',
            'objectid="1" transform="2 0 0 0 2 0 0 0 2 0 0 5"',  # scale x2, lift 5
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scaled.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                for name, xml in parts.items():
                    zf.writestr(name, xml)
            mesh = load_mesh(path)
            self.assertEqual(mesh.vertices.min(axis=0).tolist(), [100.0, 0.0, 5.0])
            self.assertEqual(mesh.vertices.max(axis=0).tolist(), [120.0, 20.0, 25.0])

    def test_objects_marked_not_printable_are_skipped(self):
        import zipfile

        parts = component_model_parts()
        parts["3D/3dmodel.model"] = parts["3D/3dmodel.model"].replace('printable="1"', 'printable="0"')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parked.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                for name, xml in parts.items():
                    zf.writestr(name, xml)
            with self.assertRaises(MeshLoadError):
                load_mesh(path)

    def test_reference_to_a_missing_part_raises_a_clear_error(self):
        import zipfile

        parts = component_model_parts()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("3D/3dmodel.model", parts["3D/3dmodel.model"])
            with self.assertRaises(MeshLoadError) as ctx:
                load_mesh(path)
            self.assertIn("object_1.model", str(ctx.exception))

    def test_not_a_zip_raises_mesh_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.3mf"
            path.write_text("definitely not a zip")
            with self.assertRaises(MeshLoadError):
                load_mesh(path)

    def test_missing_model_part_raises(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("readme.txt", "nothing here")
            with self.assertRaises(MeshLoadError):
                load_mesh(path)



class TestDamagedFilesGiveReadableErrors(unittest.TestCase):
    def _load(self, model_xml: str):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("3D/3dmodel.model", model_xml)
            return load_mesh(path)

    def _model(self, vertices: str, triangles: str) -> str:
        return (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
            f'<object id="1"><mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh></object>'
            '</resources><build><item objectid="1"/></build></model>'
        )

    def test_vertex_missing_a_coordinate(self):
        xml = self._model('<vertex x="0" y="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>', '<triangle v1="0" v2="1" v3="2"/>')
        with self.assertRaises(MeshLoadError):
            self._load(xml)

    def test_triangle_pointing_at_a_vertex_that_does_not_exist(self):
        xml = self._model('<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>', '<triangle v1="0" v2="1" v3="9"/>')
        with self.assertRaises(MeshLoadError) as ctx:
            self._load(xml)
        self.assertIn("vertex that doesn't exist", str(ctx.exception))

    def test_bad_transform_and_non_numeric_values(self):
        xml = self._model('<vertex x="a" y="0" z="0"/>', '<triangle v1="0" v2="0" v3="0"/>')
        with self.assertRaises(MeshLoadError):
            self._load(xml)

    def test_stl_with_no_triangles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.stl"
            path.write_bytes(b"\0" * 80 + (0).to_bytes(4, "little"))
            with self.assertRaises(MeshLoadError):
                load_mesh(path)

    def test_object_filter_loads_only_the_requested_build_items(self):
        from .project_fixtures import write_two_plate_project

        with tempfile.TemporaryDirectory() as tmp:
            path = write_two_plate_project(Path(tmp) / "two.3mf")
            everything = load_mesh(path)
            self.assertEqual(everything.face_count, 24)
            self.assertGreater(float(everything.vertices[:, 0].max() - everything.vertices[:, 0].min()), 300)
            right = load_mesh(path, object_ids=["4"])
            self.assertEqual(right.face_count, 12)
            self.assertEqual(right.vertices.min(axis=0).tolist(), [400.0, 120.0, 0.0])
            with self.assertRaises(MeshLoadError):
                load_mesh(path, object_ids=["99"])


if __name__ == "__main__":
    unittest.main()
