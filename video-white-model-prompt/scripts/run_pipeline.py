#!/usr/bin/env python3
"""Run depth inference and optional Qwen prompt reversal as one coordinated job."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from media_preflight import (
    DEFAULT_INLINE_LIMIT_MB,
    MediaPreflightError,
    build_compression_command,
    estimated_data_url_size,
    probe_video_signature,
    validate_analysis_video as validate_analysis_video_shared,
    validate_image_input as validate_image_input_shared,
)

__all__ = [
    "build_compression_command",
    "estimated_data_url_size",
    "probe_video_signature",
]


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
    parser.add_argument("--analysis-video", type=Path)
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
    parser.add_argument("--character-image", type=Path)
    parser.add_argument("--selling-points", default="")
    parser.add_argument("--user-idea", default="")
    parser.add_argument("--transcript-file", type=Path)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--depth-model", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--save-debug", action="store_true")
    parser.add_argument(
        "--max-inline-request-mb",
        type=float,
        default=DEFAULT_INLINE_LIMIT_MB,
    )
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.R_OK):
        raise PipelineError(f"{label}不存在或不可读：{resolved}")
    return resolved


def validate_analysis_video(
    source_video: Path,
    analysis_video: Path,
    inline_limit_mb: float,
) -> None:
    try:
        validate_analysis_video_shared(source_video, analysis_video, inline_limit_mb)
    except MediaPreflightError as exc:
        raise PipelineError(str(exc)) from exc


def validate_image_input(path: Path, label: str, inline_limit_mb: float) -> None:
    try:
        validate_image_input_shared(path, label, inline_limit_mb)
    except MediaPreflightError as exc:
        raise PipelineError(str(exc)) from exc


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


def prepare_output_dir(path: Path, resume: bool) -> Path:
    resolved = path.expanduser().resolve()
    if resume:
        if not resolved.is_dir():
            raise PipelineError(f"恢复运行要求输出目录已存在：{resolved}")
        (resolved / "depth").mkdir(exist_ok=True)
        return resolved
    if resolved.exists():
        if not resolved.is_dir():
            raise PipelineError(f"输出路径不是目录：{resolved}")
        if any(resolved.iterdir()):
            raise PipelineError(f"输出目录不是空目录，拒绝覆盖：{resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / "depth").mkdir(exist_ok=True)
    return resolved


def file_identity(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def run_manifest(
    args: argparse.Namespace,
    video: Path,
    analysis_video: Path,
    model: Path,
    character_image: Path | None,
    product_images: list[Path],
    transcript_file: Path | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "video": file_identity(video),
        "analysis_video": file_identity(analysis_video),
        "scope": args.scope,
        "segment_max_seconds": args.segment_max_seconds,
        "product_name": args.product_name.strip(),
        "product_images": [file_identity(path) for path in product_images],
        "character_image": file_identity(character_image),
        "selling_points": args.selling_points.strip(),
        "user_idea": args.user_idea.strip(),
        "transcript_file": file_identity(transcript_file),
        "depth_model": file_identity(model),
    }


def write_manifest(path: Path, body: dict[str, object]) -> None:
    serialized = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(serialized)


def validate_manifest(path: Path, expected: dict[str, object]) -> None:
    if not path.is_file():
        raise PipelineError(f"恢复运行缺少输入清单：{path}")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"输入清单不是有效 JSON：{path}") from exc
    if actual != expected:
        raise PipelineError("恢复运行的输入视频、产品、参数或深度模型与原任务不一致。")


def depth_cache_complete(work_dir: Path) -> bool:
    return (work_dir / "raw_depth_float16.npy").is_file() and (
        work_dir / "global_bounds.json"
    ).is_file()


def depth_outputs(directory: Path, video: Path) -> list[Path]:
    return sorted(directory.glob(f"{video.stem}_depth_720p_part_*.mp4"))


def archive_depth_outputs(output_dir: Path, video: Path) -> None:
    depth_dir = output_dir / "depth"
    if not depth_outputs(depth_dir, video):
        return
    index = 1
    while (output_dir / f"depth_previous_{index}").exists():
        index += 1
    archived = output_dir / f"depth_previous_{index}"
    depth_dir.rename(archived)
    depth_dir.mkdir()
    print(f"PIPELINE archived_previous_depth={archived}", flush=True)


def validate_completed_outputs(
    prompt_path: Path,
    plan_path: Path,
    depth_files: list[Path],
) -> None:
    if not prompt_path.is_file() or prompt_path.stat().st_size == 0:
        raise PipelineError("完成标记存在，但正式提示词缺失或为空。")
    if not plan_path.is_file():
        raise PipelineError("完成标记存在，但分段计划缺失。")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        expected_count = len(plan["segments"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PipelineError("完成标记存在，但分段计划无效。") from exc
    if expected_count <= 0 or len(depth_files) != expected_count:
        raise PipelineError(
            "完成标记存在，但白模分段数量不匹配："
            f"{len(depth_files)} != {expected_count}"
        )
    if any(path.stat().st_size == 0 for path in depth_files):
        raise PipelineError("完成标记存在，但白模文件为空。")


def run_process(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def main() -> int:
    args = parse_args()
    try:
        if args.resume and args.scope != "depth-and-prompt":
            raise PipelineError("--resume 仅支持白模+提示词模式")
        video = require_file(args.video, "参考视频")
        analysis_video = (
            require_file(args.analysis_video, "压缩分析视频")
            if args.analysis_video
            else video
        )
        if len(args.product_image) > 9:
            raise PipelineError("产品图片最多 9 张")
        product_images = [
            require_file(path, f"第{index}张产品图")
            for index, path in enumerate(args.product_image, start=1)
        ]
        character_image = (
            require_file(args.character_image, "人物形象图")
            if args.character_image
            else None
        )
        transcript_file = (
            require_file(args.transcript_file, "音轨转写文件")
            if args.transcript_file
            else None
        )
        if args.scope == "depth-only" and (
            args.product_name.strip()
            or product_images
            or character_image
            or args.selling_points.strip()
            or args.user_idea.strip()
            or transcript_file
            or args.analysis_video
            or args.api_key_file
        ):
            raise PipelineError("仅白模模式不接受提示词、图片、转写或 API Key 参数")

        key_path = resolve_key_path(args.api_key_file)
        model = resolve_depth_model(args.depth_model)
        if args.scope == "depth-and-prompt":
            if not 0 < args.max_inline_request_mb <= 9.5:
                raise PipelineError("--max-inline-request-mb 必须在 0 到 9.5 之间")
            validate_analysis_video(
                video,
                analysis_video,
                args.max_inline_request_mb,
            )
            if character_image:
                validate_image_input(
                    character_image,
                    "人物形象图",
                    args.max_inline_request_mb,
                )
            for index, product_image in enumerate(product_images, start=1):
                validate_image_input(
                    product_image,
                    f"第{index}张产品图",
                    args.max_inline_request_mb,
                )
        output_dir = prepare_output_dir(args.output_dir, args.resume)
        manifest_path = output_dir / "run_manifest.json"
        expected_manifest = run_manifest(
            args,
            video,
            analysis_video,
            model,
            character_image,
            product_images,
            transcript_file,
        )
        if args.resume:
            validate_manifest(manifest_path, expected_manifest)
        else:
            write_manifest(manifest_path, expected_manifest)
        completion_path = output_dir / "completed.json"

        if args.scope == "depth-only":
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
            write_manifest(
                completion_path,
                {"schema_version": 1, "status": "complete", "scope": args.scope},
            )
            return 0

        require_file(SYSTEM_PROMPT, "Qwen 系统提示词")
        draft_path = output_dir / "prompt_draft.txt"
        draft_candidate_path = output_dir / "prompt_draft_candidate.txt"
        draft_meta_path = output_dir / "prompt_draft_meta.json"
        candidate_path = output_dir / "prompt_candidate.txt"
        prompt_path = output_dir / "prompt.txt"
        plan_path = output_dir / "segment_plan.json"
        work_dir = output_dir / ".depth_work"
        current_depth_outputs = depth_outputs(output_dir / "depth", video)

        prompt_complete = prompt_path.is_file() and plan_path.is_file()
        cache_complete = depth_cache_complete(work_dir)
        if args.resume and completion_path.is_file():
            validate_completed_outputs(
                prompt_path,
                plan_path,
                current_depth_outputs,
            )
            print("PIPELINE already_complete", flush=True)
            return 0

        qwen_command: list[str] | None = None
        if not prompt_complete:
            if not os.environ.get("DASHSCOPE_API_KEY") and key_path is None:
                raise PipelineError(
                    "白模+提示词模式需要 DASHSCOPE_API_KEY 或 --api-key-file"
                )
            qwen_command = [
                sys.executable,
                str(QWEN_SCRIPT),
                "--video",
                str(analysis_video),
                "--system-prompt",
                str(SYSTEM_PROMPT),
                "--segment-max-seconds",
                str(args.segment_max_seconds),
                "--output",
                str(prompt_path),
                "--candidate-output",
                str(candidate_path),
                "--segment-plan-output",
                str(plan_path),
                "--max-inline-request-mb",
                str(args.max_inline_request_mb),
                "--max-tokens",
                str(args.max_tokens),
            ]
            if draft_path.is_file() and draft_meta_path.is_file():
                qwen_command.extend(
                    [
                        "--draft-file",
                        str(draft_path),
                        "--draft-metadata-file",
                        str(draft_meta_path),
                    ]
                )
            else:
                qwen_command.extend(
                    [
                        "--draft-output",
                        str(draft_path),
                        "--draft-candidate-output",
                        str(draft_candidate_path),
                        "--draft-metadata-output",
                        str(draft_meta_path),
                    ]
                )
            if args.resume:
                qwen_command.append("--overwrite")
            if key_path:
                qwen_command.extend(["--api-key-file", str(key_path)])
            if args.product_name.strip():
                qwen_command.extend(["--product-name", args.product_name.strip()])
            if character_image:
                qwen_command.extend(["--character-image", str(character_image)])
            if args.selling_points.strip():
                qwen_command.extend(["--selling-points", args.selling_points.strip()])
            if args.user_idea.strip():
                qwen_command.extend(["--user-idea", args.user_idea.strip()])
            if transcript_file:
                qwen_command.extend(["--transcript-file", str(transcript_file)])
            for image in product_images:
                qwen_command.extend(["--product-image", str(image)])
            if args.save_debug:
                qwen_command.extend(
                    [
                        "--request-body-output",
                        str(output_dir / "max_request.json"),
                        "--response-body-output",
                        str(output_dir / "max_response.json"),
                        "--omni-request-body-output",
                        str(output_dir / "omni_request.json"),
                        "--omni-response-body-output",
                        str(output_dir / "omni_response.json"),
                    ]
                )

        depth_infer: list[str] | None = None
        if not cache_complete:
            work_dir.mkdir(parents=True, exist_ok=True)
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
                str(work_dir),
            ]
            if args.resume:
                depth_infer.append("--overwrite")

        print("PIPELINE parallel_start", flush=True)
        depth_process = subprocess.Popen(depth_infer) if depth_infer else None
        qwen_process = subprocess.Popen(qwen_command) if qwen_command else None
        processes = [process for process in (qwen_process, depth_process) if process]
        try:
            qwen_code = qwen_process.wait() if qwen_process else 0
            depth_code = depth_process.wait() if depth_process else 0
        except KeyboardInterrupt:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise
        print(
            f"PIPELINE parallel_done qwen={qwen_code} depth={depth_code}",
            flush=True,
        )

        encode_code = 1
        if depth_code == 0:
            depth_dir = output_dir / "depth"
            existing_outputs = depth_outputs(depth_dir, video)
            if qwen_code == 0 and plan_path.is_file():
                if existing_outputs:
                    archive_depth_outputs(output_dir, video)
                depth_encode = [
                    sys.executable,
                    str(DEPTH_SCRIPT),
                    "--mode",
                    "encode",
                    "--input",
                    str(video),
                    "--work-dir",
                    str(work_dir),
                    "--output-dir",
                    str(output_dir / "depth"),
                    "--segment-plan",
                    str(plan_path),
                ]
                encode_code = run_process(depth_encode)
            elif existing_outputs:
                encode_code = 0
            else:
                depth_encode = [
                    sys.executable,
                    str(DEPTH_SCRIPT),
                    "--mode",
                    "encode",
                    "--input",
                    str(video),
                    "--work-dir",
                    str(work_dir),
                    "--output-dir",
                    str(depth_dir),
                    "--segment-seconds",
                    str(args.segment_max_seconds),
                ]
                encode_code = run_process(depth_encode)

        if qwen_code == 0 and depth_code == 0 and encode_code == 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            write_manifest(
                completion_path,
                {"schema_version": 1, "status": "complete", "scope": args.scope},
            )
            return 0

        raise PipelineError(
            "流程未完全成功，已保留可恢复产物："
            f"qwen={qwen_code}, depth_infer={depth_code}, depth_encode={encode_code}"
        )
    except PipelineError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
