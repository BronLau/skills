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

    @staticmethod
    def speech_facts(audio: str | None = None, schema_version: int = 2) -> dict:
        return {
            "schema_version": schema_version,
            "no_speech_confirmed": False,
            "subjects": [
                {
                    "label": "<女顾客>",
                    "kind": "character",
                    "original_static_description": "一名坐着的短发女性",
                },
                {
                    "label": "<男发型师>",
                    "kind": "operator",
                    "original_static_description": "一名站在女顾客身后的男性",
                },
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
                            "shot_scale": "中景",
                            "camera": "固定机位",
                            "composition": "女顾客居中，男发型师站在后方",
                            "visible_body_range": "两人上半身",
                            "subject_action": "女顾客先说话，男发型师随后回答",
                            "operator_product_action": "男发型师手持喷雾瓶",
                            "entry_exit": "无主体进出场",
                            "scene_light": "明亮的室内环境",
                            "audio": audio
                            or (
                                "<女顾客>（画内、口型同步）说：{第一句}；"
                                "此时<男发型师>自然闭口聆听。"
                                "随后<男发型师>（画内、口型同步）回答："
                                "{第二句}；此时<女顾客>自然闭口聆听。"
                                "音效：瓶身摇晃声。"
                            ),
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
            reuse_candidate=False,
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
            self.assertIn("口型同步", permission)
            self.assertIn("自然闭口", permission)

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

            with self.assertRaisesRegex(MODULE.ScriptError, "不会提交给 Seedance"):
                MODULE.validate_audio_overrides(
                    [
                        {
                            "segment_index": 1,
                            "shot_index": 1,
                            "audio": "原创伴奏，保留原片节奏。",
                        }
                    ],
                    self.facts(),
                    True,
                )

    def test_schema_v2_audio_requires_speaker_mouth_and_listener_binding(self) -> None:
        facts = MODULE.validate_facts(self.speech_facts(), 10, 15)
        prompt = MODULE.render_prompt(
            facts,
            MODULE.default_definitions(facts),
            {},
        )
        self.assertEqual(facts["schema_version"], 2)
        self.assertIn("<女顾客>（画内、口型同步）说：{第一句}", prompt)
        self.assertIn("<男发型师>自然闭口聆听", prompt)
        self.assertIn("音效：瓶身摇晃声", prompt)

        invalid_audio = (
            ("{第一句} {第二句}", "每句.*说话人"),
            ("<女顾客>说：“第一句”", r"必须使用.*\{\}"),
            (
                "<路人>（画内、口型同步）说：{第一句}",
                "未定义说话人",
            ),
            (
                "<女顾客>（画内、口型同步）说：{第一句}；"
                "<男发型师>（画内、口型同步）回答：{第二句}",
                "多人对话",
            ),
        )
        for audio, error in invalid_audio:
            with self.subTest(audio=audio):
                with self.assertRaisesRegex(MODULE.ScriptError, error):
                    MODULE.validate_facts(self.speech_facts(audio), 10, 15)

        legacy = self.speech_facts("{第一句} {第二句}", schema_version=1)
        self.assertEqual(MODULE.validate_facts(legacy, 10, 15)["schema_version"], 1)

    def test_audio_override_reuses_strict_attribution_contract(self) -> None:
        facts = MODULE.validate_facts(self.speech_facts(), 10, 15)
        valid = MODULE.validate_audio_overrides(
            [
                {
                    "segment_index": 1,
                    "shot_index": 1,
                    "audio": (
                        "<女顾客>（画内、口型同步）说：{改写后第一句}；"
                        "此时<男发型师>自然闭口聆听。"
                    ),
                }
            ],
            facts,
            True,
        )
        self.assertIn("改写后第一句", valid[(1, 1)])

        for audio, error in (
            ("{第一句}", "每句.*说话人"),
            ("女顾客说：“第一句”", r"必须使用.*\{\}"),
            ("原创伴奏。", "丢失了原镜头台词"),
        ):
            with self.subTest(audio=audio):
                with self.assertRaisesRegex(MODULE.ScriptError, error):
                    MODULE.validate_audio_overrides(
                        [
                            {
                                "segment_index": 1,
                                "shot_index": 1,
                                "audio": audio,
                            }
                        ],
                        facts,
                        True,
                    )

        removed = MODULE.validate_audio_overrides(
            [
                {
                    "segment_index": 1,
                    "shot_index": 1,
                    "audio": "无对白。环境声：安静的室内底噪。",
                }
            ],
            facts,
            True,
        )
        self.assertIn("无对白", removed[(1, 1)])

    def test_max_context_declares_aggregate_correction_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
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
            contract = context["correction_path_contract"]

            self.assertEqual(
                contract["shot_plan"]["path"],
                "segments[i].shot_plan",
            )
            self.assertEqual(
                contract["shot_visuals"]["path"],
                "segments[i].shot_visuals",
            )
            self.assertIn(
                "segments[i].shots[j].end_seconds",
                contract["shot_plan"]["forbidden_paths"],
            )
            self.assertEqual(
                contract["shot_visuals"]["value_fields"],
                [*MODULE.VISUAL_FIELDS, "beats"],
            )
            self.assertNotIn(
                "audio",
                contract["shot_visuals"]["value_fields"],
            )
            timeline = context["timeline_contract"]
            self.assertEqual(timeline["time_type"], "JSON整数")
            self.assertFalse(timeline["fractional_seconds_allowed"])
            self.assertIn(
                "segments[i].shots[j].beats[k].end_seconds",
                timeline["applies_to"],
            )

    def test_beats_are_continuous_and_rendered_inside_one_shot(self) -> None:
        body = self.facts(camera="固定机位", action="人物表情逐渐变化")
        body["segments"][0]["shots"][0]["beats"] = [
            {
                "index": 1,
                "start_seconds": 0,
                "end_seconds": 4,
                "action": "<模特>平静看向前方，化妆刷靠近上睫毛",
            },
            {
                "index": 2,
                "start_seconds": 4,
                "end_seconds": 10,
                "action": "<模特>短暂睁大眼睛，随后自然微笑",
            },
        ]
        facts = MODULE.validate_facts(body, 10, 15)
        prompt = MODULE.render_prompt(
            facts,
            MODULE.default_definitions(facts),
            {},
        )

        self.assertIn("生成目标：", prompt)
        self.assertIn("镜头1[00:00-00:10]", prompt)
        self.assertIn("动作阶段1[00:00-00:04]", prompt)
        self.assertIn("动作阶段2[00:04-00:10]", prompt)
        self.assertEqual(prompt.count("镜头1["), 1)
        self.assertIn("全片约束：", prompt)

        broken = deepcopy(body)
        broken["segments"][0]["shots"][0]["beats"][1]["start_seconds"] = 5
        with self.assertRaisesRegex(MODULE.ScriptError, "beat 无效或不连续"):
            MODULE.validate_facts(broken, 10, 15)

    def test_beat_action_changes_use_one_aggregate_path(self) -> None:
        omni_body = self.facts(camera="固定机位")
        omni_body["segments"][0]["shots"][0]["beats"] = [
            {
                "index": 1,
                "start_seconds": 0,
                "end_seconds": 4,
                "action": "刷涂人物右眼睫毛",
            },
            {
                "index": 2,
                "start_seconds": 4,
                "end_seconds": 10,
                "action": "刷头离开画面",
            },
        ]
        verified_body = deepcopy(omni_body)
        verified_body["segments"][0]["shots"][0]["beats"][0]["action"] = (
            "刷涂人物左眼睫毛"
        )

        omni = MODULE.validate_facts(omni_body, 10, 15)
        verified = MODULE.validate_facts(verified_body, 10, 15)
        differences = MODULE.visual_differences(omni, verified, 10.0)

        self.assertEqual(
            set(differences),
            {"segments[0].shots[0].beat_actions"},
        )

    def test_beat_plan_and_actions_are_independent_corrections(self) -> None:
        omni_body = self.facts(camera="固定机位")
        omni_body["segments"][0]["shots"][0]["beats"] = [
            {
                "index": 1,
                "start_seconds": 0,
                "end_seconds": 4,
                "action": "刷涂人物右眼睫毛",
            },
            {
                "index": 2,
                "start_seconds": 4,
                "end_seconds": 10,
                "action": "刷头离开画面",
            },
        ]
        verified_body = deepcopy(omni_body)
        verified_beats = verified_body["segments"][0]["shots"][0]["beats"]
        verified_beats[0]["end_seconds"] = 5
        verified_beats[0]["action"] = "刷涂人物左眼睫毛"
        verified_beats[1]["start_seconds"] = 5

        omni = MODULE.validate_facts(omni_body, 10, 15)
        verified = MODULE.validate_facts(verified_body, 10, 15)
        differences = MODULE.visual_differences(omni, verified, 10.0)

        self.assertEqual(
            set(differences),
            {
                "segments[0].shots[0].beat_plan",
                "segments[0].shots[0].beat_actions",
            },
        )

    def test_image_binding_is_explicit_and_limits_reference_scope(self) -> None:
        facts = MODULE.validate_facts(self.facts(), 10, 15)
        definitions = MODULE.definitions_from_bindings(facts, {"<模特>": [1]})
        definition = definitions["<模特>"]

        self.assertTrue(definition.startswith("@图片1是<模特>的静态外观参考"))
        self.assertIn("不参考图片中的姿态、动作、景别、机位或拼版布局", definition)
        self.assertIn("各视图共同定义同一主体，不生成多个副本", definition)
        self.assertIn("全文统一称为<模特>", definition)

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
        self.assertEqual(
            differences["segments[0].shot_plan"][2],
            (0.0, 5.0, 10.0),
        )

    def test_shot_plan_evidence_includes_shot_and_beat_sample_points(self) -> None:
        body = self.facts(camera="固定机位")
        original = body["segments"][0]["shots"][0]
        first = {
            **original,
            "index": 1,
            "start_seconds": 0,
            "end_seconds": 4,
            "beats": [
                {
                    "index": 1,
                    "start_seconds": 0,
                    "end_seconds": 4,
                    "action": "人物保持正面姿态",
                }
            ],
        }
        second = {
            **original,
            "index": 2,
            "start_seconds": 4,
            "end_seconds": 10,
            "beats": [
                {
                    "index": 1,
                    "start_seconds": 4,
                    "end_seconds": 7,
                    "action": "人物抬手",
                },
                {
                    "index": 2,
                    "start_seconds": 7,
                    "end_seconds": 10,
                    "action": "人物放下手",
                },
            ],
        }
        body["segments"][0]["shots"] = [first, second]
        facts = MODULE.validate_facts(body, 10, 15)
        allowed = MODULE.evidence_time_index(facts, 10.0)[
            "segments[0].shot_plan"
        ]

        self.assertIn(2.0, allowed)
        self.assertIn(4.0, allowed)
        self.assertIn(5.5, allowed)
        self.assertIn(7.0, allowed)
        self.assertIn(8.5, allowed)

    def test_shot_field_evidence_includes_internal_beat_sample_points(self) -> None:
        body = self.facts(camera="固定机位")
        body["segments"][0]["shots"][0]["beats"] = [
            {
                "index": 1,
                "start_seconds": 0,
                "end_seconds": 4,
                "action": "刷涂睫毛",
            },
            {
                "index": 2,
                "start_seconds": 4,
                "end_seconds": 10,
                "action": "展示刷后效果",
            },
        ]
        facts = MODULE.validate_facts(body, 10, 15)
        index = MODULE.evidence_time_index(facts, 10.0)

        for path in (
            "segments[0].shots[0]",
            "segments[0].shots[0].beat_actions",
        ):
            self.assertEqual(index[path], [0.0, 2.0, 4.0, 5.0, 7.0, 10.0])

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
            self.assertIn("@图片1是<模特>的静态外观参考", prompt)
            self.assertIn("生成目标：", prompt)
            self.assertIn("动作阶段1[00:00-00:10]", prompt)
            self.assertIn("固定机位", prompt)
            self.assertIn("仅轻微眨眼", prompt)
            self.assertNotIn("平稳横移", prompt)
            self.assertNotIn("抬手摸脸", prompt)

    def test_main_reuses_valid_saved_max_candidate_without_api_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.reuse_candidate = True
            args.overwrite = True
            omni = MODULE.validate_facts(self.facts(), 10, 15)
            verified = MODULE.validate_facts(
                self.facts(camera="固定机位", action="人物正对镜头，仅轻微眨眼"),
                10,
                15,
            )
            differences = MODULE.visual_differences(omni, verified, 10.0)
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
                "appearance_bindings": [{"label": "<模特>", "image_refs": [1]}],
                "audio_overrides": [],
            }
            args.omni_facts_output.write_text(
                json.dumps(omni, ensure_ascii=False), encoding="utf-8"
            )
            args.omni_facts_file = args.omni_facts_output
            args.omni_metadata_file = args.omni_metadata_output
            args.omni_metadata_output.write_text(
                json.dumps(MODULE.input_metadata(args, args.video, None, 10)),
                encoding="utf-8",
            )
            args.candidate_output.write_text(
                json.dumps(max_result, ensure_ascii=False), encoding="utf-8"
            )

            class FakeResolver:
                def __init__(self, _: object) -> None:
                    pass

                def resolve(self, path: Path, kind: str) -> str:
                    return f"data:{kind}/{path.name}"

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
                mock.patch.object(
                    MODULE,
                    "call_omni",
                    side_effect=AssertionError("已有 Omni 事实时不应调用 Omni"),
                ),
                mock.patch.object(
                    MODULE,
                    "call_qwen",
                    side_effect=AssertionError("有效候选不应重复调用 Max"),
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                code = MODULE.main()

            self.assertEqual(code, 0)
            self.assertTrue(args.verification_output.is_file())
            self.assertTrue(args.fact_lock_output.is_file())
            self.assertIn("固定机位", args.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
