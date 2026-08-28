import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "update.py"
SPEC = importlib.util.spec_from_file_location("playlist_update", SCRIPT)
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


class UpdatePlaylistTests(unittest.TestCase):
    def test_match_requires_normalized_group_and_current_name(self):
        source = (
            '#EXTM3U\n'
            '#EXTINF:-1 group-title="SCTV" tvg-id="upstream",SCTV Phim Tổng Hợp\n'
            'https://new.example/stream.m3u8\n'
        )
        _, source_map, _, _ = update.build_source_map(source)

        matching_key = (
            update.normalize_group("sctv"),
            update.normalize_name("SCTV Phim Tong Hop"),
        )
        wrong_group_key = (
            update.normalize_group("Khác"),
            update.normalize_name("SCTV Phim Tong Hop"),
        )

        self.assertIn(matching_key, source_map)
        self.assertNotIn(wrong_group_key, source_map)

    def test_update_keeps_exact_extinf_and_replaces_only_body(self):
        original_extinf = (
            '#EXTINF:-1 tvg-id="voice-id" tvg-logo="custom.png" '
            'group-title="SCTV",Tên gọi bằng voice'
        )
        target = f'#EXTM3U\n{original_extinf}\nhttps://old.example/live\n'
        source_block = [
            '#EXTINF:-1 tvg-id="other" group-title="SCTV",Ten goi bang voice',
            '#EXTVLCOPT:http-user-agent=New UA',
            'https://new.example/live',
        ]
        key = (
            update.normalize_group("SCTV"),
            update.normalize_name("Tên gọi bằng voice"),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m3u.m3u"
            path.write_text(target, encoding="utf-8")
            update.update_target_file(str(path), {key: {"block": source_block}})
            result = path.read_text(encoding="utf-8")

        self.assertEqual(update.get_extinf_lines(result), [original_extinf])
        self.assertIn("#EXTVLCOPT:http-user-agent=New UA", result)
        self.assertIn("https://new.example/live", result)
        self.assertNotIn("https://old.example/live", result)

    def test_fail_safe_aborts_before_write_if_extinf_changes(self):
        extinf = '#EXTINF:-1 group-title="SCTV",Tên voice'
        target = f'#EXTM3U\n{extinf}\nhttps://old.example/live\n'
        key = (
            update.normalize_group("SCTV"),
            update.normalize_name("Tên voice"),
        )
        source = {key: {"block": [extinf, "https://new.example/live"]}}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m3u.m3u"
            path.write_text(target, encoding="utf-8")

            real_get_extinf_lines = update.get_extinf_lines
            calls = 0

            def changed_on_output(text):
                nonlocal calls
                calls += 1
                lines = real_get_extinf_lines(text)
                return lines if calls == 1 else [lines[0] + " changed"]

            with patch.object(update, "get_extinf_lines", changed_on_output):
                with self.assertRaisesRegex(RuntimeError, "Fail-safe"):
                    update.update_target_file(str(path), source)

            self.assertEqual(path.read_text(encoding="utf-8"), target)


if __name__ == "__main__":
    unittest.main()
