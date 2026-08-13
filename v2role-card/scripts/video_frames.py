#!/usr/bin/env python3
"""Sample candidate frames or extract exact keyframes from a video."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"缺少 {name}，请先安装 ffmpeg（其中包含 ffprobe）。")
    return path


FFMPEG = require_binary("ffmpeg")
FFPROBE = require_binary("ffprobe")


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SystemExit(f"命令执行失败：{' '.join(command[:3])}\n{detail}")


def probe(video: Path) -> dict:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height,avg_frame_rate",
            "-of",
            "json",
            str(video),
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"无法读取视频：{completed.stderr.strip()}")
    data = json.loads(completed.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise SystemExit("无法取得有效视频时长。")
    return {
        "duration": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "avg_frame_rate": stream.get("avg_frame_rate") or "unknown",
    }


def ensure_video(path_value: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"视频不存在：{path}")
    return path


def ensure_output(path_value: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def auto_interval(duration: float) -> float:
    # Aim for roughly 36–60 candidates while keeping short videos inspectable.
    return max(1.0, min(10.0, math.ceil((duration / 48.0) * 2.0) / 2.0))


def parse_interval(value: str, duration: float) -> float:
    if value == "auto":
        return auto_interval(duration)
    try:
        interval = float(value)
    except ValueError as exc:
        raise SystemExit("--interval 必须为 auto 或正数秒。") from exc
    if interval <= 0:
        raise SystemExit("--interval 必须大于 0。")
    return interval


def format_timestamp(timestamp: float) -> str:
    minutes = int(timestamp // 60)
    seconds = timestamp - minutes * 60
    seconds_text = f"{seconds:05.2f}".rstrip("0").rstrip(".")
    return f"{minutes:02d}:{seconds_text}"


def check_targets(targets: list[Path], force: bool) -> None:
    existing = [path for path in targets if path.exists()]
    if existing and not force:
        examples = "\n".join(str(path) for path in existing[:5])
        raise SystemExit(f"目标文件已存在；请改用新目录或显式传入 --force：\n{examples}")


def rename_sampled_frames(output: Path, interval: float) -> list[tuple[Path, float]]:
    raw_frames = sorted(output.glob("sample_raw_*.jpg"))
    renamed: list[tuple[Path, float]] = []
    for index, raw_path in enumerate(raw_frames):
        timestamp = index * interval
        final_path = output / f"frame_{index + 1:04d}_t{timestamp:08.2f}s.jpg"
        raw_path.replace(final_path)
        renamed.append((final_path, timestamp))
    return renamed


def create_contact_sheets(
    frames: list[tuple[Path, float]], output: Path, columns: int, rows: int
) -> list[Path]:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError:
        print("提示：未安装 Pillow，已跳过 contact sheet；候选帧仍可正常使用。", file=sys.stderr)
        return []

    per_page = columns * rows
    cell_width, cell_height, label_height, gutter = 220, 330, 28, 8
    page_width = gutter + columns * (cell_width + gutter)
    font = ImageFont.load_default()
    sheets: list[Path] = []

    for page_index in range(math.ceil(len(frames) / per_page)):
        page_frames = frames[page_index * per_page : (page_index + 1) * per_page]
        page_rows = max(1, math.ceil(len(page_frames) / columns))
        page_height = gutter + page_rows * (cell_height + label_height + gutter)
        page = Image.new("RGB", (page_width, page_height), "white")
        draw = ImageDraw.Draw(page)
        for local_index, (frame_path, timestamp) in enumerate(page_frames):
            row, column = divmod(local_index, columns)
            x = gutter + column * (cell_width + gutter)
            y = gutter + row * (cell_height + label_height + gutter)
            with Image.open(frame_path) as source:
                thumb = ImageOps.contain(source.convert("RGB"), (cell_width, cell_height))
            frame_bg = Image.new("RGB", (cell_width, cell_height), (238, 238, 238))
            frame_bg.paste(
                thumb,
                ((cell_width - thumb.width) // 2, (cell_height - thumb.height) // 2),
            )
            page.paste(frame_bg, (x, y))
            label = (
                f"{page_index * per_page + local_index + 1:04d}  "
                f"{format_timestamp(timestamp)}"
            )
            draw.text((x + 4, y + cell_height + 6), label, fill="black", font=font)
        sheet_path = output / f"contact_sheet_{page_index + 1:03d}.jpg"
        page.save(sheet_path, quality=92)
        sheets.append(sheet_path)
    return sheets


def sample(args: argparse.Namespace) -> None:
    video = ensure_video(args.video)
    output = ensure_output(args.output)
    metadata = probe(video)
    interval = parse_interval(args.interval, metadata["duration"])

    existing = list(output.glob("frame_*.jpg")) + list(output.glob("contact_sheet_*.jpg"))
    existing += list(output.glob("sample_raw_*.jpg"))
    existing += [output / "metadata.json"] if (output / "metadata.json").exists() else []
    check_targets(existing, args.force)
    if args.force:
        for path in existing:
            path.unlink()

    raw_pattern = output / "sample_raw_%04d.jpg"
    run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            f"fps=1/{interval}",
            "-q:v",
            "2",
            "-y" if args.force else "-n",
            str(raw_pattern),
        ]
    )
    frames = rename_sampled_frames(output, interval)
    if not frames:
        raise SystemExit("没有抽取到候选帧。")
    sheets = create_contact_sheets(frames, output, args.columns, args.rows)

    manifest = {
        "video": str(video),
        **metadata,
        "sample_interval": interval,
        "candidate_count": len(frames),
        "contact_sheets": [path.name for path in sheets],
        "frames": [
            {"file": path.name, "approx_timestamp": timestamp}
            for path, timestamp in frames
        ],
    }
    (output / "metadata.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def extract(args: argparse.Namespace) -> None:
    video = ensure_video(args.video)
    output = ensure_output(args.output)
    metadata = probe(video)
    try:
        timestamps = [float(value) for value in args.timestamps]
    except ValueError as exc:
        raise SystemExit("--timestamps 必须全部为数字秒数。") from exc
    invalid = [value for value in timestamps if value < 0 or value > metadata["duration"]]
    if invalid:
        raise SystemExit(f"时间点超出视频范围 0–{metadata['duration']:.2f}s：{invalid}")

    targets = [
        output / f"keyframe_{index + 1:03d}_t{timestamp:08.2f}s.png"
        for index, timestamp in enumerate(timestamps)
    ]
    check_targets(targets, args.force)
    for timestamp, target in zip(timestamps, targets):
        run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-c:v",
                "png",
                "-y" if args.force else "-n",
                str(target),
            ]
        )
        print(target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从视频抽取候选帧、联系表或精确时间点关键帧。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser("sample", help="按固定或自动间隔抽取候选帧")
    sample_parser.add_argument("video")
    sample_parser.add_argument("output")
    sample_parser.add_argument("--interval", default="auto", help="auto 或秒数，默认 auto")
    sample_parser.add_argument("--columns", type=int, default=6)
    sample_parser.add_argument("--rows", type=int, default=4)
    sample_parser.add_argument("--force", action="store_true")
    sample_parser.set_defaults(func=sample)

    extract_parser = subparsers.add_parser("extract", help="按精确秒数抽取无损 PNG")
    extract_parser.add_argument("video")
    extract_parser.add_argument("output")
    extract_parser.add_argument("--timestamps", nargs="+", required=True)
    extract_parser.add_argument("--force", action="store_true")
    extract_parser.set_defaults(func=extract)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if getattr(args, "columns", 1) <= 0 or getattr(args, "rows", 1) <= 0:
        raise SystemExit("--columns 和 --rows 必须大于 0。")
    args.func(args)


if __name__ == "__main__":
    main()
