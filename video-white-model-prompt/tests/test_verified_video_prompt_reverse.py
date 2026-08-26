from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "verified_video_prompt_reverse.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("verified_video_prompt_reverse", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载：{SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifiedPromptTests(unittest.TestCase):
    @staticmethod
    def facts(camera: str = "平稳横移", action: str = "人物抬手摸脸") -> dict:
        return {
            "schema_version": 1,
            "no_speech_confirmed": True,
            "subjects": [
                {
                    "label": "<模特>",
                    "kind": "character",
                    "original_static_description": "一名长发女性",
                }
            ],
            "segments": [
                {
                    "index": 1,
                    "source_start_seconds": 0,
                    "source_end_seconds": 10,
                    "duration_seconds": 10,
                    "shots": [
                        {
                            "index": 1,
                            "start_seconds": 0,
                            "end_seconds": 10,
                            "shot_scale": "近景特写",
                            "camera": camera,
                            "composition": "人物居中",
                            "visible_body_range": "头部至胸口",
                            "subject_action": action,
                            "operator_product_action": "化妆师刷涂睫毛",
                            "entry_exit": "无主体进出场",
                            "scene_light": "浅色背景与柔和光线",
                            "audio": "",
                        }
                    ],
                }
            ],
        }

    def make_args(self, root: Path) -> SimpleNamespace:
        video = root / "video.mp4"
        video.write_bytes(b"video")
        character = root / "character.png"
        character.write_bytes(b"image")
        return SimpleNamespace(
            video=video,
            segment_max_seconds=15,
            character_image=character,
            product_image=[],
            product_name="",
            selling_points="",
            user_idea="",
            allow_audio_rewrite=False,
            spoken_replacement=[],
            transcript_file=None,
            api_key_file=None,
            base_url="https://example.invalid/v1",
            model="qwen3.8-max",
            omni_model="qwen3.5-omni-plus",
            fps=4.0,
            omni_system_prompt=SKILL_DIR / "prompts/video_reverse_omni_facts_system.txt",
            max_system_prompt=SKILL_DIR
            / "prompts/video_reverse_max_verify_appearance_system.txt",
            omni_facts_output=root / "omni_facts.json",
            omni_facts_file=None,
            omni_metadata_output=root / "omni_meta.json",
            omni_metadata_file=None,
            draft_output=root / "prompt_draft.txt",
            verification_output=root / "max_verification.json",
            candidate_output=root / "prompt_candidate.txt",
            output=root / "prompt.txt",
            segment_plan_output=root / "segment_plan.json",
            fact_lock_output=root / "fact_lock.json",
            omni_request_body_output=None,
            omni_response_body_output=None,
            max_request_body_output=None,
            max_response_body_output=None,
            overwrite=False,
            temperature=0.3,
            max_tokens=32768,
            timeout=300,
            retries=2,
            max_inline_request_mb=9.5,
        )

    def test_audio_rewrite_context_declares_complete_override_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            args.allow_audio_rewrite = True
            messages = MODULE.build_max_messages(
                args,
                "system",
                "data:video/mp4;base64,AA==",
                self.facts(),
                10.0,
                None,
                [],
            )
            context_text = messages[1]["content"][-1]["text"]
            context = json.loads(context_text.split("：\n", 1)[1])
            permission = context["audio_permission"]
            self.assertIn("segment_index", permission)
            self.assertIn("shot_index", permission)
            self.assertIn("audio", permission)
            self.assertIn("JSON整数", permission)

            overrides = MODULE.validate_audio_overrides(
                [
                    {
                        "segment_index": 1,
                        "shot_index": 1,
                        "audio": "原创无歌词流行伴奏。",
                    }
                ],
                self.facts(),
                True,
            )
            self.assertEqual(overrides[(1, 1)], "原创无歌词流行伴奏。")

    def test_max_correction_must_match_every_fact_difference(self) -> None:
        omni = MODULE.validate_facts(self.facts(), 10, 15)
        verified = MODULE.validate_facts(
            self.facts(camera="固定机位", action="人物正对镜头，仅轻微眨眼"),
            10,
            15,
        )
        differences = MODULE.visual_differences(omni, verified, 10.0)
        self.assertEqual(
            set(differences),
            {
                "segments[0].shots[0].camera",
                "segments[0].shots[0].subject_action",
            },
        )
        review = {
            "status": "corrected",
            "corrections": [
                {
                    "path": path,
                    "omni_value": before,
                    "corrected_value": after,
                    "evidence_times": [interval[0], interval[1]],
                    "evidence_description": "原片对应时间点可见",
                }
                for path, (before, after, interval) in differences.items()
            ],
        }
        corrections = MODULE.validate_corrections(review, differences)
        self.assertEqual(len(corrections), 2)

    def test_unexplained_fact_change_is_rejected(self) -> None:
        omni = MODULE.validate_facts(self.facts(), 10, 15)
        verified = MODULE.validate_facts(self.facts(camera="固定机位"), 10, 15)
        body = {
            "fact_review": {"status": "unchanged", "corrections": []},
            "verified_source_facts": verified,
            "appearance_bindings": [{"label": "<模特>", "image_refs": []}],
            "audio_overrides": [],
        }
        with self.assertRaisesRegex(MODULE.ScriptError, "未逐项解释"):
            MODULE.validate_max_result(body, omni, 10, 15, False, 0, False, 10.0)

    def test_free_text_appearance_and_wrong_image_role_are_rejected(self) -> None:
        facts = MODULE.validate_facts(self.facts(camera="固定机位"), 10, 15)
        with self.assertRaisesRegex(MODULE.ScriptError, "必须是对象"):
            MODULE.validate_appearance_bindings(
                [
                    {
                        "label": "<模特>",
                        "definition": "正在转身离场的短发女性",
                    }
                ],
                facts,
                True,
                0,
            )
        product_facts = deepcopy(facts)
        product_facts["subjects"].append(
            {
                "label": "<产品>",
                "kind": "product",
                "original_static_description": "一支睫毛膏",
            }
        )
        with self.assertRaisesRegex(MODULE.ScriptError, "人物图"):
            MODULE.validate_appearance_bindings(
                [
                    {"label": "<模特>", "image_refs": []},
                    {"label": "<产品>", "image_refs": [1]},
                ],
                product_facts,
                True,
                0,
            )

    def test_nan_evidence_is_rejected(self) -> None:
        omni = MODULE.validate_facts(self.facts(), 10, 15)
        verified = MODULE.validate_facts(self.facts(camera="固定机位"), 10, 15)
        differences = MODULE.visual_differences(omni, verified, 10.0)
        path, (before, after, _) = next(iter(differences.items()))
        with self.assertRaisesRegex(MODULE.ScriptError, "有限数"):
            MODULE.validate_corrections(
                {
                    "status": "corrected",
                    "corrections": [
                        {
                            "path": path,
                            "omni_value": before,
                            "corrected_value": after,
                            "evidence_times": [0, float("nan")],
                            "evidence_description": "证据",
                        }
                    ],
                },
                differences,
            )
        with self.assertRaisesRegex(MODULE.ScriptError, "必须是数字"):
            MODULE.validate_corrections(
                {
                    "status": "corrected",
                    "corrections": [
                        {
                            "path": path,
                            "omni_value": before,
                            "corrected_value": after,
                            "evidence_times": [0, None],
                            "evidence_description": "证据",
                        }
                    ],
                },
                differences,
            )

    def test_max_can_correct_shot_structure_inside_locked_segment(self) -> None:
        omni = MODULE.validate_facts(self.facts(), 10, 15)
        verified_body = self.facts(camera="固定机位", action="人物正对镜头")
        original = verified_body["segments"][0]["shots"][0]
        first = {**original, "index": 1, "start_seconds": 0, "end_seconds": 5}
        second = {**original, "index": 2, "start_seconds": 5, "end_seconds": 10}
        verified_body["segments"][0]["shots"] = [first, second]
        verified = MODULE.validate_facts(verified_body, 10, 15)
        differences = MODULE.visual_differences(omni, verified, 10.0)
        self.assertIn("segments[0].shot_plan", differences)
        self.assertIn("segments[0].shot_visuals", differences)

    def test_main_uses_two_calls_and_renders_max_verified_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            omni = self.facts()
            verified = self.facts(
                camera="固定机位",
                action="人物正对镜头，仅轻微眨眼",
            )
            differences = MODULE.visual_differences(
                MODULE.validate_facts(deepcopy(omni), 10, 15),
                MODULE.validate_facts(deepcopy(verified), 10, 15),
                10.0,
            )
            max_result = {
                "fact_review": {
                    "status": "corrected",
                    "corrections": [
                        {
                            "path": path,
                            "omni_value": before,
                            "corrected_value": after,
                            "evidence_times": [interval[0], interval[1]],
                            "evidence_description": "原片对应时间点可见",
                        }
                        for path, (before, after, interval) in differences.items()
                    ],
                },
                "verified_source_facts": verified,
                "appearance_bindings": [
                    {
                        "label": "<模特>",
                        "image_refs": [1],
                    }
                ],
                "audio_overrides": [],
            }

            class FakeResolver:
                def __init__(self, _: object) -> None:
                    pass

                def resolve(self, path: Path, kind: str) -> str:
                    return f"data:{kind}/{path.name}"

            omni_mock = mock.Mock(
                return_value=(json.dumps(omni, ensure_ascii=False), {"finish_reason": "stop"})
            )
            max_mock = mock.Mock(
                return_value=(
                    json.dumps(max_result, ensure_ascii=False),
                    {"choices": [{"finish_reason": "stop"}]},
                )
            )
            with (
                mock.patch.object(MODULE, "parse_args", return_value=args),
                mock.patch.object(
                    MODULE,
                    "probe_video",
                    return_value={
                        "duration_seconds": 10,
                        "source_duration": 10.0,
                        "has_audio": False,
                    },
                ),
                mock.patch.object(MODULE, "validate_video_api_limits"),
                mock.patch.object(MODULE, "validate_image_api_limits"),
                mock.patch.object(MODULE, "MediaResolver", FakeResolver),
                mock.patch.object(MODULE, "call_omni", omni_mock),
                mock.patch.object(MODULE, "call_qwen", max_mock),
                mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test-value"}),
            ):
                code = MODULE.main()

            self.assertEqual(code, 0)
            omni_mock.assert_called_once()
            max_mock.assert_called_once()
            max_payload = max_mock.call_args.args[2]
            self.assertEqual(max_payload["messages"][1]["content"][0]["type"], "video_url")
            prompt = args.output.read_text(encoding="utf-8")
            self.assertIn("固定机位", prompt)
            self.assertIn("仅轻微眨眼", prompt)
            self.assertNotIn("平稳横移", prompt)
            self.assertNotIn("抬手摸脸", prompt)


if __name__ == "__main__":
    unittest.main()
