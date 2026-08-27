import unittest

from scripts.update import (
    merge_block,
    normalize_group,
    replace_selected_groups,
    sanitize_extinf,
)


class DynamicMergeTests(unittest.TestCase):
    def test_routes_every_matched_stream_and_http_license_through_worker(self):
        target = ['#EXTINF:-1 group-title="G",N', "https://old.test/live"]
        source = [
            '#EXTINF:-1 group-title="G",N',
            "#KODIPROP:inputstream.adaptive.license_key=https://key.test/current",
            "https://stream.test/current",
        ]

        merged = merge_block(target, source)

        self.assertIn("kind=license", merged[-2])
        self.assertIn("/channel?group=G&name=N", merged[-1])

    def test_preserves_inline_clearkey_values(self):
        target = ['#EXTINF:-1 group-title="G",N', "https://old.test/live"]
        source = [
            '#EXTINF:-1 group-title="G",N',
            "#KODIPROP:inputstream.adaptive.license_key=kid:key",
            "https://stream.test/current",
        ]

        merged = merge_block(target, source)

        self.assertEqual(merged[-2], source[-2])

    def test_extinf_keeps_only_id_group_and_channel_name(self):
        original = '#EXTINF:-1 tvg-id="sctv9" type="stream" group-title="SCTV" tvg-logo="https://img.test/9.png" catchup="append", SCTV9 '
        self.assertEqual(
            sanitize_extinf(original),
            '#EXTINF:-1 tvg-id="sctv9" group-title="SCTV",SCTV9',
        )

    def test_replaces_all_vtvcab_and_htv_but_keeps_other_blocks_exactly(self):
        untouched = [
            '#EXTINF:-1 tvg-logo="keep.png" group-title="SCTV",Keep Me',
            "https://keep.test/live",
        ]
        target = [
            ['#EXTINF:-1 group-title="VTVcab",Old Cab', "https://old/cab"],
            untouched,
            ['#EXTINF:-1 group-title="HTV",Old HTV', "https://old/htv"],
        ]
        source_groups = {
            normalize_group("VTVcab"): [
                ['#EXTINF:-1 tvg-id="cab1" group-title="VTV CAB",Cab 1', "http://new/cab1"],
                ['#EXTINF:-1 tvg-id="cab2" group-title="VTVcab",Cab 2', "http://new/cab2"],
            ],
            normalize_group("HTV"): [
                ['#EXTINF:-1 tvg-id="htv1" group-title="HTV",HTV 1', "http://new/htv1"],
            ],
        }

        result = replace_selected_groups(target, source_groups)

        self.assertEqual(result[2], untouched)
        self.assertEqual(len(result), 4)
        self.assertIn("Cab 1", result[0][0])
        self.assertIn("Cab 2", result[1][0])
        self.assertIn("HTV 1", result[3][0])
        self.assertTrue(result[0][-1].startswith("https://vietmitv-stream."))


if __name__ == "__main__":
    unittest.main()
