#!/usr/bin/env python3
"""Remove white, dark-outlined burned subtitles from generated videos."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


class SubtitleRemovalError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roi-top", type=float, default=0.42)
    parser.add_argument("--white-threshold", type=int, default=195)
    parser.add_argument("--dark-threshold", type=int, default=115)
    parser.add_argument("--inpaint-radius", type=float, default=5.0)
    parser.add_argument("--crf", type=int, default=18)
    return parser.parse_args()


def subtitle_mask(
    frame: np.ndarray,
    roi_top: float,
    white_threshold: int,
    dark_threshold: int,
) -> np.ndarray:
    height, width = frame.shape[:2]
    top = max(0, min(height - 1, int(round(height * roi_top))))
    roi = frame[top:]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    channel_min = roi.min(axis=2)
    channel_max = roi.max(axis=2)
    neutral_white = (
        (channel_min >= white_threshold)
        & ((channel_max.astype(np.int16) - channel_min.astype(np.int16)) <= 48)
    ).astype(np.uint8) * 255
    dark = (gray <= dark_threshold).astype(np.uint8) * 255
    dark_near = cv2.dilate(dark, np.ones((9, 9), np.uint8), iterations=1)
    candidate = cv2.bitwise_and(neutral_white, dark_near)
    candidate = cv2.morphologyEx(
        candidate,
        cv2.MORPH_OPEN,
        np.ones((2, 2), np.uint8),
    )

    selected = np.zeros_like(candidate)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, 8)
    max_height = max(14, int(round(height * 0.075)))
    max_area = max(1500, int(round(width * height * 0.006)))
    for label in range(1, count):
        x, y, component_width, component_height, area = stats[label]
        if not 2 <= component_width <= int(width * 0.95):
            continue
        if not 5 <= component_height <= max_height:
            continue
        if not 10 <= area <= max_area:
            continue
        selected[labels == label] = 255

    selected = cv2.dilate(selected, np.ones((9, 9), np.uint8), iterations=1)
    full = np.zeros((height, width), dtype=np.uint8)
    full[top:] = selected
    return full


def clean_frame(frame: np.ndarray, args: argparse.Namespace) -> tuple[np.ndarray, np.ndarray]:
    mask = subtitle_mask(
        frame,
        args.roi_top,
        args.white_threshold,
        args.dark_threshold,
    )
    if not np.any(mask):
        return frame, mask
    return cv2.inpaint(frame, mask, args.inpaint_radius, cv2.INPAINT_TELEA), mask


def clean_image(source: Path, destination: Path, args: argparse.Namespace) -> None:
    frame = cv2.imread(str(source), cv2.IMREAD_COLOR)
    if frame is None:
        raise SubtitleRemovalError(f"无法读取图片：{source}")
    cleaned, mask = clean_frame(frame, args)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), cleaned):
        raise SubtitleRemovalError(f"无法写入图片：{destination}")
    mask_path = destination.with_name(f"{destination.stem}_mask.png")
    cv2.imwrite(str(mask_path), mask)


def clean_video(source: Path, destination: Path, args: argparse.Namespace) -> None:
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise SubtitleRemovalError(f"无法读取视频：{source}")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if width <= 0 or height <= 0 or fps <= 0:
        capture.release()
        raise SubtitleRemovalError("视频尺寸或帧率无效。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{fps:.8f}",
        "-i",
        "-",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(args.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-shortest",
        str(destination),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    if process.stdin is None:
        capture.release()
        raise SubtitleRemovalError("无法打开 FFmpeg 视频输入管道。")
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            cleaned, _ = clean_frame(frame, args)
            process.stdin.write(cleaned.tobytes())
            index += 1
            if index == 1 or index % 120 == 0 or index == total:
                print(f"SUBTITLE_REMOVE {index}/{total}", flush=True)
    finally:
        capture.release()
        process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise SubtitleRemovalError(f"FFmpeg 编码失败：exit={return_code}")
    if index == 0:
        raise SubtitleRemovalError("输入视频没有可处理的视频帧。")


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve(strict=True)
    destination = args.output.expanduser().resolve()
    if not 0 <= args.roi_top < 1:
        raise SubtitleRemovalError("--roi-top 必须在 [0,1) 范围内。")
    if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        clean_image(source, destination, args)
    else:
        clean_video(source, destination, args)
    print(f"SUBTITLE_REMOVE complete={destination}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
