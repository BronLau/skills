from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "depth_video_pipeline.py"
SPEC = importlib.util.spec_from_file_location("depth_video_pipeline", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载测试目标：{SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DepthPipelineTests(unittest.TestCase):
    def test_parse_frame_rate(self) -> None:
        self.assertEqual(MODULE.parse_frame_rate("30/1"), 30.0)
        self.assertEqual(MODULE.parse_frame_rate("0/0"), 0.0)
        with self.assertRaisesRegex(RuntimeError, "帧率字段无效"):
            MODULE.parse_frame_rate("bad")

    def test_preprocess_returns_model_tensor(self) -> None:
        frame = np.full((64, 32, 3), 127, dtype=np.uint8)
        tensor = MODULE.preprocess(frame)
        self.assertEqual(tensor.shape, (1, 3, 518, 518))
        self.assertEqual(tensor.dtype, np.float32)
        self.assertTrue(np.isfinite(tensor).all())

    def test_segment_times_must_be_positive_and_increasing(self) -> None:
        self.assertEqual(MODULE.parse_segment_times("10, 20.5"), [10.0, 20.5])
        for value in ("", "10,10", "10,5", "-1,5"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    MODULE.parse_segment_times(value)

    def test_load_segment_plan_supports_both_model_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "segment_plan.json"
            for maximum, split_times in ((15, [10]), (30, [20.0])):
                with self.subTest(maximum=maximum):
                    path.write_text(
                        json.dumps(
                            {
                                "segment_max_seconds": maximum,
                                "split_times_seconds": split_times,
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        MODULE.load_segment_plan(path),
                        (maximum, [float(split_times[0])]),
                    )

    def test_output_size_is_even_and_bounded_to_720p(self) -> None:
        self.assertEqual(MODULE.output_size(1920, 1080), (1280, 720))
        self.assertEqual(MODULE.output_size(1080, 1920), (720, 1280))
        width, height = MODULE.output_size(1001, 777)
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)
        self.assertLessEqual(width, 1280)
        self.assertLessEqual(height, 720)


if __name__ == "__main__":
    unittest.main()
