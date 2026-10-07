from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import qwen_video_prompt_reverse as qwen
import verified_video_prompt_reverse as verified
import seedance_video_pipeline as seedance
import video_prompt_format as public


def facts() -> dict:
    subjects = [
        {"label": "<演示女子>", "kind": "character", "original_static_description": "短发、穿白衣的女子"},
        {"label": "<桌面场景>", "kind": "scene", "original_static_description": "窗旁木桌，背景为白墙"},
        {"label": "<展示罐>", "kind": "product", "original_static_description": "圆形金属罐，包装上有可辨认的品牌字样"},
    ]
    shots = []
    for i, (start, end) in enumerate(((0, 0.4), (0.4, 0.9), (0.9, 10)), start=1):
        action = ["<演示女子>托起<展示罐>，嘴部自然说话", "<演示女子>旋开<展示罐>盖", "<演示女子>将<展示罐>放回木桌，动作在画面结束时仍在进行"][i - 1]
        shots.append({
            "index": i, "start_seconds": start, "end_seconds": end,
            "shot_scale": "近景", "camera": "固定机位，硬切衔接",
            "composition": "<演示女子>位于画面左侧，<展示罐>在右侧",
            "visible_body_range": "双手与上半身", "scene_light": "窗边侧光",
            "subject_action": action, "operator_product_action": "操作<展示罐>",
            "entry_exit": "无主体进出场",
            "beats": [{"index": 1, "start_seconds": start, "end_seconds": end, "action": action}],
            "audio": [
                "<演示女子>（画内、口型同步）说：{这款}",
                "<演示女子>（画内、口型同步）说：{产品。}<金属盖旋开的轻响>",
                "(钢琴配乐逐渐减弱)<窗外持续的雨声>",
            ][i - 1],
        })
    return {
        "schema_version": 3, "no_speech_confirmed": False,
        "overall_av": {"visual": "二维手绘动画，低饱和色彩", "audio": "普通话女声自然讲述，钢琴配乐与雨声持续铺底"},
        "subjects": subjects,
        "segments": [{"index": 1, "source_start_seconds": 0, "source_end_seconds": 10, "duration_seconds": 10, "shots": shots}],
    }


def render(body: dict) -> str:
    return verified.render_prompt(body, verified.default_definitions(body), {})


class PublicPromptTests(unittest.TestCase):
    def test_quick_cuts_keep_precise_facts_and_spoken_fragments(self):
        body = verified.validate_facts(facts(), 10, 15)
        prompt = render(body)
        plan = qwen.validate_prompt_contract(prompt, 10, 10.0, 15, 0, "test", facts=body)
        self.assertEqual(len(plan["shot_timelines"][0]["shots"]), 3)
        self.assertEqual(plan["shot_timelines"][0]["shots"][0]["end_seconds"], 0.4)
        self.assertIn("镜头1｜00:00 - 00:01", prompt)
        self.assertIn("镜头2｜00:00 - 00:01", prompt)
        self.assertEqual(verified.dialogue_texts(prompt), ["这款", "产品。"])
        self.assertIn("动作在画面结束时仍在进行", prompt)
        self.assertIn("二维手绘动画", prompt)
        self.assertNotIn("真实摄影", prompt)
        self.assertNotIn("<演示女子>", prompt)
        self.assertIn("<金属盖旋开的轻响>", prompt)
        self.assertIn("(钢琴配乐逐渐减弱)", prompt)
        self.assertLess(prompt.index("定义为展示罐"), prompt.index("定义为演示女子"))
        self.assertLess(prompt.index("定义为演示女子"), prompt.index("定义为桌面场景"))
        self.assertIn("包装上有可辨认的品牌字样", prompt)
        merged = prompt.replace("镜头2｜00:00 - 00:01", "镜头2｜00:00 - 00:02")
        with self.assertRaisesRegex(qwen.ScriptError, "锁定事实不一致"):
            qwen.validate_prompt_contract(merged, 10, 10.0, 15, 0, "test", facts=body)

    def test_precise_timeline_still_rejects_invalid_facts(self):
        for value in (float("nan"), float("inf"), True, "0.4", -0.1, 0.5):
            body = facts()
            body["segments"][0]["shots"][1]["start_seconds"] = value
            with self.subTest(value=value), self.assertRaises(verified.ScriptError):
                verified.validate_facts(body, 10, 15)
        body = facts()
        body["segments"][0]["shots"][0]["beats"][0]["end_seconds"] = 0.3
        with self.assertRaisesRegex(verified.ScriptError, "完整镜头时长"):
            verified.validate_facts(body, 10, 15)

    def test_global_av_is_verified_with_evidence(self):
        before = verified.validate_facts(facts(), 10, 15)
        after = deepcopy(before)
        after["overall_av"]["visual"] = "三维动画，低饱和色彩"
        differences = verified.visual_differences(before, after, 10.0)
        self.assertEqual(set(differences), {"overall_av"})
        allowed = verified.evidence_time_index(before, 10.0)["overall_av"]
        result = verified.resolve_corrections({"status": "unchanged", "corrections": []}, differences)
        self.assertEqual(result[0]["corrected_value"], after["overall_av"])
        self.assertTrue(set(result[0]["evidence_times"]).issubset(allowed))

    def test_reference_injection_keeps_three_sections_and_binding_roles(self):
        body = verified.validate_facts(facts(), 10, 15)
        bindings = {"<演示女子>": [1], "<桌面场景>": [], "<展示罐>": [2]}
        prompt = verified.render_prompt(body, verified.definitions_from_bindings(body, bindings), {})
        for kind in ("depth", "openpose"):
            with self.subTest(kind=kind):
                compiled = seedance.compile_prompt(
                    prompt, 3, True, with_reference_audio=True, with_character_image=True,
                    motion_reference_type=kind, effect_image_indices=[3], effect_reference_scope="只参考肤色与皮肤纹理",
                )
                definitions, overall, shots = public.split_sections(compiled)
                self.assertTrue(compiled.startswith(public.HEADINGS[0]))
                self.assertIn("@图片1", definitions)
                self.assertIn("@图片2", definitions)
                self.assertIn("@图片3", definitions)
                self.assertIn("@视频1", definitions)
                self.assertIn("@音频1", overall)
                self.assertNotIn("@音频1", definitions)
                self.assertEqual(verified.dialogue_texts(shots), ["这款", "产品。"])
                public.validate_body(compiled, 10, body["segments"][0])

    def test_visual_only_removes_audio_but_keeps_acting_and_global_style(self):
        body = verified.validate_facts(facts(), 10, 15)
        compiled = seedance.compile_prompt(render(body), 0, False, strip_dialogue_for_visual_only=True)
        definitions, overall, shots = public.split_sections(compiled)
        self.assertIn("二维手绘动画", overall)
        self.assertIn("嘴部自然说话", shots)
        self.assertIn("旋开展示罐盖", shots)
        for removed in ("{这款}", "{产品。}", "金属盖旋开的轻响", "钢琴配乐", "普通话女声", "持续的雨声"):
            self.assertNotIn(removed, compiled)
        self.assertEqual(len(public.SHOT_PATTERN.findall(shots)), 3)
        self.assertNotIn("@视频1", compiled)

    def test_literal_dialogue_characters_are_untouched(self):
        body = facts()
        original = "()、<演示女子>、〖字〗都是原文；音乐：是台词。"
        body["segments"][0]["shots"][0]["audio"] = "<演示女子>（画内、口型同步）说：{" + original + "}"
        body = verified.validate_facts(body, 10, 15)
        prompt = render(body)
        self.assertEqual(verified.dialogue_texts(prompt)[0], original)
        public.validate_body(prompt, 10, body["segments"][0])

    def test_continuous_speech_merges_without_erasing_interruptions(self):
        prefix = "<演示女子>（画内、口型同步）说："
        audio = prefix + "{第一句。}；" + prefix + "{第二句。}。停顿后，" + prefix + "{第三句。}"
        result = public.render_audio(audio, ["<演示女子>"])
        self.assertEqual(verified.dialogue_texts(result), ["第一句。第二句。", "第三句。"])
        self.assertIn("停顿后", result)

    def test_requested_captions_use_separate_policy(self):
        body = facts()
        body["segments"][0]["shots"][0]["beats"][0]["action"] += "；00:00 - 00:01，底部白色字幕显示〖这款〗"
        body = verified.validate_facts(body, 10, 15)
        prompt = render(body)
        self.assertNotIn("全片不生成字幕", prompt)
        self.assertIn("按分镜中的字幕标记", prompt)
        public.validate_body(prompt, 10)

    def test_multisegment_prompts_reset_display_time_only(self):
        body = facts()
        second = deepcopy(body["segments"][0])
        second.update(index=2, source_start_seconds=10, source_end_seconds=20)
        body["segments"].append(second)
        body = verified.validate_facts(body, 20, 15)
        prompt = render(body)
        plan = qwen.validate_prompt_contract(prompt, 20, 20.0, 15, 0, "test", facts=body)
        pieces = seedance.split_prompt(prompt, plan["segments"])
        self.assertEqual(plan["split_times_seconds"], [10.0])
        self.assertEqual(len(pieces), 2)
        for piece in pieces:
            public.validate_body(piece, 10)
            self.assertNotIn("对齐参考视频", piece)
            self.assertIn("镜头1｜00:00 - 00:01", piece)
        self.assertEqual([item["segment_index"] for item in plan["shot_timelines"]], [1, 2])
        body["segments"][0]["shots"][0]["beats"][0]["action"] += "；底部字幕〖这款〗"
        captioned = render(body)
        qwen.validate_prompt_contract(captioned, 20, 20.0, 15, 0, "test", facts=body)
        first, second = seedance.split_prompt(captioned, plan["segments"])
        self.assertNotIn("全片不生成字幕", first)
        self.assertIn("全片不生成字幕", second)

    def test_model_default_and_video_sampling(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "argv", ["qwen", "--video", "unused.mp4", "--segment-max-seconds", "15"]):
            self.assertEqual(qwen.parse_args().omni_model, "qwen3.8-omni-flash")
        self.assertEqual(verified.DEFAULT_OMNI_MODEL, "qwen3.8-omni-flash")
        messages = verified.build_omni_messages("system", "data:video/mp4;base64,AA==", 10, 15, True, "", fps=2)
        self.assertEqual(messages[1]["content"][0]["fps"], 2)


if __name__ == "__main__":
    unittest.main()
