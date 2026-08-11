from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate_publication.py"
SPEC = importlib.util.spec_from_file_location("foundation_pose_validate_publication", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class PublicationManifestHashTests(unittest.TestCase):
    def test_text_manifest_hash_is_independent_of_windows_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lf_path = root / "lf.md"
            crlf_path = root / "crlf.md"
            lf_path.write_bytes(b"one\ntwo\n")
            crlf_path.write_bytes(b"one\r\ntwo\r\n")

            self.assertNotEqual(VALIDATOR.sha256(lf_path), VALIDATOR.sha256(crlf_path))
            self.assertEqual(
                VALIDATOR.manifest_sha256(lf_path),
                VALIDATOR.manifest_sha256(crlf_path),
            )

    def test_binary_manifest_hash_remains_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.png"
            path.write_bytes(b"binary\r\npayload")

            self.assertEqual(
                VALIDATOR.manifest_sha256(path),
                VALIDATOR.sha256(path),
            )


if __name__ == "__main__":
    unittest.main()
