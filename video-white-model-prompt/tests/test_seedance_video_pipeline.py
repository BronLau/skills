from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
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
        omni_facts = root / "omni_facts.json"
        verification = root / "max_verification.json"
        omni_facts.write_text("{}\n", encoding="utf-8")
        verification.write_text("{}\n", encoding="utf-8")
        fact_lock = root / "fact_lock.json"
        fact_lock.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "locked",
                    "assembly_mode": "deterministic_from_max_verified_facts",
                    "prompt_sha256": MODULE.prompt_text_sha256(prompt),
                    "segment_plan_sha256": MODULE.file_sha256(plan),
                    "analysis_video": MODULE.file_identity(source),
                    "omni_facts": MODULE.file_identity(omni_facts),
                    "max_verification": MODULE.file_identity(verification),
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            prompt=prompt,
            segment_plan=plan,
            fact_lock=fact_lock,
            source_video=source,
            depth_dir=None,
            character_image=None,
            character_image_type=None,
            character_asset_id=None,
            confirm_virtual_portrait_rights=False,
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
            self.assertEqual(body["segment_max_seconds"], 15)
            self.assertEqual(
                body["model"], MODULE.MODEL_BY_SEGMENT_MAX_SECONDS[15]
            )

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

    def test_prepare_virtual_character_records_private_asset_contract(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text(
                "参考@图片1中的虚拟人物，将其定义为<主播>。\n"
                "镜头1[00:00-00:10] <主播>展示产品。\n",
                encoding="utf-8",
            )
            character = root / "virtual-character.png"
            Image.new("RGB", (720, 1280), "blue").save(character)
            args = self.make_prepare_args(root, source, prompt, plan)
            args.character_image = character
            args.character_image_type = "virtual"
            args.confirm_virtual_portrait_rights = True

            with mock.patch.object(MODULE, "probe_video", return_value=self.metadata()):
                plan_path = MODULE.prepare(args)

            body = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(body["images"][0]["reference_role"], "character")
            self.assertEqual(
                body["character_reference"],
                {
                    "image_id": "image-01",
                    "portrait_type": "virtual",
                    "asset_id": None,
                    "virtual_rights_confirmed": True,
                },
            )

    def test_prepare_real_character_requires_authorized_asset_id(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text(
                "参考@图片1中的人物，将其定义为<主播>。\n"
                "镜头1[00:00-00:10] <主播>展示产品。\n",
                encoding="utf-8",
            )
            character = root / "real-character.png"
            Image.new("RGB", (720, 1280), "blue").save(character)
            args = self.make_prepare_args(root, source, prompt, plan)
            args.character_image = character
            args.character_image_type = "real"

            with self.assertRaisesRegex(MODULE.SeedanceError, "已授权"):
                MODULE.prepare(args)

    def test_virtual_character_asset_creation_is_persisted_and_polled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            character = root / "character.png"
            character.write_bytes(b"character")
            state_path = root / "tasks.json"
            state = {"schema_version": 1, "run_id": "run-id", "uploads": {}, "segments": {}}
            MODULE.atomic_write_json(state_path, state)
            plan = {
                "run_id": "run-id",
                "character_reference": {
                    "portrait_type": "virtual",
                    "asset_id": None,
                },
                "images": [
                    {
                        "reference_role": "character",
                        "identity": MODULE.file_identity(character),
                    }
                ],
            }

            class FakeLibrary:
                def __init__(self) -> None:
                    self.statuses = ["Processing", "Active"]
                    self.group_calls = 0
                    self.asset_calls = 0

                def create_group(self, _: str, __: str) -> str:
                    self.group_calls += 1
                    return "group-test"

                def create_asset(self, _: str, __: str, ___: str) -> str:
                    self.asset_calls += 1
                    return "asset-test"

                def get_asset(self, _: str) -> dict[str, str]:
                    return {
                        "Status": self.statuses.pop(0),
                        "ProjectName": "default",
                    }

            library = FakeLibrary()
            uri = MODULE.ensure_character_asset_uri(
                plan,
                state,
                state_path,
                library,
                "https://tos.example.com/character.png",
                "default",
                0,
                5,
                False,
                False,
            )

            self.assertEqual(uri, "asset://asset-test")
            self.assertEqual(library.group_calls, 1)
            self.assertEqual(library.asset_calls, 1)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["character_asset"]["status"], "Active")

    def test_existing_real_character_asset_is_validated_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            character = root / "character.png"
            character.write_bytes(b"character")
            state_path = root / "tasks.json"
            state = {"schema_version": 1, "run_id": "run-id", "uploads": {}, "segments": {}}
            MODULE.atomic_write_json(state_path, state)
            plan = {
                "run_id": "run-id",
                "character_reference": {
                    "portrait_type": "real",
                    "asset_id": "asset-authorized",
                },
                "images": [
                    {
                        "reference_role": "character",
                        "identity": MODULE.file_identity(character),
                    }
                ],
            }

            class ExistingAssetLibrary:
                def create_group(self, *_: object) -> str:
                    raise AssertionError("不应创建素材组")

                def create_asset(self, *_: object) -> str:
                    raise AssertionError("不应创建素材")

                def get_asset(self, asset_id: str) -> dict[str, str]:
                    self.asset_id = asset_id
                    return {
                        "Id": asset_id,
                        "Status": "Active",
                        "ProjectName": "default",
                        "URL": "https://example.invalid/character.png",
                    }

            library = ExistingAssetLibrary()
            with mock.patch.object(
                MODULE, "validate_character_asset_identity"
            ) as identity_check:
                uri = MODULE.ensure_character_asset_uri(
                    plan,
                    state,
                    state_path,
                    library,
                    None,
                    "default",
                    0,
                    5,
                    False,
                    False,
                )

            self.assertEqual(uri, "asset://asset-authorized")
            self.assertEqual(library.asset_id, "asset-authorized")
            identity_check.assert_called_once()

    def test_existing_asset_with_different_character_image_is_rejected(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            character = root / "character.png"
            Image.new("RGB", (600, 900), "red").save(character)
            remote = io.BytesIO()
            Image.new("RGB", (600, 900), "blue").save(remote, format="PNG")
            response = io.BytesIO(remote.getvalue())
            plan = {
                "images": [
                    {
                        "reference_role": "character",
                        "identity": MODULE.file_identity(character),
                    }
                ]
            }

            with mock.patch.object(
                MODULE.urllib.request,
                "urlopen",
                return_value=response,
            ):
                with self.assertRaisesRegex(MODULE.SeedanceError, "视觉不一致"):
                    MODULE.validate_character_asset_identity(
                        plan,
                        {"URL": "https://example.invalid/character.png"},
                    )

    def test_ambiguous_character_group_creation_is_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            character = root / "character.png"
            character.write_bytes(b"character")
            state_path = root / "tasks.json"
            state = {"schema_version": 1, "run_id": "run-id", "uploads": {}, "segments": {}}
            MODULE.atomic_write_json(state_path, state)
            plan = {
                "run_id": "run-id",
                "character_reference": {
                    "portrait_type": "virtual",
                    "asset_id": None,
                },
                "images": [
                    {
                        "reference_role": "character",
                        "identity": MODULE.file_identity(character),
                    }
                ],
            }

            class AmbiguousLibrary:
                def __init__(self) -> None:
                    self.calls = 0

                def create_group(self, _: str, __: str) -> str:
                    self.calls += 1
                    raise TimeoutError("unknown result")

            library = AmbiguousLibrary()
            for _ in range(2):
                with self.assertRaisesRegex(MODULE.SeedanceError, "自动重复创建"):
                    MODULE.ensure_character_asset_uri(
                        plan,
                        state,
                        state_path,
                        library,
                        "https://tos.example.com/character.png",
                        "default",
                        0,
                        5,
                        False,
                        False,
                    )

            self.assertEqual(library.calls, 1)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["character_asset"]["group_create_status"],
                "create_ambiguous",
            )

    def test_virtual_character_reuses_matching_asset_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            character = root / "character.png"
            character.write_bytes(b"character")
            state_path = root / "tasks.json"
            state = {
                "schema_version": 1,
                "run_id": "run-id",
                "uploads": {},
                "segments": {},
            }
            MODULE.atomic_write_json(state_path, state)
            plan = {
                "run_id": "run-id",
                "character_reference": {
                    "portrait_type": "virtual",
                    "asset_id": None,
                },
                "images": [
                    {
                        "reference_role": "character",
                        "identity": MODULE.file_identity(character),
                    }
                ],
            }

            class ReuseLibrary:
                def find_asset(self, name: str) -> dict[str, str]:
                    self.name = name
                    return {
                        "Id": "asset-existing",
                        "GroupId": "group-existing",
                    }

                def create_group(self, *_: object) -> str:
                    raise AssertionError("复用素材时不应创建素材组")

                def create_asset(self, *_: object) -> str:
                    raise AssertionError("复用素材时不应创建素材")

                def get_asset(self, asset_id: str) -> dict[str, str]:
                    return {
                        "Id": asset_id,
                        "Status": "Active",
                        "ProjectName": "default",
                        "URL": "https://example.invalid/character.png",
                    }

            upload_called = False

            def character_url() -> str:
                nonlocal upload_called
                upload_called = True
                return "https://tos.example.com/character.png"

            library = ReuseLibrary()
            with mock.patch.object(MODULE, "validate_character_asset_identity"):
                uri = MODULE.ensure_character_asset_uri(
                    plan,
                    state,
                    state_path,
                    library,
                    character_url,
                    "default",
                    0,
                    5,
                    False,
                    False,
                )

            self.assertEqual(uri, "asset://asset-existing")
            self.assertFalse(upload_called)
            self.assertEqual(
                library.name,
                "vwm-" + MODULE.file_sha256(character)[:32],
            )

    def test_character_asset_resume_skips_duplicate_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            character = root / "character.png"
            character.write_bytes(b"character")
            state_path = root / "tasks.json"
            state = {
                "schema_version": 1,
                "run_id": "run-id",
                "uploads": {},
                "segments": {},
                "character_asset": {
                    "project_name": "default",
                    "portrait_type": "virtual",
                    "asset_id": "asset-recorded",
                    "source": "created",
                    "status": "Processing",
                },
            }
            MODULE.atomic_write_json(state_path, state)
            plan = {
                "run_id": "run-id",
                "character_reference": {
                    "portrait_type": "virtual",
                    "asset_id": None,
                },
                "images": [
                    {
                        "reference_role": "character",
                        "identity": MODULE.file_identity(character),
                    }
                ],
            }

            class ResumeLibrary:
                def find_asset(self, _: str) -> None:
                    raise AssertionError("恢复时不应重新搜索素材")

                def get_asset(self, asset_id: str) -> dict[str, str]:
                    return {
                        "Id": asset_id,
                        "Status": "Active",
                        "ProjectName": "default",
                    }

            uri = MODULE.ensure_character_asset_uri(
                plan,
                state,
                state_path,
                ResumeLibrary(),
                None,
                "default",
                0,
                5,
                False,
                False,
            )

            self.assertEqual(uri, "asset://asset-recorded")

    def test_get_asset_transient_failure_is_retried(self) -> None:
        class FlakyLibrary:
            def __init__(self) -> None:
                self.calls = 0

            def get_asset(self, asset_id: str) -> dict[str, str]:
                self.calls += 1
                if self.calls < 3:
                    raise TimeoutError("temporary")
                return {"Id": asset_id, "Status": "Active"}

        library = FlakyLibrary()
        result = MODULE.get_character_asset_with_retry(
            library,
            "asset-existing",
            0,
        )

        self.assertEqual(result["Status"], "Active")
        self.assertEqual(library.calls, 3)

    def test_legacy_image_plan_requires_prepare_migration(self) -> None:
        with self.assertRaisesRegex(MODULE.SeedanceError, "重新运行 prepare"):
            MODULE.validate_character_reference_plan(
                {
                    "schema_version": 1,
                    "images": [{"id": "image-01"}],
                }
            )

    def test_ark_config_carries_seedance_and_asset_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "ark.json"
            config_path.write_text(
                "ARK_API_KEY: ark-key-value-123456\n"
                "accessKey: asset-ak\n"
                "secretKey: asset-sk\n",
                encoding="utf-8",
            )

            config = MODULE.load_ark_config(
                config_path,
                require_asset_credentials=True,
            )

            self.assertEqual(config["api_key"], "ark-key-value-123456")
            self.assertEqual(config["access_key"], "asset-ak")
            self.assertEqual(config["secret_key"], "asset-sk")
            self.assertEqual(config["region"], "cn-beijing")
            self.assertNotIn("bucket", config)

    def test_asset_library_builds_documented_openapi_requests(self) -> None:
        captured: list[tuple[str, dict[str, object]]] = []

        class FakeService:
            def json(self, action: str, _: dict[str, object], body: str) -> str:
                request = json.loads(body)
                captured.append((action, request))
                responses = {
                    "CreateAssetGroup": {"Id": "group-created"},
                    "CreateAsset": {"Id": "asset-created"},
                    "GetAsset": {
                        "Id": "asset-created",
                        "Status": "Active",
                        "ProjectName": "project-a",
                    },
                    "ListAssets": {
                        "Items": [
                            {
                                "Id": "asset-existing",
                                "Name": "vwm-hash",
                                "ProjectName": "project-a",
                                "Status": "Active",
                            }
                        ]
                    },
                }
                return json.dumps(responses[action])

        library = MODULE.ArkAssetLibrary.__new__(MODULE.ArkAssetLibrary)
        library.project_name = "project-a"
        library.service = FakeService()

        self.assertEqual(
            library.create_group("group-name", "description"),
            "group-created",
        )
        self.assertEqual(
            library.create_asset(
                "group-created",
                "https://example.invalid/character.png",
                "vwm-hash",
            ),
            "asset-created",
        )
        self.assertEqual(library.get_asset("asset-created")["Status"], "Active")
        self.assertEqual(library.find_asset("vwm-hash")["Id"], "asset-existing")

        requests = {action: body for action, body in captured}
        self.assertEqual(
            requests["CreateAssetGroup"]["ProjectName"],
            "project-a",
        )
        self.assertEqual(requests["CreateAsset"]["AssetType"], "Image")
        self.assertEqual(requests["GetAsset"]["ProjectName"], "project-a")
        self.assertEqual(
            requests["ListAssets"]["Filter"]["Statuses"],
            ["Active", "Processing"],
        )

    def test_default_seedance_resolution_is_720p(self) -> None:
        self.assertEqual(MODULE.DEFAULT_RESOLUTION, "720p")
        self.assertEqual(MODULE.MAX_SEED, 2147483647)
        self.assertEqual(
            MODULE.MODEL_DURATION_RANGE_BY_SEGMENT_MAX_SECONDS,
            {15: (4, 15), 30: (4, 30)},
        )

    def test_prepare_rejects_seed_above_ark_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            args = self.make_prepare_args(root, source, prompt, plan)
            args.seed = MODULE.MAX_SEED + 1

            with mock.patch.object(
                MODULE, "probe_video", return_value=self.metadata()
            ):
                with self.assertRaisesRegex(MODULE.SeedanceError, "2147483647"):
                    MODULE.prepare(args)

    def test_prepare_random_seed_stays_within_ark_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text(
                "镜头1[00:00-00:10] 户外人物展示。\n", encoding="utf-8"
            )
            args = self.make_prepare_args(root, source, prompt, plan)
            args.seed = None

            with (
                mock.patch.object(MODULE, "probe_video", return_value=self.metadata()),
                mock.patch.object(
                    MODULE.secrets, "randbelow", return_value=MODULE.MAX_SEED
                ) as random_mock,
            ):
                plan_path = MODULE.prepare(args)

            random_mock.assert_called_once_with(MODULE.MAX_SEED + 1)
            body = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(body["parameters"]["seed"], MODULE.MAX_SEED)

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
            self.assertEqual(body["segment_max_seconds"], 30)
            self.assertEqual(
                body["model"], MODULE.MODEL_BY_SEGMENT_MAX_SECONDS[30]
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

    def test_prepare_and_request_include_authorized_voice_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            prompt.write_text("镜头1[00:00-00:10] 展示。\n", encoding="utf-8")
            reference_audio = root / "voice.wav"
            with wave.open(str(reference_audio), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(b"\x00\x00" * 16000 * 2)
            args = self.make_prepare_args(root, source, prompt, plan)
            args.reference_audio = reference_audio
            args.confirm_voice_rights = True
            with mock.patch.object(MODULE, "probe_video", return_value=self.metadata()):
                plan_path = MODULE.prepare(args)
            body = json.loads(plan_path.read_text(encoding="utf-8"))
            compiled = Path(body["segments"][0]["prompt"]["path"]).read_text(
                encoding="utf-8"
            )

            request = MODULE.build_request(
                body,
                body["segments"][0],
                [],
                None,
                "https://example.invalid/voice.wav",
            )

            self.assertEqual(body["schema_version"], 4)
            self.assertTrue(body["audio_reference"]["rights_confirmed"])
            self.assertIn("@音频1只作为全片人物口播的统一音色参考", compiled)
            self.assertEqual(request["content"][-1]["type"], "audio_url")
            self.assertEqual(request["content"][-1]["role"], "reference_audio")
            self.assertEqual(request["omni_reference_task_type"], "reference")

    def test_prepare_voice_reference_requires_rights_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            reference_audio = root / "voice.wav"
            reference_audio.write_bytes(b"not-used-before-rights-check")
            args = self.make_prepare_args(root, source, prompt, plan)
            args.reference_audio = reference_audio
            args.confirm_voice_rights = False

            with self.assertRaisesRegex(MODULE.SeedanceError, "声音权利"):
                MODULE.prepare(args)

    def test_tos_markdown_supports_sts_role_and_authorized_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "TOS_Config.md"
            config_path.write_text(
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

    def test_ark_and_tos_files_keep_credentials_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tos_path = root / "TOS_Config.md"
            tos_path.write_text(
                "endpoint: [tos-cn-beijing.volces.com]  region: cn-beijing  "
                "accessKey: tos-ak  secretKey: tos-sk  bucket: test-bucket  "
                "roleTrn: trn:iam::1:role/tos-put  "
                "publicDomain: https://tos.example.com/  "
                "mainPath: authorized/prod\n",
                encoding="utf-8",
            )
            ark_path = root / "Volcengine_API_KEY.md"
            ark_path.write_text(
                "ARK_API_KEY: ark-key-value-123456\n"
                "**accessKey**： asset-ak\n**secretKey**： asset-sk\n",
                encoding="utf-8",
            )

            tos_config = MODULE.load_tos_config(tos_path)
            ark_config = MODULE.load_ark_config(
                ark_path,
                require_asset_credentials=True,
            )

            self.assertEqual(tos_config["access_key"], "tos-ak")
            self.assertEqual(tos_config["secret_key"], "tos-sk")
            self.assertNotIn("asset_access_key", tos_config)
            self.assertEqual(tos_config["endpoint"], "tos-cn-beijing.volces.com")
            self.assertEqual(tos_config["bucket"], "test-bucket")
            self.assertEqual(
                tos_config["prefix"], "authorized/prod/video-white-model-prompt"
            )
            self.assertEqual(ark_config["api_key"], "ark-key-value-123456")
            self.assertEqual(ark_config["access_key"], "asset-ak")
            self.assertEqual(ark_config["secret_key"], "asset-sk")
            self.assertEqual(ark_config["region"], "cn-beijing")

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
                    captured["assume_count"] = int(
                        captured.get("assume_count", 0)
                    ) + 1
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
                publisher.upload(path, "run-id", "image-02")

            self.assertEqual(captured["role"], config["role_trn"])
            self.assertEqual(captured["client"]["security_token"], "temp-token")
            self.assertEqual(captured["assume_count"], 1)
            self.assertTrue(
                str(uploaded["url"]).startswith(
                    "https://tos.example.com/authorized/prod/"
                )
            )
            self.assertNotIn(" ", str(uploaded["url"]))

    def test_local_proxy_is_bypassed_for_seedance_network_calls(self) -> None:
        proxy_environment = {
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://localhost:7890",
        }
        with mock.patch.dict(os.environ, proxy_environment, clear=False):
            MODULE.configure_network_environment()
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertEqual(os.environ["NO_PROXY"], "*")
            self.assertEqual(os.environ["no_proxy"], "*")

    def test_sts_network_error_is_wrapped_as_seedance_error(self) -> None:
        class FailingSts:
            def set_ak(self, _: str) -> None:
                return

            def set_sk(self, _: str) -> None:
                return

            def assume_role(self, _: dict[str, str]) -> dict[str, object]:
                raise TimeoutError("proxy timeout")

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
        with mock.patch(
            "volcengine.sts.StsService.StsService",
            return_value=FailingSts(),
        ):
            publisher = MODULE.TosPublisher(config, 3600)
            with self.assertRaisesRegex(MODULE.SeedanceError, "AssumeRole 失败"):
                publisher.client()

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
            "model": MODULE.MODEL_BY_SEGMENT_MAX_SECONDS[15],
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

    def test_submit_rejects_model_that_does_not_match_segment_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, segment_plan = self.write_single_segment_inputs(root)
            prompt.write_text("镜头1[00:00-00:10] 展示。\n", encoding="utf-8")
            prepare_args = self.make_prepare_args(root, source, prompt, segment_plan)
            with mock.patch.object(
                MODULE, "probe_video", return_value=self.metadata(has_audio=False)
            ):
                plan_path = MODULE.prepare(prepare_args)

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["model"] = MODULE.MODEL_BY_SEGMENT_MAX_SECONDS[30]
            MODULE.atomic_write_json(plan_path, plan)
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

            with self.assertRaisesRegex(MODULE.SeedanceError, "模型与最大分段时长不匹配"):
                MODULE.submit(submit_args)

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

    def test_submit_resume_does_not_create_duplicate_task(self) -> None:
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
                legacy_plan = json.loads(plan_path.read_text(encoding="utf-8"))
                legacy_plan.pop("fact_lock")
                plan_path.write_text(json.dumps(legacy_plan), encoding="utf-8")
                MODULE.submit(submit_args, client_factory=lambda _: client)

            self.assertEqual(tasks.create_count, 1)
            self.assertEqual(tasks.get_count, 1)
            state = json.loads(
                plan_path.with_name("tasks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["segments"]["1"]["status"], "downloaded")
            self.assertEqual(state["full_output"]["status"], "complete")

    def test_submit_uses_private_asset_uri_for_virtual_character(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, segment_plan = self.write_single_segment_inputs(root)
            prompt.write_text(
                "参考@图片1中的虚拟人物，将其定义为<主播>。\n"
                "镜头1[00:00-00:10] <主播>展示产品。\n",
                encoding="utf-8",
            )
            character = root / "character.png"
            Image.new("RGB", (720, 1280), "blue").save(character)
            prepare_args = self.make_prepare_args(
                root, source, prompt, segment_plan
            )
            prepare_args.character_image = character
            prepare_args.character_image_type = "virtual"
            prepare_args.confirm_virtual_portrait_rights = True
            with mock.patch.object(
                MODULE, "probe_video", return_value=self.metadata(has_audio=False)
            ):
                plan_path = MODULE.prepare(prepare_args)

            class FakePublisher:
                def upload(
                    self, _: Path, __: str, asset_id: str
                ) -> dict[str, object]:
                    return {
                        "object_key": asset_id,
                        "url": f"https://tos.example.com/{asset_id}",
                        "url_expires_at": 2**31 - 1,
                    }

                def sign(self, object_key: str) -> dict[str, object]:
                    return {
                        "object_key": object_key,
                        "url": f"https://tos.example.com/{object_key}",
                        "url_expires_at": 2**31 - 1,
                    }

            class FakeAssetLibrary:
                def create_group(self, _: str, __: str) -> str:
                    return "group-private"

                def create_asset(self, _: str, __: str, ___: str) -> str:
                    return "asset-private"

                def get_asset(self, _: str) -> dict[str, str]:
                    return {"Status": "Active", "ProjectName": "default"}

            tasks = FakeTasks()
            client = SimpleNamespace(content_generation=SimpleNamespace(tasks=tasks))
            captured_configs: dict[str, dict[str, str]] = {}
            submit_args = SimpleNamespace(
                plan=plan_path,
                ark_api_key_file=None,
                tos_config_file=None,
                poll_interval=0.0,
                poll_timeout=5.0,
                signed_url_ttl=3600,
                retry_failed=False,
                allow_recreate_ambiguous=False,
                asset_project_name="default",
                asset_poll_interval=0.0,
                asset_poll_timeout=5.0,
            )

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
                mock.patch.dict(
                    os.environ,
                    {
                        "ARK_API_KEY": "ark-test-key-value",
                        "ARK_ACCESS_KEY": "asset-ak",
                        "ARK_SECRET_KEY": "asset-sk",
                    },
                ),
                mock.patch.object(
                    MODULE,
                    "load_tos_config",
                    return_value={
                        "access_key": "ak",
                        "secret_key": "sk",
                        "region": "cn-beijing",
                    },
                ),
                mock.patch.object(MODULE, "download_video", side_effect=fake_download),
                mock.patch.object(MODULE, "validate_generated_video"),
                mock.patch.object(
                    MODULE, "concat_generated_videos", side_effect=fake_concat
                ),
            ):
                MODULE.submit(
                    submit_args,
                    client_factory=lambda _: client,
                    publisher_factory=lambda config, _ttl: (
                        captured_configs.setdefault("tos", dict(config))
                        and FakePublisher()
                    ),
                    asset_library_factory=lambda config, _project: (
                        captured_configs.setdefault("asset", dict(config))
                        and FakeAssetLibrary()
                    ),
                )

            request = json.loads(
                (plan_path.parent / "responses" / "request_part_01.json").read_text(
                    encoding="utf-8"
                )
            )
            image_item = next(
                item for item in request["content"] if item["type"] == "image_url"
            )
            self.assertEqual(
                image_item["image_url"]["url"], "asset://asset-private"
            )
            self.assertEqual(captured_configs["tos"]["access_key"], "ak")
            self.assertEqual(captured_configs["tos"]["secret_key"], "sk")
            self.assertEqual(captured_configs["asset"]["access_key"], "asset-ak")
            self.assertEqual(captured_configs["asset"]["secret_key"], "asset-sk")

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

    def test_fact_lock_accepts_restricted_static_visual_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prompt, plan = self.write_single_segment_inputs(root)
            args = self.make_prepare_args(root, source, prompt, plan)
            overrides = root / "static_visual_overrides.json"
            overrides.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "subject_definitions": {
                            "<主要人物>": "将月白色太极服人物定义为<主要人物>。"
                        },
                        "shot_overrides": [
                            {
                                "segment_index": 1,
                                "shot_index": 1,
                                "scene_light": "清雅茶室，柔和窗侧自然光",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            lock = json.loads(args.fact_lock.read_text(encoding="utf-8"))
            lock["assembly_mode"] = MODULE.STATIC_OVERRIDE_ASSEMBLY_MODE
            lock["static_visual_overrides"] = MODULE.file_identity(overrides)
            args.fact_lock.write_text(
                json.dumps(lock, ensure_ascii=False), encoding="utf-8"
            )

            validated = MODULE.validate_fact_lock_file(
                args.fact_lock, prompt, plan
            )

            self.assertEqual(
                validated["assembly_mode"], MODULE.STATIC_OVERRIDE_ASSEMBLY_MODE
            )

    def test_static_visual_overrides_reject_action_fields(self) -> None:
        with self.assertRaisesRegex(MODULE.SeedanceError, "镜头字段越权"):
            MODULE.validate_static_visual_overrides(
                {
                    "schema_version": 1,
                    "subject_definitions": {},
                    "shot_overrides": [
                        {
                            "segment_index": 1,
                            "shot_index": 1,
                            "subject_action": "改成饮茶动作",
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
