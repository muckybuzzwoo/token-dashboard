import tempfile
import unittest
from pathlib import Path

from token_dashboard.jsonutil import read_json_dict


class ReadJsonDictTests(unittest.TestCase):
    def _tmp(self, content: str) -> Path:
        p = Path(tempfile.mkdtemp()) / "f.json"
        p.write_text(content, encoding="utf-8")
        return p

    def test_reads_object(self):
        self.assertEqual(read_json_dict(self._tmp('{"a": 1}')), {"a": 1})

    def test_non_object_json_returns_empty(self):
        # A top-level array parses fine but callers expect a dict to .get() on.
        self.assertEqual(read_json_dict(self._tmp("[1, 2, 3]")), {})

    def test_malformed_returns_empty(self):
        self.assertEqual(read_json_dict(self._tmp("{not json")), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(read_json_dict(Path(tempfile.mkdtemp()) / "nope.json"), {})


if __name__ == "__main__":
    unittest.main()
