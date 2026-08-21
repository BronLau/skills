#!/usr/bin/env python3
"""Run depth inference and optional Qwen prompt reversal as one coordinated job."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
DEPTH_SCRIPT = SKILL_DIR / "scripts" / "depth_video_pipeline.py"
QWEN_SCRIPT = SKILL_DIR / "scripts" / "qwen_video_prompt_reverse.py"
SYSTEM_PROMPT = SKILL_DIR / "prompts" / "video_reverse_system_prompt.txt"
MODEL_NAME = "depth_anything_v2_vits.onnx"
LOCAL_MODEL_FALLBACK = Path(
    "/Users/bron/Documents/CodeX/Video/models/depth_anything_v2_vits.onnx"
)


class PipelineError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument(
        "--scope",
        choices=("depth-only", "depth-and-prompt"),
        required=True,
    )
    parser.add_argument(
        "--segment-max-seconds",
        type=int,
        choices=(15, 30),
        required=True,
    )
    parser.add_argument("--product-name", default="")
    parser.add_argument("--product-image", type=Path, action="append", default=[])
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--depth-model", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--save-debug", action="store_true")
    parser.add_argument("--max-inline-request-mb", type=float, default=100)
    return parser.parse_args()


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.R_OK):
        raise PipelineError(f"{label}不存在或不可读：{resolved}")
    return resolved


def resolve_key_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if resolved.is_dir():
        resolved = resolved / "DASHSCOPE_API_KEY.md"
    return require_file(resolved, "DashScope Key 文件")


def resolve_depth_model(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    environment_path = os.environ.get("DEPTH_ANYTHING_MODEL")
    if environment_path:
        candidates.append(Path(environment_path))
    candidates.extend(
        [
            SKILL_DIR / "models" / MODEL_NAME,
            LOCAL_MODEL_FALLBACK,
        ]
    )
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file() and os.access(resolved, os.R_OK):
            return resolved
    raise PipelineError(
        "未找到 Depth Anything V2 ONNX 模型；请设置 DEPTH_ANYTHING_MODEL "
        "或传入 --depth-model。"
    )


def prepare_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise PipelineError(f"输出路径不是目录：{resolved}")
        if any(resolved.iterdir()):
            raise PipelineError(f"输出目录不是空目录，拒绝覆盖：{resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / "depth").mkdir(exist_ok=True)
    return resolved


def run_process(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def main() -> int:
    args = parse_args()
    try:
        video = require_file(args.video, "参考视频")
        if len(args.product_image) > 9:
            raise PipelineError("产品图片最多 9 张")
        product_images = [
            require_file(path, f"第{index}张产品图")
            for index, path in enumerate(args.product_image, start=1)
        ]
        if args.scope == "depth-only" and (
            args.product_name.strip() or product_images or args.api_key_file
        ):
            raise PipelineError("仅白模模式不接受产品或 API Key 参数")

        key_path = resolve_key_path(args.api_key_file)
        if args.scope == "depth-and-prompt":
            if not os.environ.get("DASHSCOPE_API_KEY") and key_path is None:
                raise PipelineError(
                    "白模+提示词模式需要 DASHSCOPE_API_KEY 或 --api-key-file"
                )
            require_file(SYSTEM_PROMPT, "Qwen 系统提示词")

        model = resolve_depth_model(args.depth_model)
        output_dir = prepare_output_dir(args.output_dir)
        prompt_path = output_dir / "prompt.txt"
        plan_path = output_dir / "segment_plan.json"

        with tempfile.TemporaryDirectory(prefix="chuangliang-depth-") as work_dir:
            depth_infer = [
                sys.executable,
                str(DEPTH_SCRIPT),
                "--mode",
                "infer",
                "--input",
                str(video),
                "--model",
                str(model),
                "--work-dir",
                work_dir,
            ]

            if args.scope == "depth-only":
                if run_process(depth_infer) != 0:
                    raise PipelineError("白模深度推理失败")
                depth_encode = [
                    sys.executable,
                    str(DEPTH_SCRIPT),
                    "--mode",
                    "encode",
                    "--input",
                    str(video),
                    "--work-dir",
                    work_dir,
                    "--output-dir",
                    str(output_dir / "depth"),
                    "--segment-seconds",
                    str(args.segment_max_seconds),
                ]
                if run_process(depth_encode) != 0:
                    raise PipelineError("白模编码失败")
                return 0

            qwen_command = [
                sys.executable,
                str(QWEN_SCRIPT),
                "--video",
                str(video),
                "--system-prompt",
                str(SYSTEM_PROMPT),
                "--segment-max-seconds",
                str(args.segment_max_seconds),
                "--output",
                str(prompt_path),
                "--segment-plan-output",
                str(plan_path),
                "--max-inline-request-mb",
                str(args.max_inline_request_mb),
            ]
            if key_path:
                qwen_command.extend(["--api-key-file", str(key_path)])
            if args.product_name.strip():
                qwen_command.extend(["--product-name", args.product_name.strip()])
            for image in product_images:
                qwen_command.extend(["--product-image", str(image)])
            if args.save_debug:
                qwen_command.extend(
                    [
                        "--request-body-output",
                        str(output_dir / "request.json"),
                        "--response-body-output",
                        str(output_dir / "response.json"),
                    ]
                )

            print("PIPELINE parallel_start", flush=True)
            depth_process = subprocess.Popen(depth_infer)
            qwen_process = subprocess.Popen(qwen_command)
            try:
                qwen_code = qwen_process.wait()
                depth_code = depth_process.wait()
            except KeyboardInterrupt:
                for process in (qwen_process, depth_process):
                    if process.poll() is None:
                        process.terminate()
                for process in (qwen_process, depth_process):
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                raise
            print(
                f"PIPELINE parallel_done qwen={qwen_code} depth={depth_code}",
                flush=True,
            )

            encode_code = 1
            if depth_code == 0:
                depth_encode = [
                    sys.executable,
                    str(DEPTH_SCRIPT),
                    "--mode",
                    "encode",
                    "--input",
                    str(video),
                    "--work-dir",
                    work_dir,
                    "--output-dir",
                    str(output_dir / "depth"),
                ]
                if qwen_code == 0 and plan_path.is_file():
                    depth_encode.extend(["--segment-plan", str(plan_path)])
                else:
                    depth_encode.extend(
                        ["--segment-seconds", str(args.segment_max_seconds)]
                    )
                encode_code = run_process(depth_encode)

            if qwen_code != 0 or depth_code != 0 or encode_code != 0:
                raise PipelineError(
                    "流程未完全成功："
                    f"qwen={qwen_code}, depth_infer={depth_code}, "
                    f"depth_encode={encode_code}"
                )
        return 0
    except PipelineError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
