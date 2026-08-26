from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from copy import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "qwen_video_prompt_reverse.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("qwen_video_prompt_reverse", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载测试目标：{SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeChunk:
    def __init__(
        self,
        content: str = "",
        usage: object | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.choices = (
            []
            if not content
            else [
                SimpleNamespace(
                    delta=SimpleNamespace(content=content),
                    finish_reason=finish_reason,
                )
            ]
        )
        self.usage = usage

    def model_dump(self, **_: object) -> dict[str, object]:
        return {"content": self.choices[0].delta.content if self.choices else ""}


class RetryableError(RuntimeError):
    status_code = 429


class FakeDashScopeHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        model = payload["model"]
        if "omni" in model:
            draft = (
                "[[NO_SPEECH_CONFIRMED]]\n"
                "镜头1[00:00-00:10] 本地 SSE 视听初稿"
            )
            chunks = [
                {
                    "id": "chatcmpl-omni",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": draft},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-omni",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                },
            ]
            body = (
                "".join(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    for chunk in chunks
                )
                + "data: [DONE]\n\n"
            )
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        final = "镜头1[00:00-00:10] 本地 HTTP 最终精修稿"
        response = {
            "id": "chatcmpl-max",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": final},
                    "finish_reason": "stop",
                }
            ],
        }
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class PipelinePromptTests(unittest.TestCase):
    def make_args(self, root: Path) -> SimpleNamespace:
        video = root / "input.mp4"
        video.write_bytes(b"fake-video")
        return SimpleNamespace(
            video=video,
            system_prompt=SKILL_DIR / "prompts" / "video_reverse_system_prompt.txt",
            omni_draft_addendum=(
                SKILL_DIR / "prompts" / "video_reverse_omni_draft_addendum.txt"
            ),
            max_refine_addendum=(
                SKILL_DIR / "prompts" / "video_reverse_max_refine_addendum.txt"
            ),
            api_key_file=None,
            base_url="https://example.invalid/compatible-mode/v1",
            model="qwen3.8-max",
            omni_model="qwen3.5-omni-plus",
            fps=4.0,
            aspect_ratio=None,
            duration_seconds=None,
            segment_max_seconds=15,
            character_image=None,
            product_image=[],
            product_name="",
            selling_points="",
            user_idea="",
            allow_audio_rewrite=False,
            spoken_replacement=[],
            transcript_file=None,
            output=root / "prompt.txt",
            draft_output=root / "prompt_draft.txt",
            draft_file=None,
            draft_metadata_output=root / "prompt_draft_meta.json",
            draft_metadata_file=None,
            candidate_output=root / "prompt_candidate.txt",
            draft_candidate_output=root / "prompt_draft_candidate.txt",
            request_body_output=None,
            response_body_output=None,
            omni_request_body_output=None,
            omni_response_body_output=None,
            segment_plan_output=root / "segment_plan.json",
            overwrite=False,
            temperature=0.6,
            max_tokens=32768,
            timeout=300,
            retries=2,
            max_inline_request_mb=9.5,
        )

    @staticmethod
    def metadata(has_audio: bool) -> dict[str, object]:
        return {
            "aspect_ratio": "9:16",
            "source_duration": 10.0,
            "duration_seconds": 10,
            "has_audio": has_audio,
        }

    def test_context_routes_target_generator_by_segment_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            fifteen_second_context = MODULE.context_lines(args, "9:16", 10)
            self.assertIn(
                "target_generator：Doubao Seedance 2.0",
                fifteen_second_context,
            )
            self.assertIn("image_reference_limit：9", fifteen_second_context)

            args.segment_max_seconds = 30
            thirty_second_context = MODULE.context_lines(args, "9:16", 10)
            self.assertIn(
                "target_generator：Doubao Seedance 2.5",
                thirty_second_context,
            )
            self.assertIn("image_reference_limit：30", thirty_second_context)

    def test_image_reference_limit_counts_character_and_product_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.character_image = root / "character.png"

            args.product_image = [root / f"product-{index}.png" for index in range(8)]
            MODULE.validate_args(args)

            args.product_image.append(root / "product-9.png")
            with self.assertRaisesRegex(MODULE.ScriptError, "Seedance 2.0.*9 张"):
                MODULE.validate_args(args)

            args.segment_max_seconds = 30
            args.product_image = [
                root / f"product-{index}.png" for index in range(29)
            ]
            MODULE.validate_args(args)

            args.product_image.append(root / "product-30.png")
            with self.assertRaisesRegex(MODULE.ScriptError, "Seedance 2.5.*30 张"):
                MODULE.validate_args(args)

    def run_main(
        self,
        args: SimpleNamespace,
        has_audio: bool,
        omni_result: str = (
            "[[NO_SPEECH_CONFIRMED]]\n"
            "镜头1[00:00-00:10] 仅含 BGM 的视听初稿"
        ),
        max_result: str = "镜头1[00:00-00:10] 最终精修内容",
        omni_finish_reason: str = "stop",
        max_finish_reason: str = "stop",
        max_repair_result: str | None = None,
    ) -> tuple[int, mock.Mock, mock.Mock]:
        omni_mock = mock.Mock(
            return_value=(
                omni_result,
                {"model": args.omni_model, "finish_reason": omni_finish_reason},
            )
        )
        def max_response(content: str) -> tuple[str, dict[str, object]]:
            return (
                content,
                {
                    "choices": [
                        {
                            "message": {"content": content},
                            "finish_reason": max_finish_reason,
                        }
                    ]
                },
            )

        max_mock = (
            mock.Mock(side_effect=[max_response(max_result), max_response(max_repair_result)])
            if max_repair_result is not None
            else mock.Mock(return_value=max_response(max_result))
        )
        metadata = self.metadata(has_audio)
        if args.duration_seconds is not None:
            metadata["source_duration"] = float(args.duration_seconds)
            metadata["duration_seconds"] = int(args.duration_seconds)
        patches = [
            mock.patch.object(MODULE, "parse_args", return_value=args),
            mock.patch.object(
                MODULE,
                "probe_video",
                return_value=metadata,
            ),
            mock.patch.object(
                MODULE, "file_to_data_url", return_value="data:video/mp4;base64,AAAA"
            ),
            mock.patch.object(MODULE, "call_omni", omni_mock),
            mock.patch.object(MODULE, "call_qwen", max_mock),
            mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-test-value"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            code = MODULE.main()
        return code, omni_mock, max_mock

    def test_audio_video_runs_omni_then_refines_with_max(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            draft = (
                "[[NO_SPEECH_CONFIRMED]]\n"
                "镜头1[00:00-00:10] 含 BGM 和音效的视听初稿"
            )
            code, omni_mock, max_mock = self.run_main(args, True, draft)

            self.assertEqual(code, 0)
            omni_mock.assert_called_once()
            max_mock.assert_called_once()
            self.assertEqual(
                args.draft_output.read_text(encoding="utf-8").strip(), draft
            )
            self.assertIn("最终精修内容", args.output.read_text(encoding="utf-8"))
            self.assertTrue(args.draft_metadata_output.is_file())
            self.assertFalse(args.candidate_output.exists())
            self.assertFalse(args.draft_candidate_output.exists())
            max_payload = max_mock.call_args.args[2]
            max_system = max_payload["messages"][0]["content"]
            max_user_content = max_payload["messages"][1]["content"]
            max_user_text = max_user_content[-1]["text"]
            self.assertIn("视觉精修终稿", max_system)
            self.assertIn(draft, max_user_text)
            self.assertIn("locked_spoken_content", max_user_text)

    def test_omni_shot_structure_error_is_delegated_to_max(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            invalid_draft = "镜头1[00:00-00:11] {原始台词}"
            repaired_final = "镜头1[00:00-00:10] {原始台词}"

            code, omni_mock, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=invalid_draft,
                max_result=repaired_final,
            )

            self.assertEqual(code, 0)
            omni_mock.assert_called_once()
            max_mock.assert_called_once()
            self.assertEqual(
                args.draft_output.read_text(encoding="utf-8").strip(),
                invalid_draft,
            )
            max_payload = max_mock.call_args.args[2]
            max_user_text = max_payload["messages"][1]["content"][-1]["text"]
            self.assertIn("draft_structure_repair", max_user_text)
            self.assertIn("超出本段时长", max_user_text)
            self.assertIn("locked_segment_plan", max_user_text)

    def test_omni_invalid_segment_headers_still_retry_omni(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.duration_seconds = 20
            invalid_draft = "镜头1[00:00-00:20] {原始台词}"

            code, omni_mock, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=invalid_draft,
            )

            self.assertEqual(code, 1)
            self.assertEqual(omni_mock.call_count, 2)
            max_mock.assert_not_called()

    def test_runtime_context_declares_exact_segments_and_available_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.segment_max_seconds = 30

            no_image_text = MODULE.build_draft_user_text(args, "9:16", 41, "")
            self.assertIn("required_segment_count：2", no_image_text)
            self.assertIn("本次必须恰好输出2段", no_image_text)
            self.assertIn(
                "available_image_references：空；所有主体使用纯文本定义。",
                no_image_text,
            )
            self.assertIn("仅用于构图推理；提示词正文不复述该比例", no_image_text)

            args.character_image = root / "character.png"
            args.product_image = [root / "product-1.png", root / "product-2.png"]
            image_text = MODULE.build_draft_user_text(args, "9:16", 41, "")
            self.assertIn("@图片1=人物形象图", image_text)
            self.assertIn("@图片2=第1张产品参考图", image_text)
            self.assertIn("@图片3=第2张产品参考图", image_text)

    def test_silent_video_skips_omni(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            code, omni_mock, max_mock = self.run_main(args, False)

            self.assertEqual(code, 0)
            omni_mock.assert_not_called()
            max_mock.assert_called_once()
            self.assertFalse(args.draft_output.exists())
            max_payload = max_mock.call_args.args[2]
            max_user_text = max_payload["messages"][1]["content"][-1]["text"]
            self.assertIn("没有音轨", max_user_text)

    def test_max_cannot_move_omni_audio_safe_split_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.duration_seconds = 20
            omni_draft = (
                "[[NO_SPEECH_CONFIRMED]]\n"
                "【第一段提示词（10秒，对齐参考视频0-10秒）】\n"
                "镜头1[00:00-00:10] 初稿第一段\n"
                "【第二段提示词（10秒，对齐参考视频10-20秒）】\n"
                "镜头1[00:00-00:10] 初稿第二段"
            )
            moved_final = (
                "【第一段提示词（9秒，对齐参考视频0-9秒）】\n"
                "镜头1[00:00-00:09] 终稿第一段\n"
                "【第二段提示词（11秒，对齐参考视频9-20秒）】\n"
                "镜头1[00:00-00:11] 终稿第二段"
            )
            code, _, _ = self.run_main(
                args,
                True,
                omni_result=omni_draft,
                max_result=moved_final,
            )

            self.assertEqual(code, 1)
            self.assertTrue(args.candidate_output.is_file())
            self.assertFalse(args.output.exists())
            self.assertFalse(args.segment_plan_output.exists())

    def test_omni_retries_retryable_status_before_content(self) -> None:
        create = mock.Mock(
            side_effect=[
                RetryableError("rate limited"),
                iter([FakeChunk("初稿正文")]),
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        payload = {
            "model": "qwen3.5-omni-plus",
            "messages": [],
            "modalities": ["text"],
            "stream": True,
        }
        with (
            mock.patch("openai.OpenAI", return_value=client),
            mock.patch.object(MODULE.time, "sleep"),
        ):
            result, response = MODULE.call_omni(
                "https://example.invalid/v1",
                "sk-test-value",
                payload,
                timeout=30,
                retries=2,
                capture_chunks=True,
            )

        self.assertEqual(result, "初稿正文")
        self.assertEqual(response["model"], "qwen3.5-omni-plus")
        self.assertEqual(create.call_count, 2)

    def test_omni_does_not_retry_after_partial_content(self) -> None:
        def broken_stream():
            yield FakeChunk("部分初稿")
            raise RetryableError("stream interrupted")

        create = mock.Mock(return_value=broken_stream())
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        payload = {
            "model": "qwen3.5-omni-plus",
            "messages": [],
            "modalities": ["text"],
            "stream": True,
        }
        with mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(MODULE.ScriptError):
                MODULE.call_omni(
                    "https://example.invalid/v1",
                    "sk-test-value",
                    payload,
                    timeout=30,
                    retries=2,
                    capture_chunks=False,
                )

        self.assertEqual(create.call_count, 1)

    def test_invalid_max_output_remains_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            invalid_max_result = (
                "镜头1[00:00-00:04] 候选第一镜头\n镜头2[00:05-00:10] 候选第二镜头"
            )
            code, _, _ = self.run_main(
                args,
                has_audio=True,
                max_result=invalid_max_result,
            )

            self.assertEqual(code, 1)
            self.assertFalse(args.output.exists())
            self.assertEqual(
                args.candidate_output.read_text(encoding="utf-8").strip(),
                invalid_max_result,
            )
            self.assertFalse(args.segment_plan_output.exists())

    def test_invalid_max_output_is_repaired_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            invalid = (
                "镜头1[00:00-00:08] 候选第一镜头\n"
                "镜头2[00:08-00:11] 候选第二镜头"
            )
            repaired = "镜头1[00:00-00:10] 修复后的完整终稿"
            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                max_result=invalid,
                max_repair_result=repaired,
            )

            self.assertEqual(code, 0)
            self.assertEqual(max_mock.call_count, 2)
            self.assertEqual(args.output.read_text(encoding="utf-8").strip(), repaired)
            repair_payload = max_mock.call_args_list[1].args[2]
            repair_text = repair_payload["messages"][-1]["content"]
            self.assertIn("超出本段时长", repair_text)

    def test_segment_overview_is_rejected_and_repaired_by_max(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            invalid = (
                "生成一条户外真实摄影风格的种草短视频，节奏轻快。\n"
                "镜头1[00:00-00:10] 完整分镜"
            )
            repaired = "镜头1[00:00-00:10] 户外真实摄影，节奏轻快。"

            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                max_result=invalid,
                max_repair_result=repaired,
            )

            self.assertEqual(code, 0)
            self.assertEqual(max_mock.call_count, 2)
            repair_payload = max_mock.call_args_list[1].args[2]
            self.assertIn(
                "镜头1前包含非主体定义内容",
                repair_payload["messages"][-1]["content"],
            )
            self.assertNotIn(
                "生成一条",
                args.output.read_text(encoding="utf-8"),
            )

    def test_alternative_segment_overview_is_rejected(self) -> None:
        result = (
            "本段整体采用户外真实摄影风格。\n"
            "镜头1[00:00-00:10] 户外人物展示。"
        )
        with self.assertRaisesRegex(MODULE.ScriptError, "非主体定义内容"):
            MODULE.validate_prompt_contract(result, 10, 10.0, 15, 0, "测试稿")

    def test_image_definition_accepts_zhong_with_or_without_de(self) -> None:
        for line in (
            "参考@图片1中具有黑色短发的女性，将其定义为<模特>。",
            "参考@图片1中的具有黑色短发的女性，将其定义为<模特>。",
        ):
            with self.subTest(line=line):
                self.assertIsNotNone(MODULE.DEFINITION_LINE_PATTERN.fullmatch(line))

    def test_omni_without_spoken_content_requires_confirmation_marker(self) -> None:
        draft = "镜头1[00:00-00:10] 轻快 BGM。"
        with self.assertRaisesRegex(MODULE.AudioFactError, "无人声确认标记"):
            MODULE.validate_omni_audio_fact_coverage(draft)
        confirmed = (
            "[[NO_SPEECH_CONFIRMED]]\n"
            "镜头1[00:00-00:10] 轻快 BGM。"
        )
        self.assertEqual(
            MODULE.validate_omni_audio_fact_coverage(confirmed),
            draft,
        )

    def test_max_cannot_change_valid_omni_shot_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            omni_draft = (
                "[[NO_SPEECH_CONFIRMED]]\n"
                "镜头1[00:00-00:04] 第一镜头。\n"
                "镜头2[00:04-00:10] 第二镜头。"
            )
            merged_final = "镜头1[00:00-00:10] 合并后的镜头。"

            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=omni_draft,
                max_result=merged_final,
            )

            self.assertEqual(code, 1)
            self.assertEqual(max_mock.call_count, 2)
            self.assertFalse(args.output.exists())

    def test_max_truncation_keeps_candidate_and_rejects_final(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            code, _, _ = self.run_main(
                args,
                has_audio=True,
                max_finish_reason="length",
            )

            self.assertEqual(code, 1)
            self.assertTrue(args.candidate_output.is_file())
            self.assertFalse(args.output.exists())
            self.assertFalse(args.segment_plan_output.exists())

    def test_omni_truncation_keeps_draft_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_finish_reason="length",
            )

            self.assertEqual(code, 1)
            max_mock.assert_not_called()
            self.assertTrue(args.draft_candidate_output.is_file())
            self.assertFalse(args.draft_output.exists())
            self.assertFalse(args.draft_metadata_output.exists())

    def test_segment_plan_rejects_wrong_reference_range(self) -> None:
        result = (
            "【第一段提示词（5秒，对齐参考视频0-6秒）】\n"
            "镜头1[00:00-00:05] 第一段\n"
            "【第二段提示词（5秒，对齐参考视频5-10秒）】\n"
            "镜头1[00:00-00:05] 第二段"
        )
        with self.assertRaises(MODULE.ScriptError):
            MODULE.build_segment_plan(result, 10, 10.0, 15)

    def test_image_reference_contract_matches_every_segment(self) -> None:
        two_segments = (
            "【第一段提示词（5秒，对齐参考视频0-5秒）】\n"
            "参考@图片1。镜头1[00:00-00:05] 第一段\n"
            "【第二段提示词（5秒，对齐参考视频5-10秒）】\n"
            "参考@图片1。镜头1[00:00-00:05] 第二段"
        )
        MODULE.validate_image_reference_contract(two_segments, 1, "测试稿")

        with self.assertRaisesRegex(MODULE.ScriptError, "实际输入不一致"):
            MODULE.validate_image_reference_contract(two_segments, 0, "测试稿")

        missing_second = two_segments.replace("参考@图片1。镜头1", "镜头1", 1)
        with self.assertRaisesRegex(MODULE.ScriptError, "实际输入不一致"):
            MODULE.validate_image_reference_contract(missing_second, 1, "测试稿")

        with self.assertRaisesRegex(MODULE.ScriptError, "缺少 @ 前缀"):
            MODULE.validate_image_reference_contract(
                "镜头1[00:00-00:10] 参考图片1。",
                1,
                "测试稿",
            )

    def test_contract_repair_messages_request_complete_rewrite(self) -> None:
        messages = [{"role": "system", "content": "system"}]
        repaired = MODULE.build_contract_repair_messages(
            messages,
            "候选正文",
            "镜头超出段时长",
            "Max 最终提示词",
        )
        self.assertEqual(repaired[-2]["role"], "assistant")
        self.assertEqual(repaired[-2]["content"], "候选正文")
        self.assertIn("镜头超出段时长", repaired[-1]["content"])
        self.assertIn("完整重写正文", repaired[-1]["content"])

        with_spoken = MODULE.build_contract_repair_messages(
            messages,
            "候选正文",
            "台词漂移",
            "Max 最终提示词",
            "locked_spoken_content：\n1. {原始台词}",
        )
        self.assertIn("1. {原始台词}", with_spoken[-1]["content"])

    def test_spoken_content_ignores_punctuation_but_rejects_changed_words(self) -> None:
        draft = "镜头1[00:00-00:10] {很多姐妹，都问我！}{看一下啊}"
        same = "镜头1[00:00-00:10] {很多姐妹都问我 看一下啊}"
        MODULE.require_same_spoken_content(draft, same)

        with self.assertRaisesRegex(MODULE.ScriptError, "改变了 Omni 初稿"):
            MODULE.require_same_spoken_content(
                draft,
                "镜头1[00:00-00:10] {很多姐妹都问我 看一下吧}",
            )

        draft_segments = (
            "【第一段提示词（5秒，对齐参考视频0-5秒）】\n"
            "镜头1[00:00-00:05] {第一段台词}\n"
            "【第二段提示词（5秒，对齐参考视频5-10秒）】\n"
            "镜头1[00:00-00:05] {第二段台词}"
        )
        moved_across_split = (
            "【第一段提示词（5秒，对齐参考视频0-5秒）】\n"
            "镜头1[00:00-00:05] {第一段台词 第二段}\n"
            "【第二段提示词（5秒，对齐参考视频5-10秒）】\n"
            "镜头1[00:00-00:05] {台词}"
        )
        with self.assertRaisesRegex(MODULE.ScriptError, "第1段"):
            MODULE.require_same_spoken_content(draft_segments, moved_across_split)

        contract = MODULE.spoken_content_contract(draft_segments)
        self.assertIn("第1段：", contract)
        self.assertIn("第2段：", contract)

    def test_api_control_literals_are_rejected_from_prompt_body(self) -> None:
        MODULE.validate_no_api_control_literals(
            "镜头1[00:00-00:10] 竖屏近景，真实摄影质感。",
            "测试稿",
        )
        for literal in ("9:16竖屏", "输出720p", "4K画质"):
            with self.subTest(literal=literal):
                with self.assertRaisesRegex(MODULE.ScriptError, "Seedance API 控制"):
                    MODULE.validate_no_api_control_literals(literal, "测试稿")

    def test_changed_audio_is_repaired_once_without_rewrite_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            draft = "镜头1[00:00-00:10] {原始台词}"
            changed = "镜头1[00:00-00:10] {修改台词}"
            repaired = "镜头1[00:00-00:10] {原始台词}"
            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=draft,
                max_result=changed,
                max_repair_result=repaired,
            )

            self.assertEqual(code, 0)
            self.assertEqual(max_mock.call_count, 2)
            repair_payload = max_mock.call_args_list[1].args[2]
            self.assertIn(
                "改变了 Omni 初稿第1段的台词原文",
                repair_payload["messages"][-1]["content"],
            )

    def test_product_name_alone_does_not_unlock_spoken_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.product_name = "收腹裤"
            draft = "镜头1[00:00-00:10] {原始台词}"
            changed = "镜头1[00:00-00:10] {修改台词}"
            repaired = "镜头1[00:00-00:10] {原始台词}"

            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=draft,
                max_result=changed,
                max_repair_result=repaired,
            )

            self.assertEqual(code, 0)
            self.assertEqual(max_mock.call_count, 2)
            first_payload = max_mock.call_args_list[0].args[2]
            first_user_text = first_payload["messages"][1]["content"][-1]["text"]
            self.assertIn("未授权改写", first_user_text)
            self.assertIn("locked_spoken_content", first_user_text)

    def test_explicit_audio_rewrite_permission_unlocks_spoken_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.product_name = "收腹裤"
            args.allow_audio_rewrite = True
            draft = "镜头1[00:00-00:10] {原始台词}"
            changed = "镜头1[00:00-00:10] {用户明确要求的新台词}"

            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=draft,
                max_result=changed,
            )

            self.assertEqual(code, 0)
            max_mock.assert_called_once()
            self.assertIn(
                "用户明确要求的新台词",
                args.output.read_text(encoding="utf-8"),
            )

    def test_explicit_spoken_replacement_allows_only_mapped_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.product_name = "收腹裤"
            args.spoken_replacement = ["旧产品=收腹裤"]
            draft = "镜头1[00:00-00:10] {这款旧产品很好穿}"
            replaced = "镜头1[00:00-00:10] {这款收腹裤很好穿}"

            code, _, max_mock = self.run_main(
                args,
                has_audio=True,
                omni_result=draft,
                max_result=replaced,
            )

            self.assertEqual(code, 0)
            max_mock.assert_called_once()
            max_payload = max_mock.call_args.args[2]
            max_user_text = max_payload["messages"][1]["content"][-1]["text"]
            self.assertIn("旧产品→收腹裤", max_user_text)
            self.assertIn("{这款收腹裤很好穿}", max_user_text)
            with self.assertRaisesRegex(MODULE.ScriptError, "改变了 Omni 初稿"):
                MODULE.require_same_spoken_content(
                    draft,
                    "镜头1[00:00-00:10] {这款收腹裤非常好穿}",
                    [("旧产品", "收腹裤")],
                )

    def test_segment_plan_rejects_shot_gap(self) -> None:
        result = "镜头1[00:00-00:04] 第一镜头\n镜头2[00:05-00:10] 第二镜头"
        header_plan = MODULE.build_segment_header_plan(result, 10, 10.0, 15)
        self.assertEqual(header_plan["split_times_seconds"], [])
        with self.assertRaises(MODULE.ScriptError):
            MODULE.build_segment_plan(result, 10, 10.0, 15)

    def test_segment_plan_uses_minimum_count_and_rejects_short_tail(self) -> None:
        valid = (
            "【第一段提示词（27秒，对齐参考视频0-27秒）】\n"
            "镜头1[00:00-00:27] 第一段\n"
            "【第二段提示词（4秒，对齐参考视频27-31秒）】\n"
            "镜头1[00:00-00:04] 第二段"
        )
        plan = MODULE.build_segment_plan(valid, 31, 30.8, 30)
        self.assertEqual(plan["expected_segment_count"], 2)
        self.assertEqual(
            [segment["duration_seconds"] for segment in plan["segments"]],
            [27.0, 4.0],
        )

        short_tail = (
            "【第一段提示词（30秒，对齐参考视频0-30秒）】\n"
            "镜头1[00:00-00:30] 第一段\n"
            "【第二段提示词（1秒，对齐参考视频30-31秒）】\n"
            "镜头1[00:00-00:01] 第二段"
        )
        with self.assertRaisesRegex(MODULE.ScriptError, "4 到 30"):
            MODULE.build_segment_plan(short_tail, 31, 30.8, 30)

    def test_segment_plan_rejects_extra_or_fractional_segments(self) -> None:
        extra = (
            "【第一段提示词（10秒，对齐参考视频0-10秒）】\n"
            "镜头1[00:00-00:10] 第一段\n"
            "【第二段提示词（10秒，对齐参考视频10-20秒）】\n"
            "镜头1[00:00-00:10] 第二段"
        )
        with self.assertRaisesRegex(MODULE.ScriptError, "最少段数"):
            MODULE.build_segment_plan(extra, 20, 20.0, 30)

        fractional = (
            "【第一段提示词（10.5秒，对齐参考视频0-10.5秒）】\n"
            "镜头1[00:00:00-00:00:10.5] 第一段\n"
            "【第二段提示词（10.5秒，对齐参考视频10.5-21秒）】\n"
            "镜头1[00:00:00-00:00:10.5] 第二段\n"
            "【第三段提示词（10秒，对齐参考视频21-31秒）】\n"
            "镜头1[00:00-00:10] 第三段"
        )
        with self.assertRaisesRegex(MODULE.ScriptError, "整数秒"):
            MODULE.build_segment_plan(fractional, 31, 31.0, 15)

    def test_reuses_verified_draft_without_omni_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_args = self.make_args(root)
            first_code, _, _ = self.run_main(first_args, True)
            self.assertEqual(first_code, 0)

            second_args = copy(first_args)
            second_args.output = root / "prompt_retry.txt"
            second_args.candidate_output = root / "prompt_retry_candidate.txt"
            second_args.segment_plan_output = root / "segment_plan_retry.json"
            second_args.draft_file = first_args.draft_output
            second_args.draft_metadata_file = first_args.draft_metadata_output
            second_args.draft_output = None
            second_args.draft_metadata_output = None
            second_args.draft_candidate_output = None
            code, omni_mock, max_mock = self.run_main(second_args, True)

            self.assertEqual(code, 0)
            omni_mock.assert_not_called()
            max_mock.assert_called_once()
            self.assertTrue(second_args.output.is_file())

    def test_reused_draft_rejects_changed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_args = self.make_args(root)
            first_code, _, _ = self.run_main(first_args, True)
            self.assertEqual(first_code, 0)

            second_args = copy(first_args)
            second_args.output = root / "prompt_retry.txt"
            second_args.candidate_output = root / "prompt_retry_candidate.txt"
            second_args.segment_plan_output = root / "segment_plan_retry.json"
            second_args.draft_file = first_args.draft_output
            second_args.draft_metadata_file = first_args.draft_metadata_output
            second_args.draft_output = None
            second_args.draft_metadata_output = None
            second_args.draft_candidate_output = None
            second_args.product_name = "changed-product"
            code, omni_mock, max_mock = self.run_main(second_args, True)

            self.assertEqual(code, 1)
            omni_mock.assert_not_called()
            max_mock.assert_not_called()

    def test_over_limit_video_is_rejected_with_ffmpeg_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.max_inline_request_mb = 0.000001
            resolver = MODULE.MediaResolver(args)
            with mock.patch.object(
                MODULE,
                "probe_video",
                return_value={"source_duration": 10.0, "has_audio": True},
            ):
                with self.assertRaises(MODULE.ScriptError) as raised:
                    resolver.resolve(args.video, "video")

            message = str(raised.exception)
            self.assertIn("ffmpeg -i", message)
            self.assertIn("-c:a aac", message)
            self.assertIn("input_compressed.mp4", message)

    def test_local_http_contract_covers_omni_stream_and_max_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            FakeDashScopeHandler.requests = []
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDashScopeHandler)
            except PermissionError:
                self.skipTest("当前沙箱不允许绑定本地测试端口")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            args.base_url = f"http://127.0.0.1:{server.server_port}/v1"
            try:
                with (
                    mock.patch.object(MODULE, "parse_args", return_value=args),
                    mock.patch.object(
                        MODULE,
                        "probe_video",
                        return_value=self.metadata(True),
                    ),
                    mock.patch.dict(
                        os.environ,
                        {"DASHSCOPE_API_KEY": "sk-test-value"},
                    ),
                ):
                    code = MODULE.main()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(code, 0)
            self.assertEqual(
                [request["model"] for request in FakeDashScopeHandler.requests],
                ["qwen3.5-omni-plus", "qwen3.8-max"],
            )
            self.assertIn("本地 SSE", args.draft_output.read_text(encoding="utf-8"))
            self.assertIn("本地 HTTP", args.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
