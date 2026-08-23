from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "seedance_video_pipeline.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("seedance_video_pipeline", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载测试目标：{SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeTasks:
    def __init__(self, create_error: Exception | None = None) -> None:
        self.create_error = create_error
        self.create_count = 0
        self.get_count = 0

    def create(self, **_: object) -> SimpleNamespace:
        self.create_count += 1
        if self.create_error:
            raise self.create_error
        return SimpleNamespace(id="task-1")

    def get(self, **_: object) -> dict[str, object]:
        self.get_count += 1
        return {
            "id": "task-1",
            "status": "succeeded",
            "content": {"video_url": "https://example.invalid/result.mp4"},
        }


class SeedancePipelineTests(unittest.TestCase):
    @staticmethod
    def metadata(has_audio: bool = True) -> dict[str, object]:
        return {
            "duration": 10.0,
            "width": 720,
            "height": 1280,
            "fps": 30.0,
            "codec": "h264",
            "has_audio": has_audio,
        }

    def make_prepare_args(
        self,
        root: Path,
        source: Path,
        prompt: Path,
        plan: Path,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            prompt=prompt,
            segment_plan=plan,
            source_video=source,
            depth_dir=None,
            character_image=None,
            product_image=[],
            transcript_file=None,
            output_dir=root / "seedance",
            resolution="720p",
            ratio="source",
            output_format="mp4",
            generate_audio="auto",
            watermark=False,
            seed=42,
            overwrite=False,
        )

    @staticmethod
    def write_single_segment_inputs(root: Path) -> tuple[Path, Path, Path]:
        source = root / "source.mp4"
        source.write_bytes(b"source")
        prompt = root / "prompt.txt"
        prompt.write_text(
            "参考图片1中的红色包装。镜头1[00:00-00:10] 4K高清产品展示。\n",
            encoding="utf-8",
        )
        plan = root / "segment_plan.json"
        plan.write_text(
            json.dumps(
                {
                    "segment_max_seconds": 15,
                    "prompt_duration_seconds": 10,
                    "segments": [{"index": 1, "duration_seconds": 10}],
                }
            ),
            encoding="utf-8",
        )
        return source, prompt, plan

    def test_prepare_without_depth_compiles_seedance_image_aliases(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            image = root / "product.png"
            Image.new("RGB", (320, 320), "red").save(image)
            args = self.make_prepare_args(root, source, prompt, plan)
            args.product_image = [image]

            with mock.patch.object(MODULE, "probe_video", return_value=self.metadata()):
                plan_path = MODULE.prepare(args)

            body = json.loads(plan_path.read_text(encoding="utf-8"))
            compiled_path = Path(body["segments"][0]["prompt"]["path"])
            compiled = compiled_path.read_text(encoding="utf-8")
            self.assertEqual(body["mode"], "text-and-image-reference")
            self.assertIn("@图片1", compiled)
            self.assertNotIn("4K", compiled)
            self.assertNotIn("@视频1", compiled)
            self.assertNotIn("生成一段全新视频", compiled)
            self.assertNotIn("【参考素材职责】", compiled)
            self.assertTrue(body["parameters"]["generate_audio"])
            self.assertEqual(body["parameters"]["resolution"], "720p")

    def test_prepare_with_depth_adds_only_generated_white_model_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text(
                "镜头1[00:00-00:10] 产品展示。\n",
                encoding="utf-8",
            )
            depth_dir = root / "depth"
            depth_dir.mkdir()
            depth = depth_dir / "source_depth_720p_part_01.mp4"
            depth.write_bytes(b"depth")
            args = self.make_prepare_args(root, source, prompt, plan)
            args.depth_dir = depth_dir

            with (
                mock.patch.object(MODULE, "probe_video", return_value=self.metadata()),
                mock.patch.object(MODULE, "normalize_depth_video", return_value=depth),
            ):
                plan_path = MODULE.prepare(args)

            body = json.loads(plan_path.read_text(encoding="utf-8"))
            compiled = Path(body["segments"][0]["prompt"]["path"]).read_text(
                encoding="utf-8"
            )
            self.assertEqual(body["mode"], "depth-reference")
            self.assertTrue(compiled.startswith("@视频1是本段细粒度深度白模参考"))
            self.assertIn("@视频1是本段细粒度深度白模参考", compiled)
            self.assertNotIn("生成一段全新视频", compiled)
            self.assertNotIn("【参考素材职责】", compiled)
            self.assertNotIn("@音频", compiled)
            self.assertEqual(
                body["segments"][0]["depth_video"]["path"], str(depth.resolve())
            )

    def test_default_seedance_resolution_is_720p(self) -> None:
        self.assertEqual(MODULE.DEFAULT_RESOLUTION, "720p")

    def test_prepare_splits_multi_segment_prompt_into_minimum_task_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            prompt = root / "prompt.txt"
            prompt.write_text(
                "【第一段提示词（27秒，对齐参考视频0-27秒）】\n"
                "镜头1[00:00-00:27] 第一段。\n"
                "【第二段提示词（4秒，对齐参考视频27-31秒）】\n"
                "镜头1[00:00-00:04] 第二段。\n",
                encoding="utf-8",
            )
            plan = root / "segment_plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "segment_max_seconds": 30,
                        "prompt_duration_seconds": 31,
                        "segments": [
                            {"index": 1, "duration_seconds": 27},
                            {"index": 2, "duration_seconds": 4},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = self.make_prepare_args(root, source, prompt, plan)
            metadata = {**self.metadata(), "duration": 30.8}

            with mock.patch.object(MODULE, "probe_video", return_value=metadata):
                plan_path = MODULE.prepare(args)

            body = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [segment["duration_seconds"] for segment in body["segments"]],
                [27, 4],
            )
            self.assertIn(
                "第一段",
                Path(body["segments"][0]["prompt"]["path"]).read_text(encoding="utf-8"),
            )
            self.assertIn(
                "第二段",
                Path(body["segments"][1]["prompt"]["path"]).read_text(encoding="utf-8"),
            )

    def test_request_never_contains_original_video_or_audio_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text("镜头1[00:00-00:10] 展示。\n", encoding="utf-8")
            args = self.make_prepare_args(root, source, prompt, plan)
            with mock.patch.object(MODULE, "probe_video", return_value=self.metadata()):
                plan_path = MODULE.prepare(args)
            body = json.loads(plan_path.read_text(encoding="utf-8"))

            request = MODULE.build_request(body, body["segments"][0], [], None)
            self.assertEqual([item["type"] for item in request["content"]], ["text"])
            self.assertNotIn("omni_reference_task_type", request)
            self.assertNotIn(str(source), json.dumps(request, ensure_ascii=False))
            self.assertNotIn("audio_url", json.dumps(request, ensure_ascii=False))

    def test_markdown_config_supports_sts_role_and_authorized_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "Volc engine_API_KEY.md"
            config_path.write_text(
                "Volcengine_API_KEY: ark-key-value-123456\n\n"
                "tos: endpoint: [tos-cn-beijing.volces.com]"
                "(http://tos-cn-beijing.volces.com)  region: cn-beijing  "
                "accessKey: AKLT-test  secretKey: secret-test  "
                "bucket: test-bucket  roleTrn: trn:iam::1:role/tos-put  "
                "publicDomain: https://tos.example.com/  "
                "mainPath: authorized/prod\n",
                encoding="utf-8",
            )

            config = MODULE.load_tos_config(config_path)

            self.assertEqual(config["endpoint"], "tos-cn-beijing.volces.com")
            self.assertEqual(config["role_trn"], "trn:iam::1:role/tos-put")
            self.assertEqual(config["public_domain"], "https://tos.example.com")
            self.assertEqual(
                config["prefix"], "authorized/prod/video-white-model-prompt"
            )
            self.assertEqual(
                MODULE.load_key_file(config_path, "Key", "ARK_API_KEY"),
                "ark-key-value-123456",
            )

    def test_tos_publisher_assumes_role_and_returns_public_tos_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "asset image.png"
            path.write_bytes(b"asset")
            captured: dict[str, object] = {}

            class FakeSts:
                def set_ak(self, value: str) -> None:
                    captured["base_ak"] = value

                def set_sk(self, value: str) -> None:
                    captured["base_sk"] = value

                def assume_role(self, params: dict[str, str]) -> dict[str, object]:
                    captured["role"] = params["RoleTrn"]
                    return {
                        "Result": {
                            "Credentials": {
                                "AccessKeyId": "temp-ak",
                                "SecretAccessKey": "temp-sk",
                                "SessionToken": "temp-token",
                            }
                        }
                    }

            class FakeTosClient:
                def __init__(self, **kwargs: object) -> None:
                    captured["client"] = kwargs

                def put_object_from_file(
                    self,
                    bucket: str,
                    key: str,
                    file_path: str,
                    content_type: str,
                ) -> None:
                    captured["put"] = (bucket, key, file_path, content_type)

            config = {
                "access_key": "base-ak",
                "secret_key": "base-sk",
                "endpoint": "tos-cn-beijing.volces.com",
                "region": "cn-beijing",
                "bucket": "bucket",
                "role_trn": "trn:iam::1:role/tos-put",
                "prefix": "authorized/prod/video-white-model-prompt",
                "public_domain": "https://tos.example.com",
            }
            with (
                mock.patch(
                    "volcengine.sts.StsService.StsService",
                    return_value=FakeSts(),
                ),
                mock.patch("tos.TosClientV2", side_effect=FakeTosClient),
            ):
                publisher = MODULE.TosPublisher(config, 3600)
                uploaded = publisher.upload(path, "run-id", "image-01")

            self.assertEqual(captured["role"], config["role_trn"])
            self.assertEqual(captured["client"]["security_token"], "temp-token")
            self.assertTrue(
                str(uploaded["url"]).startswith(
                    "https://tos.example.com/authorized/prod/"
                )
            )
            self.assertNotIn(" ", str(uploaded["url"]))

    def test_create_task_passes_new_seedance_fields_through_extra_body(self) -> None:
        captured: dict[str, object] = {}

        class RecordingTasks:
            def create(self, **kwargs: object) -> SimpleNamespace:
                captured.update(kwargs)
                return SimpleNamespace(id="task-extra-body")

        client = SimpleNamespace(
            content_generation=SimpleNamespace(tasks=RecordingTasks())
        )
        request = {
            "model": MODULE.MODEL_ID,
            "content": [{"type": "text", "text": "生成一段全新视频。"}],
            "generate_audio": False,
            "ratio": "9:16",
            "duration": 10,
            "resolution": "720p",
            "watermark": False,
            "seed": 42,
            "output_format": "mp4",
            "omni_reference_task_type": "reference",
        }

        task_id = MODULE.create_task(client, request)

        self.assertEqual(task_id, "task-extra-body")
        self.assertEqual(
            captured["extra_body"],
            {
                "omni_reference_task_type": "reference",
                "output_format": "mp4",
            },
        )

    def test_normalizes_incompatible_depth_video_to_seedance_limits(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("未安装 ffmpeg")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "depth.mp4"
            destination = root / "normalized.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=gray:size=160x90:rate=10",
                    "-t",
                    "4",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                check=True,
            )

            normalized = MODULE.normalize_depth_video(source, destination)
            metadata = MODULE.probe_video(normalized)
            self.assertEqual(normalized, destination)
            self.assertTrue(MODULE.depth_video_compatible(metadata))
            self.assertGreaterEqual(float(metadata["fps"]), 24)

    def test_ffmpeg_concatenates_generated_parts_in_plan_order(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("未安装 ffmpeg")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parts = []
            for index, color in enumerate(("red", "blue"), start=1):
                part = root / f"part_{index:02d}.mp4"
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:size=320x320:rate=24",
                        "-f",
                        "lavfi",
                        "-i",
                        f"sine=frequency={400 + index * 100}:sample_rate=48000",
                        "-t",
                        "2",
                        "-shortest",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        str(part),
                    ],
                    check=True,
                )
                parts.append(part)

            full = root / "full.mp4"
            result = MODULE.concat_generated_videos(
                parts,
                full,
                expected_duration=4,
                expect_audio=True,
            )

            self.assertEqual(result, full)
            metadata = MODULE.probe_video(full)
            self.assertAlmostEqual(float(metadata["duration"]), 4, delta=0.2)
            self.assertTrue(metadata["has_audio"])

    def test_submit_resume_does_not_create_duplicate_paid_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text("镜头1[00:00-00:10] 展示。\n", encoding="utf-8")
            prepare_args = self.make_prepare_args(root, source, prompt, plan)
            with mock.patch.object(
                MODULE, "probe_video", return_value=self.metadata(has_audio=False)
            ):
                plan_path = MODULE.prepare(prepare_args)

            submit_args = SimpleNamespace(
                plan=plan_path,
                ark_api_key_file=None,
                tos_config_file=None,
                poll_interval=0.0,
                poll_timeout=5.0,
                signed_url_ttl=3600,
                retry_failed=False,
                allow_recreate_ambiguous=False,
            )
            tasks = FakeTasks()
            client = SimpleNamespace(content_generation=SimpleNamespace(tasks=tasks))

            def fake_download(_: str, destination: Path, attempts: int = 3) -> None:
                del attempts
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"generated")

            def fake_concat(
                parts: list[Path],
                destination: Path,
                expected_duration: int,
                expect_audio: bool,
            ) -> Path:
                del expected_duration, expect_audio
                destination.write_bytes(parts[0].read_bytes())
                return destination

            with (
                mock.patch.dict(os.environ, {"ARK_API_KEY": "ark-test-key-value"}),
                mock.patch.object(MODULE, "download_video", side_effect=fake_download),
                mock.patch.object(MODULE, "validate_generated_video"),
                mock.patch.object(
                    MODULE,
                    "concat_generated_videos",
                    side_effect=fake_concat,
                ),
            ):
                MODULE.submit(submit_args, client_factory=lambda _: client)
                MODULE.submit(submit_args, client_factory=lambda _: client)

            self.assertEqual(tasks.create_count, 1)
            self.assertEqual(tasks.get_count, 1)
            state = json.loads(
                plan_path.with_name("tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["segments"]["1"]["status"], "downloaded")
            self.assertEqual(state["full_output"]["status"], "complete")

    def test_submit_creates_all_segments_before_parallel_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            prompt = root / "prompt.txt"
            prompt.write_text(
                "【第一段提示词（10秒，对齐参考视频0-10秒）】\n"
                "镜头1[00:00-00:10] 第一段。\n"
                "【第二段提示词（10秒，对齐参考视频10-20秒）】\n"
                "镜头1[00:00-00:10] 第二段。\n",
                encoding="utf-8",
            )
            segment_plan = root / "segment_plan.json"
            segment_plan.write_text(
                json.dumps(
                    {
                        "segment_max_seconds": 15,
                        "prompt_duration_seconds": 20,
                        "segments": [
                            {"index": 1, "duration_seconds": 10},
                            {"index": 2, "duration_seconds": 10},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            prepare_args = self.make_prepare_args(
                root,
                source,
                prompt,
                segment_plan,
            )
            with mock.patch.object(
                MODULE,
                "probe_video",
                return_value={
                    **self.metadata(has_audio=False),
                    "duration": 20.0,
                },
            ):
                plan_path = MODULE.prepare(prepare_args)

            class ParallelTasks:
                def __init__(self) -> None:
                    self.create_count = 0
                    self.lock = threading.Lock()
                    self.poll_barrier = threading.Barrier(2)

                def create(self, **_: object) -> SimpleNamespace:
                    with self.lock:
                        self.create_count += 1
                        task_id = f"task-{self.create_count}"
                    return SimpleNamespace(id=task_id)

                def get(self, task_id: str) -> dict[str, object]:
                    with self.lock:
                        if self.create_count != 2:
                            raise AssertionError("轮询前必须先创建全部任务")
                    self.poll_barrier.wait(timeout=2)
                    return {
                        "id": task_id,
                        "status": "succeeded",
                        "content": {
                            "video_url": f"https://example.invalid/{task_id}.mp4"
                        },
                    }

            tasks = ParallelTasks()
            client = SimpleNamespace(content_generation=SimpleNamespace(tasks=tasks))
            submit_args = SimpleNamespace(
                plan=plan_path,
                ark_api_key_file=None,
                tos_config_file=None,
                poll_interval=0.0,
                poll_timeout=5.0,
                signed_url_ttl=3600,
                retry_failed=False,
                allow_recreate_ambiguous=False,
            )

            def fake_download(_: str, destination: Path, attempts: int = 3) -> None:
                del attempts
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(destination.name.encode("utf-8"))

            def fake_concat(
                parts: list[Path],
                destination: Path,
                expected_duration: int,
                expect_audio: bool,
            ) -> Path:
                del expected_duration, expect_audio
                destination.write_bytes(b"".join(part.read_bytes() for part in parts))
                return destination

            with (
                mock.patch.dict(os.environ, {"ARK_API_KEY": "ark-test-key-value"}),
                mock.patch.object(MODULE, "download_video", side_effect=fake_download),
                mock.patch.object(MODULE, "validate_generated_video"),
                mock.patch.object(
                    MODULE,
                    "concat_generated_videos",
                    side_effect=fake_concat,
                ),
            ):
                MODULE.submit(submit_args, client_factory=lambda _: client)

            self.assertEqual(tasks.create_count, 2)
            state = json.loads(
                plan_path.with_name("tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["segments"]["1"]["status"], "downloaded")
            self.assertEqual(state["segments"]["2"]["status"], "downloaded")

    def test_invalid_existing_download_is_archived_and_redownloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text("镜头1[00:00-00:10] 展示。\n", encoding="utf-8")
            prepare_args = self.make_prepare_args(root, source, prompt, plan)
            with mock.patch.object(
                MODULE, "probe_video", return_value=self.metadata(has_audio=False)
            ):
                plan_path = MODULE.prepare(prepare_args)
            prepared = json.loads(plan_path.read_text(encoding="utf-8"))
            output = Path(prepared["segments"][0]["output_file"])
            output.write_bytes(b"corrupt")
            MODULE.atomic_write_json(
                plan_path.with_name("tasks.json"),
                {
                    "schema_version": 1,
                    "run_id": prepared["run_id"],
                    "uploads": {},
                    "segments": {"1": {"task_id": "task-1", "status": "succeeded"}},
                },
            )
            submit_args = SimpleNamespace(
                plan=plan_path,
                ark_api_key_file=None,
                tos_config_file=None,
                poll_interval=0.0,
                poll_timeout=5.0,
                signed_url_ttl=3600,
                retry_failed=False,
                allow_recreate_ambiguous=False,
            )
            tasks = FakeTasks()
            client = SimpleNamespace(content_generation=SimpleNamespace(tasks=tasks))

            def fake_validate(
                path: Path,
                expected_duration: int,
                expect_audio: bool,
                duration_tolerance: float = 1.0,
            ) -> None:
                del expected_duration, expect_audio, duration_tolerance
                if path.read_bytes() == b"corrupt":
                    raise MODULE.SeedanceError("invalid video")

            def fake_download(_: str, destination: Path, attempts: int = 3) -> None:
                del attempts
                destination.write_bytes(b"generated")

            def fake_concat(
                parts: list[Path],
                destination: Path,
                expected_duration: int,
                expect_audio: bool,
            ) -> Path:
                del expected_duration, expect_audio
                destination.write_bytes(parts[0].read_bytes())
                return destination

            with (
                mock.patch.dict(os.environ, {"ARK_API_KEY": "ark-test-key-value"}),
                mock.patch.object(
                    MODULE, "validate_generated_video", side_effect=fake_validate
                ),
                mock.patch.object(MODULE, "download_video", side_effect=fake_download),
                mock.patch.object(
                    MODULE,
                    "concat_generated_videos",
                    side_effect=fake_concat,
                ),
            ):
                MODULE.submit(submit_args, client_factory=lambda _: client)

            self.assertEqual(tasks.create_count, 0)
            self.assertEqual(tasks.get_count, 1)
            self.assertEqual(output.read_bytes(), b"generated")
            self.assertEqual(
                output.with_name("part_01.invalid_1.mp4").read_bytes(), b"corrupt"
            )

    def test_ambiguous_create_is_persisted_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text("镜头1[00:00-00:10] 展示。\n", encoding="utf-8")
            prepare_args = self.make_prepare_args(root, source, prompt, plan)
            with mock.patch.object(
                MODULE, "probe_video", return_value=self.metadata(has_audio=False)
            ):
                plan_path = MODULE.prepare(prepare_args)

            submit_args = SimpleNamespace(
                plan=plan_path,
                ark_api_key_file=None,
                tos_config_file=None,
                poll_interval=0.0,
                poll_timeout=5.0,
                signed_url_ttl=3600,
                retry_failed=False,
                allow_recreate_ambiguous=False,
            )
            tasks = FakeTasks(create_error=RuntimeError("connection reset"))
            client = SimpleNamespace(content_generation=SimpleNamespace(tasks=tasks))

            with mock.patch.dict(os.environ, {"ARK_API_KEY": "ark-test-key-value"}):
                with self.assertRaisesRegex(MODULE.SeedanceError, "结果未知"):
                    MODULE.submit(submit_args, client_factory=lambda _: client)
                with self.assertRaisesRegex(MODULE.SeedanceError, "拒绝自动重复提交"):
                    MODULE.submit(submit_args, client_factory=lambda _: client)

            self.assertEqual(tasks.create_count, 1)
            state = json.loads(
                plan_path.with_name("tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["segments"]["1"]["status"], "create_ambiguous")


if __name__ == "__main__":
    unittest.main()
