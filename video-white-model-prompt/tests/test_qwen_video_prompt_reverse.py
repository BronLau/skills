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
            draft = "镜头1[00:00-00:10] 本地 SSE 视听初稿"
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

    def run_main(
        self,
        args: SimpleNamespace,
        has_audio: bool,
        omni_result: str = "镜头1[00:00-00:10] 含台词和 BGM 的视听初稿",
        max_result: str = "镜头1[00:00-00:10] 最终精修内容",
        omni_finish_reason: str = "stop",
        max_finish_reason: str = "stop",
    ) -> tuple[int, mock.Mock, mock.Mock]:
        omni_mock = mock.Mock(
            return_value=(
                omni_result,
                {"model": args.omni_model, "finish_reason": omni_finish_reason},
            )
        )
        max_mock = mock.Mock(
            return_value=(
                max_result,
                {
                    "choices": [
                        {
                            "message": {"content": max_result},
                            "finish_reason": max_finish_reason,
                        }
                    ]
                },
            )
        )
        patches = [
            mock.patch.object(MODULE, "parse_args", return_value=args),
            mock.patch.object(
                MODULE,
                "probe_video",
                return_value=self.metadata(has_audio),
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
            draft = "镜头1[00:00-00:10] 含台词、BGM 和音效的视听初稿"
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
            omni_draft = (
                "【第一段提示词（5秒，对齐参考视频0-5秒）】\n"
                "镜头1[00:00-00:05] 初稿第一段\n"
                "【第二段提示词（5秒，对齐参考视频5-10秒）】\n"
                "镜头1[00:00-00:05] 初稿第二段"
            )
            code, _, _ = self.run_main(args, True, omni_draft)

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

    def test_segment_plan_rejects_shot_gap(self) -> None:
        result = "镜头1[00:00-00:04] 第一镜头\n镜头2[00:05-00:10] 第二镜头"
        with self.assertRaises(MODULE.ScriptError):
            MODULE.build_segment_plan(result, 10, 10.0, 15)

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
