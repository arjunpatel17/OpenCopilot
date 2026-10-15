"""Tool dependencies and builds stay out of blob sync and generated-file lists."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import blob_storage, telegram_bot


class SyncablePathTests(unittest.TestCase):
    def test_tool_dependencies_and_builds_are_not_synced(self):
        for path in (
            "tools/childrens-books/node_modules/ajv/package.json",
            "tools/childrens-books/dist/books/lion.pdf",
        ):
            self.assertFalse(blob_storage.is_syncable_path(path), path)

    def test_book_sources_and_collected_books_are_synced(self):
        for path in (
            "tools/childrens-books/books/lion/book.json",
            "output/childrens-books/lion/lion.pdf",
            "output/dist/report.md",
        ):
            self.assertTrue(blob_storage.is_syncable_path(path), path)


class TelegramSnapshotTests(unittest.TestCase):
    def test_generated_file_list_skips_tool_dependencies_and_builds(self):
        with tempfile.TemporaryDirectory() as root:
            for path in (
                "output/childrens-books/lion/lion.pdf",
                "tools/childrens-books/node_modules/ajv/package.json",
                "tools/childrens-books/dist/books/lion.pdf",
            ):
                file = Path(root, path)
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("x")
            with patch.object(telegram_bot.settings, "workspace_dir", root):
                self.assertEqual(
                    telegram_bot._snapshot_workspace_files(),
                    {"output/childrens-books/lion/lion.pdf"},
                )


if __name__ == "__main__":
    unittest.main()
