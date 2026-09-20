import tempfile
import unittest
from pathlib import Path

from bambu_companion.model_analyzer.mesh_io import MeshLoadError, load_mesh

from .mesh_fixtures import cube_soup, write_stl_binary


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

    def test_missing_model_part_raises(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("readme.txt", "nothing here")
            with self.assertRaises(MeshLoadError):
                load_mesh(path)


if __name__ == "__main__":
    unittest.main()
