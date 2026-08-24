#!/usr/bin/env python3
"""Convert one video to globally-normalized, temporally-stabilized relative depth."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import cast

import cv2
import numpy as np
import onnxruntime as ort


IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
MODEL_SIZE = (518, 518)
SAMPLE_COUNT = 8192
MAX_GLOBAL_SAMPLES = 1_000_000


def parse_frame_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"视频帧率字段无效：{value}") from exc


def video_info(path: Path) -> dict[str, float | int]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开输入视频：{path}")
    decoded_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    decoded_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    decoded_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe，无法校验视频时间基")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        metadata = json.loads(completed.stdout)
        stream = metadata["streams"][0]
        duration = float(metadata["format"]["duration"])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(f"ffprobe 无法读取视频元数据：{detail.strip()}") from exc

    average_fps = parse_frame_rate(stream.get("avg_frame_rate"))
    nominal_fps = parse_frame_rate(stream.get("r_frame_rate"))
    if nominal_fps and abs(average_fps - nominal_fps) > max(0.001, average_fps * 0.001):
        raise RuntimeError(
            "当前白模管线不支持可变帧率视频："
            f"avg_frame_rate={average_fps:.6f}, r_frame_rate={nominal_fps:.6f}"
        )
    declared_frames = stream.get("nb_frames")
    if declared_frames not in (None, "N/A"):
        try:
            declared_frame_count = int(declared_frames)
        except ValueError as exc:
            raise RuntimeError(f"视频帧数字段无效：{declared_frames}") from exc
        if abs(declared_frame_count - decoded_frames) > 1:
            raise RuntimeError(
                "OpenCV 与 ffprobe 读取的帧数不一致："
                f"{decoded_frames} != {declared_frame_count}"
            )

    info = {
        "width": int(stream.get("width") or decoded_width),
        "height": int(stream.get("height") or decoded_height),
        "fps": average_fps,
        "frames": decoded_frames,
        "duration": duration,
    }
    if (
        not info["width"]
        or not info["height"]
        or not info["frames"]
        or not np.isfinite(info["fps"])
        or info["fps"] <= 0
        or not np.isfinite(info["duration"])
        or info["duration"] <= 0
    ):
        raise RuntimeError(f"视频参数无效：{info}")
    return info


def file_identity(path: Path) -> dict[str, str | int]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def preprocess(frame: np.ndarray) -> np.ndarray:
    image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    resized = np.asarray(
        cv2.resize(image, MODEL_SIZE, interpolation=cv2.INTER_CUBIC),
        dtype=np.float32,
    )
    normalized = (resized - IMAGENET_MEAN) / IMAGENET_STD
    return normalized.transpose(2, 0, 1)[None].astype(np.float32)


def infer_all(
    input_path: Path,
    model_path: Path,
    work_dir: Path,
    overwrite: bool,
) -> tuple[Path, float, float]:
    info = video_info(input_path)
    frame_count = int(info["frames"])
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / "raw_depth_float16.npy"
    bounds_path = work_dir / "global_bounds.json"
    existing_cache = [path for path in (raw_path, bounds_path) if path.exists()]
    if existing_cache and not overwrite:
        raise RuntimeError(
            "白模工作目录已有推理缓存，拒绝覆盖："
            + ", ".join(str(path) for path in existing_cache)
        )
    if overwrite:
        for path in existing_cache:
            path.unlink()
    cache_bytes = (
        frame_count
        * MODEL_SIZE[0]
        * MODEL_SIZE[1]
        * np.dtype(np.float16).itemsize
    )
    required_bytes = int(cache_bytes * 1.1) + 512 * 1024 * 1024
    free_bytes = shutil.disk_usage(work_dir).free
    if free_bytes < required_bytes:
        raise RuntimeError(
            "白模中间缓存空间不足："
            f"预计至少需要 {required_bytes / 1024**3:.2f} GiB，"
            f"当前可用 {free_bytes / 1024**3:.2f} GiB"
        )
    print(
        f"CACHE_ESTIMATE {cache_bytes / 1024**3:.2f} GiB "
        f"FREE {free_bytes / 1024**3:.2f} GiB",
        flush=True,
    )
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 8
    session_options.inter_op_num_threads = 1
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(model_path),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    input_name = session.get_inputs()[0].name

    raw = np.lib.format.open_memmap(
        raw_path,
        mode="w+",
        dtype=np.float16,
        shape=(frame_count, MODEL_SIZE[1], MODEL_SIZE[0]),
    )
    rng = np.random.default_rng(20260820)
    samples_per_frame = max(
        1,
        min(SAMPLE_COUNT, MAX_GLOBAL_SAMPLES // max(frame_count, 1)),
    )
    sample_indices = np.sort(
        rng.choice(
            MODEL_SIZE[0] * MODEL_SIZE[1],
            size=samples_per_frame,
            replace=False,
        )
    )
    samples = np.empty((frame_count, samples_per_frame), dtype=np.float16)

    cap = cv2.VideoCapture(str(input_path))
    start = time.time()
    processed = 0
    try:
        while processed < frame_count:
            ok, frame = cap.read()
            if not ok:
                break
            depth = session.run(None, {input_name: preprocess(frame)})[0][0]
            raw[processed] = depth.astype(np.float16)
            samples[processed] = depth.reshape(-1)[sample_indices].astype(np.float16)
            processed += 1
            if processed == 1 or processed % 30 == 0 or processed == frame_count:
                elapsed = time.time() - start
                rate = processed / elapsed
                eta = (frame_count - processed) / max(rate, 1e-6)
                print(
                    f"INFER {processed}/{frame_count} "
                    f"({processed / frame_count:.1%}) {rate:.2f} fps ETA {eta:.0f}s",
                    flush=True,
                )
    finally:
        cap.release()
        raw.flush()
    if processed != frame_count:
        raise RuntimeError(f"输入解码提前结束：仅处理 {processed}/{frame_count} 帧")

    sampled = samples.astype(np.float32).reshape(-1)
    low, high = np.percentile(sampled, [1.0, 99.0]).astype(float)
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        raise RuntimeError(f"全局深度范围无效：low={low}, high={high}")
    bounds_path.write_text(
        json.dumps(
            {
                "normalization": "global sampled percentile",
                "percentiles": [1.0, 99.0],
                "low": low,
                "high": high,
                "direction": "near_white_far_black",
                "model": model_path.name,
                "model_input": [1, 3, MODEL_SIZE[1], MODEL_SIZE[0]],
                "preprocess": "RGB/255, bicubic 518x518, ImageNet mean/std",
                "frames": frame_count,
                "video_info": info,
                "source_file": file_identity(input_path),
                "model_file": file_identity(model_path),
                "samples_per_frame": samples_per_frame,
                "total_global_samples": frame_count * samples_per_frame,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"GLOBAL_RANGE low={low:.6f} high={high:.6f}", flush=True)
    return raw_path, low, high


def load_inference_artifacts(
    work_dir: Path, input_path: Path
) -> tuple[Path, float, float]:
    raw_path = work_dir / "raw_depth_float16.npy"
    bounds_path = work_dir / "global_bounds.json"
    if not raw_path.is_file() or not bounds_path.is_file():
        raise RuntimeError(f"白模推理产物不完整：{work_dir}")
    bounds = json.loads(bounds_path.read_text(encoding="utf-8"))
    expected_source = file_identity(input_path)
    if bounds.get("source_file") != expected_source:
        raise RuntimeError(
            "白模推理缓存不属于当前输入视频："
            f"缓存={bounds.get('source_file')}，当前={expected_source}"
        )
    try:
        low = float(bounds["low"])
        high = float(bounds["high"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"白模全局深度范围文件无效：{bounds_path}") from exc
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        raise RuntimeError(f"白模全局深度范围无效：low={low}, high={high}")
    return raw_path, low, high


def parse_segment_times(value: str | None) -> list[float]:
    if value is None:
        return []
    try:
        times = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError("--segment-times 必须是逗号分隔的秒数") from exc
    if not times:
        raise RuntimeError("--segment-times 不能为空")
    if any(not np.isfinite(item) or item <= 0 for item in times):
        raise RuntimeError("--segment-times 中的时间点必须是正数")
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise RuntimeError("--segment-times 必须严格递增")
    return times


def load_segment_plan(path: Path) -> tuple[int, list[float]]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        plan = json.loads(resolved.read_text(encoding="utf-8"))
        segment_max_seconds = int(plan["segment_max_seconds"])
        raw_times = plan["split_times_seconds"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Qwen 分段计划文件无效：{resolved}") from exc
    if segment_max_seconds not in (15, 30):
        raise RuntimeError(
            f"Qwen 分段计划的最大时长必须为 15 或 30：{segment_max_seconds}"
        )
    if not isinstance(raw_times, list):
        raise RuntimeError("Qwen 分段计划中的 split_times_seconds 必须是数组")
    if not raw_times:
        return segment_max_seconds, []
    return segment_max_seconds, parse_segment_times(
        ",".join(str(value) for value in raw_times)
    )


def even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def output_size(width: int, height: int) -> tuple[int, int]:
    max_width, max_height = ((1280, 720) if width >= height else (720, 1280))
    scale = min(max_width / width, max_height / height)
    return even(width * scale), even(height * scale)


def encode(
    input_path: Path,
    raw_path: Path,
    low: float,
    high: float,
    output_dir: Path,
    ffmpeg: str,
    segment_seconds: int | None,
    segment_times: list[float],
    overwrite: bool,
) -> None:
    info = video_info(input_path)
    width = int(info["width"])
    height = int(info["height"])
    fps = float(info["fps"])
    frame_count = int(info["frames"])
    out_w, out_h = output_size(width, height)
    stab_w, stab_h = even(out_w / 2), even(out_h / 2)
    raw = np.load(raw_path, mmap_mode="r")
    if raw.shape[0] != frame_count:
        raise RuntimeError(f"深度帧数不匹配：{raw.shape[0]} != {frame_count}")

    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / f"{input_path.stem}_depth_720p_part_%02d.mp4"
    source_duration = float(info["duration"])
    if segment_times:
        if segment_times[-1] >= source_duration:
            raise RuntimeError(
                "最后一个白模切分时间点必须小于视频时长："
                f"{segment_times[-1]:g} >= {source_duration:.6f}"
            )
        boundaries = [0.0, *segment_times, source_duration]
        max_interval = max(
            current - previous for previous, current in zip(boundaries, boundaries[1:])
        )
        segment_frames = max(1, int(round(max_interval * fps)))
        encoded_times = ",".join(f"{value:.12g}" for value in segment_times)
        segment_options = [
            "-g",
            str(segment_frames),
            "-keyint_min",
            "1",
            "-sc_threshold",
            "0",
            "-force_key_frames",
            encoded_times,
            "-f",
            "segment",
            "-segment_times",
            encoded_times,
        ]
    else:
        if segment_seconds is None:
            raise RuntimeError("编码阶段必须提供 --segment-seconds 或 --segment-times")
        segment_frames = max(1, int(round(segment_seconds * fps)))
        segment_options = [
            "-g",
            str(segment_frames),
            "-keyint_min",
            str(segment_frames),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_seconds})",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
        ]
    existing_outputs = sorted(
        output_dir.glob(f"{input_path.stem}_depth_720p_part_*.mp4")
    )
    if existing_outputs and not overwrite:
        raise RuntimeError(
            "白模输出文件已存在，拒绝覆盖："
            + ", ".join(str(path) for path in existing_outputs)
        )
    if overwrite:
        for path in existing_outputs:
            path.unlink()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y" if overwrite else "-n",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s:v",
        f"{out_w}x{out_h}",
        "-r",
        f"{fps:.12g}",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        *segment_options,
        "-segment_time_delta",
        f"{0.5 / fps:.12g}",
        "-reset_timestamps",
        "1",
        "-segment_start_number",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+faststart",
        str(pattern),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    if process.stdin is None:
        raise RuntimeError("无法打开 FFmpeg 输入管道")

    cap = cv2.VideoCapture(str(input_path))
    map_x, map_y = np.meshgrid(
        np.arange(stab_w, dtype=np.float32), np.arange(stab_h, dtype=np.float32)
    )
    prev_gray: np.ndarray | None = None
    prev_stable: np.ndarray | None = None
    start = time.time()
    encoded = 0
    try:
        while encoded < frame_count:
            ok, frame = cap.read()
            if not ok:
                break
            current = cv2.resize(raw[encoded].astype(np.float32), (stab_w, stab_h), interpolation=cv2.INTER_CUBIC)
            current = np.clip((current - low) / (high - low), 0.0, 1.0)
            gray = cv2.resize(
                cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                (stab_w, stab_h),
                interpolation=cv2.INTER_AREA,
            )

            if prev_gray is None or prev_stable is None:
                stable = current
            else:
                scene_delta = float(cv2.absdiff(gray, prev_gray).mean()) / 255.0
                if scene_delta > 0.18:
                    stable = current
                else:
                    backward_flow = cv2.calcOpticalFlowFarneback(
                        gray,
                        prev_gray,
                        cast(np.ndarray, None),
                        0.5,
                        3,
                        15,
                        3,
                        5,
                        1.1,
                        0,
                    )
                    sample_x = map_x + backward_flow[..., 0]
                    sample_y = map_y + backward_flow[..., 1]
                    warped_depth = cv2.remap(
                        prev_stable,
                        sample_x,
                        sample_y,
                        cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE,
                    )
                    warped_gray = cv2.remap(
                        prev_gray,
                        sample_x,
                        sample_y,
                        cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE,
                    )
                    valid = (
                        (sample_x >= 0)
                        & (sample_x <= stab_w - 1)
                        & (sample_y >= 0)
                        & (sample_y <= stab_h - 1)
                        & (np.abs(current - warped_depth) < 0.10)
                        & (cv2.absdiff(gray, warped_gray) < 28)
                    )
                    stable = current.copy()
                    stable[valid] = 0.78 * current[valid] + 0.22 * warped_depth[valid]

            output = cv2.resize(stable, (out_w, out_h), interpolation=cv2.INTER_CUBIC)
            output = np.clip(np.rint(output * 255.0), 0, 255).astype(np.uint8)
            process.stdin.write(output.tobytes())
            prev_gray = gray
            prev_stable = stable
            encoded += 1
            if encoded == 1 or encoded % 60 == 0 or encoded == frame_count:
                elapsed = time.time() - start
                rate = encoded / elapsed
                eta = (frame_count - encoded) / max(rate, 1e-6)
                print(
                    f"ENCODE {encoded}/{frame_count} "
                    f"({encoded / frame_count:.1%}) {rate:.2f} fps ETA {eta:.0f}s",
                    flush=True,
                )
    finally:
        cap.release()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"FFmpeg 编码失败，退出码 {return_code}")
    if encoded != frame_count:
        raise RuntimeError(f"编码阶段提前结束：仅处理 {encoded}/{frame_count} 帧")
    print(f"OUTPUT_SIZE {out_w}x{out_h}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("all", "infer", "encode"), default="all")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    segment_group = parser.add_mutually_exclusive_group()
    segment_group.add_argument("--segment-seconds", type=int, choices=(15, 30))
    segment_group.add_argument(
        "--segment-times",
        help="Qwen 分段计划给出的累计切分时间点，使用逗号分隔，例如 12,27。",
    )
    segment_group.add_argument(
        "--segment-plan",
        type=Path,
        help="Qwen 输出的 segment_plan.json；自动读取最大时长和切分时间点。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="显式允许覆盖本次工作缓存或同名白模输出。",
    )
    args = parser.parse_args()

    input_path = args.input.resolve(strict=True)
    work_dir = args.work_dir.resolve()
    segment_seconds = args.segment_seconds
    segment_times = parse_segment_times(args.segment_times)
    if args.segment_plan:
        segment_seconds, segment_times = load_segment_plan(args.segment_plan)

    if args.mode in ("all", "infer"):
        if args.model is None:
            parser.error("--mode all/infer 必须提供 --model")
        model_path = args.model.resolve(strict=True)
    if args.mode in ("all", "encode"):
        if args.output_dir is None:
            parser.error("--mode all/encode 必须提供 --output-dir")
        if segment_seconds is None and not segment_times:
            parser.error(
                "--mode all/encode 必须提供 --segment-seconds、"
                "--segment-times 或 --segment-plan"
            )

    if args.mode in ("all", "infer"):
        raw_path, low, high = infer_all(
            input_path,
            model_path,
            work_dir,
            args.overwrite,
        )
        if args.mode == "infer":
            return 0
    else:
        raw_path, low, high = load_inference_artifacts(work_dir, input_path)

    encode(
        input_path,
        raw_path,
        low,
        high,
        args.output_dir,
        args.ffmpeg,
        segment_seconds,
        segment_times,
        args.overwrite,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
