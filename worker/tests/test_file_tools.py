import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import file_tools


class FileToolsTests(unittest.TestCase):
    def make_tree(self, root: Path) -> None:
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "a" / "same.txt").write_text("duplicate", encoding="utf-8")
        (root / "b" / "same-copy.txt").write_text("duplicate", encoding="utf-8")
        (root / "unique.bin").write_bytes(b"unique-data")

    def test_inventory_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            self.make_tree(root)

            inventory = Path(directory) / "inventory.json"
            args = type("Args", (), {"root": str(root), "output": str(inventory)})()
            self.assertEqual(file_tools.inventory(args), 0)
            data = json.loads(inventory.read_text(encoding="utf-8"))
            self.assertEqual(data["file_count"], 3)

            duplicates = Path(directory) / "duplicates.json"
            args = type("Args", (), {"root": str(root), "output": str(duplicates)})()
            self.assertEqual(file_tools.duplicates(args), 0)
            data = json.loads(duplicates.read_text(encoding="utf-8"))
            self.assertEqual(data["groups"], 1)
            self.assertEqual(data["duplicates"][0]["copies"], 2)

    def test_backup_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            root.mkdir()
            self.make_tree(root)
            archive = Path(directory) / "backup.tar.gz"
            metadata = Path(directory) / "backup.json"
            args = type("Args", (), {
                "root": str(root),
                "output": str(archive),
                "metadata": str(metadata),
            })()
            self.assertEqual(file_tools.backup(args), 0)
            self.assertTrue(archive.exists())
            data = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(data["file_count"], 3)
            self.assertEqual(len(data["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
