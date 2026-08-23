from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "run_pipeline.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
MEDIA = importlib.import_module("media_preflight")
SPEC = importlib.util.spec_from_file_location("run_pipeline", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载测试目标：{SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeProcess:
    def __init__(self, return_code: int, on_wait=None) -> None:
        self.return_code = return_code
        self.on_wait = on_wait
        self.finished = False

    def wait(self, timeout=None):
        if not self.finished and self.on_wait:
            self.on_wait()
        self.finished = True
        return self.return_code

    def poll(self):
        return self.return_code if self.finished else None

    def terminate(self):
        self.finished = True

    def kill(self):
        self.finished = True


class ResumePipelineTests(unittest.TestCase):
    def make_args(self, root: Path, video: Path) -> SimpleNamespace:
        return SimpleNamespace(
            video=video,
            analysis_video=None,
            scope="depth-prompt-seedance",
            segment_max_seconds=15,
            product_name="",
            product_image=[],
            character_image=None,
            selling_points="",
            user_idea="",
            transcript_file=None,
            api_key_file=None,
            depth_model=None,
            output_dir=root / "output",
            save_debug=False,
            max_inline_request_mb=9.5,
            max_tokens=32768,
            seedance_resolution="720p",
            seedance_ratio="source",
            seedance_output_format="mp4",
            seedance_generate_audio="auto",
            seedance_watermark=False,
            seedance_seed=None,
            resume=False,
        )

    @staticmethod
    def command_value(command: list[str], option: str) -> Path:
        return Path(command[command.index(option) + 1])

    def test_over_limit_analysis_video_is_rejected_before_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "source video.mp4"
            video.write_bytes(b"video")
            signature = {
                "duration": 60.0,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
            }
            with mock.patch.object(
                MEDIA,
                "probe_video_signature",
                return_value=signature,
            ):
                with self.assertRaises(MODULE.PipelineError) as raised:
                    MODULE.validate_analysis_video(video, video, 0.000001)

            message = str(raised.exception)
            self.assertIn("ffmpeg -i", message)
            self.assertIn("--analysis-video", message)
            self.assertIn("source video_compressed.mp4", message)

    def test_analysis_video_rejects_audio_or_aspect_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            analysis = root / "analysis.mp4"
            source.write_bytes(b"source")
            analysis.write_bytes(b"analysis")
            source_signature = {
                "duration": 10.0,
                "start_time": 0.0,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
            }
            no_audio = {**source_signature, "has_audio": False}
            wrong_aspect = {**source_signature, "width": 1080, "height": 1080}
            wrong_duration = {**source_signature, "duration": 10.2}
            wrong_start = {**source_signature, "start_time": 0.2}

            with mock.patch.object(
                MEDIA,
                "probe_video_signature",
                side_effect=[source_signature, no_audio],
            ):
                with self.assertRaisesRegex(MODULE.PipelineError, "音轨存在性"):
                    MODULE.validate_analysis_video(source, analysis, 9.5)

            with mock.patch.object(
                MEDIA,
                "probe_video_signature",
                side_effect=[source_signature, wrong_aspect],
            ):
                with self.assertRaisesRegex(MODULE.PipelineError, "显示画幅"):
                    MODULE.validate_analysis_video(source, analysis, 9.5)

            with mock.patch.object(
                MEDIA,
                "probe_video_signature",
                side_effect=[source_signature, wrong_duration],
            ):
                with self.assertRaisesRegex(MODULE.PipelineError, "时长不一致"):
                    MODULE.validate_analysis_video(source, analysis, 9.5)

            source_with_start = {**source_signature, "start_time": 0.0}
            with mock.patch.object(
                MEDIA,
                "probe_video_signature",
                side_effect=[source_with_start, wrong_start],
            ):
                with self.assertRaisesRegex(MODULE.PipelineError, "时间轴起点"):
                    MODULE.validate_analysis_video(source, analysis, 9.5)

            source_boundary = {
                **source_with_start,
                "duration": 15.02,
            }
            analysis_boundary = {
                **source_with_start,
                "duration": 14.97,
            }
            with mock.patch.object(
                MEDIA,
                "probe_video_signature",
                side_effect=[source_boundary, analysis_boundary],
            ):
                with self.assertRaisesRegex(MODULE.PipelineError, "向上取整"):
                    MODULE.validate_analysis_video(source, analysis, 9.5)

    def test_real_compressed_video_preserves_audio_aspect_and_duration(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("未安装 ffmpeg")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            analysis = root / "source_compressed.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-t",
                    "2",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(source),
                ],
                check=True,
            )
            signature = MODULE.probe_video_signature(source)
            command = MODULE.build_compression_command(source, signature)
            subprocess.run(
                shlex.split(command),
                check=True,
                capture_output=True,
                text=True,
            )

            MODULE.validate_analysis_video(source, analysis, 9.5)
            self.assertLess(
                MODULE.estimated_data_url_size(analysis),
                int(9.5 * 1024 * 1024),
            )

            wrong = root / "wrong.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:size=160x90:rate=10",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-t",
                    "2",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(wrong),
                ],
                check=True,
            )
            with self.assertRaisesRegex(MODULE.PipelineError, "画面内容不一致"):
                MODULE.validate_analysis_video(source, wrong, 9.5)

    def test_image_preflight_checks_base64_before_pipeline(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "product.png"
            Image.new("RGB", (32, 32), "red").save(image_path)
            MODULE.validate_image_input(image_path, "产品图", 9.5)
            with self.assertRaisesRegex(MODULE.PipelineError, "9.5 MiB"):
                MODULE.validate_image_input(image_path, "产品图", 0.000001)

    def test_seedance_image_preflight_runs_before_paid_or_depth_processes(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "input.mp4"
            video.write_bytes(b"video")
            image_path = root / "small-product.png"
            Image.new("RGB", (100, 100), "red").save(image_path)
            args = self.make_args(root, video)
            args.scope = "prompt-seedance"
            args.product_image = [image_path]

            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(MODULE, "validate_analysis_video"),
                mock.patch.object(
                    MODULE.subprocess,
                    "Popen",
                    side_effect=AssertionError("预检失败后不应启动任何子进程"),
                ),
            ):
                self.assertEqual(MODULE.main(), 1)

            self.assertFalse(args.output_dir.exists())

    def test_compression_bitrate_is_duration_aware(self) -> None:
        source = Path("/tmp/source.mp4")
        short_command = MODULE.build_compression_command(
            source,
            {
                "duration": 60.0,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
            },
        )
        long_command = MODULE.build_compression_command(
            source,
            {
                "duration": 180.0,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
            },
        )
        self.assertNotEqual(short_command, long_command)
        self.assertNotIn("-b:v 800k", short_command)
        self.assertIn("-ac 1", short_command)

    def test_resume_reuses_draft_and_depth_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "input.mp4"
            model = root / "model.onnx"
            video.write_bytes(b"video")
            model.write_bytes(b"model")
            args = self.make_args(root, video)

            initial_commands: list[list[str]] = []

            def initial_popen(command: list[str]):
                initial_commands.append(command)
                if str(MODULE.QWEN_SCRIPT) in command:

                    def write_draft():
                        self.command_value(command, "--draft-output").write_text(
                            "镜头1[00:00-00:05] 初稿",
                            encoding="utf-8",
                        )
                        self.command_value(
                            command, "--draft-metadata-output"
                        ).write_text("{}\n", encoding="utf-8")

                    return FakeProcess(1, write_draft)

                def write_depth_cache():
                    work_dir = self.command_value(command, "--work-dir")
                    work_dir.mkdir(parents=True, exist_ok=True)
                    (work_dir / "raw_depth_float16.npy").write_bytes(b"raw")
                    (work_dir / "global_bounds.json").write_text(
                        "{}\n", encoding="utf-8"
                    )

                return FakeProcess(0, write_depth_cache)

            def fake_run_process(command: list[str]) -> int:
                output_dir = self.command_value(command, "--output-dir")
                output_dir.mkdir(parents=True, exist_ok=True)
                if str(MODULE.SEEDANCE_SCRIPT) in command:
                    (output_dir / "seedance_plan.json").write_text(
                        "{}\n", encoding="utf-8"
                    )
                    return 0
                (output_dir / "input_depth_720p_part_01.mp4").write_bytes(b"depth")
                return 0

            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(MODULE, "resolve_depth_model", return_value=model),
                mock.patch.object(MODULE, "validate_analysis_video"),
                mock.patch.object(
                    MODULE.subprocess, "Popen", side_effect=initial_popen
                ),
                mock.patch.object(MODULE, "run_process", side_effect=fake_run_process),
                mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test-value"}),
            ):
                self.assertEqual(MODULE.main(), 1)

            self.assertTrue((args.output_dir / "prompt_draft.txt").is_file())
            self.assertTrue((args.output_dir / ".depth_work").is_dir())
            self.assertFalse(list((args.output_dir / "depth").glob("*.mp4")))

            args.resume = True
            resume_commands: list[list[str]] = []

            def resume_popen(command: list[str]):
                resume_commands.append(command)
                self.assertIn("--draft-file", command)
                self.assertIn("--overwrite", command)

                def write_final_prompt():
                    self.command_value(command, "--output").write_text(
                        "镜头1[00:00-00:05] 最终稿",
                        encoding="utf-8",
                    )
                    self.command_value(command, "--segment-plan-output").write_text(
                        json.dumps(
                            {
                                "segment_max_seconds": 15,
                                "segments": [{"index": 1, "duration_seconds": 5}],
                                "split_times_seconds": [],
                            }
                        ),
                        encoding="utf-8",
                    )

                return FakeProcess(0, write_final_prompt)

            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(MODULE, "resolve_depth_model", return_value=model),
                mock.patch.object(MODULE, "validate_analysis_video"),
                mock.patch.object(MODULE.subprocess, "Popen", side_effect=resume_popen),
                mock.patch.object(MODULE, "run_process", side_effect=fake_run_process),
                mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test-value"}),
            ):
                self.assertEqual(MODULE.main(), 0)

            self.assertEqual(len(resume_commands), 1)
            self.assertFalse((args.output_dir / ".depth_work").exists())
            self.assertTrue((args.output_dir / "ready_for_seedance.json").is_file())
            self.assertTrue((args.output_dir / "seedance/seedance_plan.json").is_file())
            self.assertTrue(
                (args.output_dir / "depth/input_depth_720p_part_01.mp4").is_file()
            )

            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(MODULE, "resolve_depth_model", return_value=model),
                mock.patch.object(MODULE, "validate_analysis_video"),
                mock.patch.object(
                    MODULE.subprocess,
                    "Popen",
                    side_effect=AssertionError("完成任务不应再次启动子进程"),
                ),
            ):
                self.assertEqual(MODULE.main(), 0)

    def test_qwen_failure_reports_depth_encode_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "input.mp4"
            model = root / "model.onnx"
            video.write_bytes(b"video")
            model.write_bytes(b"model")
            args = self.make_args(root, video)

            def fake_popen(command: list[str]):
                return FakeProcess(1 if str(MODULE.QWEN_SCRIPT) in command else 0)

            stderr = StringIO()
            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(MODULE, "resolve_depth_model", return_value=model),
                mock.patch.object(MODULE, "validate_analysis_video"),
                mock.patch.object(MODULE.subprocess, "Popen", side_effect=fake_popen),
                mock.patch.object(
                    MODULE,
                    "run_process",
                    side_effect=AssertionError("Qwen 失败后不应运行深度编码"),
                ),
                mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test-value"}),
                redirect_stderr(stderr),
            ):
                self.assertEqual(MODULE.main(), 1)

            self.assertIn("depth_encode=skipped", stderr.getvalue())

    def test_prompt_seedance_mode_never_resolves_or_runs_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "input.mp4"
            video.write_bytes(b"video")
            args = self.make_args(root, video)
            args.scope = "prompt-seedance"
            commands: list[list[str]] = []

            def fake_popen(command: list[str]):
                commands.append(command)
                self.assertIn(str(MODULE.QWEN_SCRIPT), command)

                def write_prompt_outputs():
                    self.command_value(command, "--output").write_text(
                        "镜头1[00:00-00:10] 最终稿",
                        encoding="utf-8",
                    )
                    self.command_value(command, "--segment-plan-output").write_text(
                        json.dumps(
                            {
                                "segment_max_seconds": 15,
                                "prompt_duration_seconds": 10,
                                "segments": [{"index": 1, "duration_seconds": 10}],
                                "split_times_seconds": [],
                            }
                        ),
                        encoding="utf-8",
                    )

                return FakeProcess(0, write_prompt_outputs)

            def fake_prepare(command: list[str]) -> int:
                self.assertIn(str(MODULE.SEEDANCE_SCRIPT), command)
                self.assertNotIn("--depth-dir", command)
                output_dir = self.command_value(command, "--output-dir")
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "seedance_plan.json").write_text("{}\n", encoding="utf-8")
                return 0

            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(
                    MODULE,
                    "resolve_depth_model",
                    side_effect=AssertionError("无白模模式不应解析深度模型"),
                ),
                mock.patch.object(MODULE, "validate_analysis_video"),
                mock.patch.object(MODULE.subprocess, "Popen", side_effect=fake_popen),
                mock.patch.object(MODULE, "run_process", side_effect=fake_prepare),
                mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test-value"}),
            ):
                self.assertEqual(MODULE.main(), 0)

            self.assertEqual(len(commands), 1)
            self.assertFalse((args.output_dir / "depth").exists())
            self.assertTrue((args.output_dir / "ready_for_seedance.json").is_file())


if __name__ == "__main__":
    unittest.main()
