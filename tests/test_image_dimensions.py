import ast
import tempfile
import unittest
from pathlib import Path


def load_dimension_reader():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    plugin_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatArchivePlugin"
    )
    methods = [
        node for node in plugin_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_positive_int", "_valid_image_dimensions", "_read_image_dimensions"}
    ]
    module = ast.Module(
        body=[ast.ClassDef(
            name="ChatArchivePlugin",
            bases=[],
            keywords=[],
            body=methods,
            decorator_list=[],
        )],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(main_path), "exec"), namespace)
    return namespace["ChatArchivePlugin"]._read_image_dimensions


class ImageDimensionTests(unittest.TestCase):
    def setUp(self):
        self.read_dimensions = load_dimension_reader()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def write_image(self, name, data):
        path = self.base / name
        path.write_bytes(data)
        return str(path)

    def test_reads_png_dimensions(self):
        data = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            + (800).to_bytes(4, "big")
            + (600).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        self.assertEqual(self.read_dimensions(self.write_image("image.png", data)), (800, 600))

    def test_reads_gif_dimensions(self):
        data = b"GIF89a" + (320).to_bytes(2, "little") + (240).to_bytes(2, "little")
        self.assertEqual(self.read_dimensions(self.write_image("image.gif", data)), (320, 240))

    def test_reads_jpeg_dimensions(self):
        data = (
            b"\xff\xd8"
            b"\xff\xe0\x00\x10" + b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xc0\x00\x11\x08"
            + (768).to_bytes(2, "big")
            + (1024).to_bytes(2, "big")
            + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        )
        self.assertEqual(self.read_dimensions(self.write_image("image.jpg", data)), (1024, 768))

    def test_reads_webp_vp8x_dimensions(self):
        width_minus_one = (640 - 1).to_bytes(3, "little")
        height_minus_one = (360 - 1).to_bytes(3, "little")
        data = (
            b"RIFF\x1e\x00\x00\x00WEBPVP8X"
            + (10).to_bytes(4, "little")
            + b"\x00\x00\x00\x00"
            + width_minus_one
            + height_minus_one
        )
        self.assertEqual(self.read_dimensions(self.write_image("image.webp", data)), (640, 360))

    def test_rejects_invalid_dimensions(self):
        data = b"GIF89a" + (0).to_bytes(2, "little") + (240).to_bytes(2, "little")
        self.assertIsNone(self.read_dimensions(self.write_image("bad.gif", data)))


if __name__ == "__main__":
    unittest.main()
