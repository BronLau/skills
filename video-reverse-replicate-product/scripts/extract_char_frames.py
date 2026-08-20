#!/usr/bin/env python3
"""
视频逆向复刻（产品替换版）- 角色抽帧
解析第一步分镜大纲里【角色卡 - XXX】的"出现镜头"与各镜头时间区间，
为每个角色从原视频抽 3 张候选帧，供用户挑选 1 张作为图生图（I2I）的身份参考。

用法:
  python3 extract_char_frames.py --cards 分镜大纲.md --video input.mp4 --output refs/frames

产出:
  refs/frames/帧_<角色名>_1.jpg / _2.jpg / _3.jpg
  refs/frames/frames.json  {"角色卡-<角色名>": [帧路径...]}

用户挑定后由智能体写入 refs/frames/selected.json（同结构，值为单路径），
generate_ref_images.py 通过 --frames-index 读取做 I2I。

依赖: ffmpeg（命令行）
"""

import os
import re
import sys
import json
import subprocess
import argparse

SHOT_RE = re.compile(r"###\s*镜头(\d+)[^\d\(（]*[\(（]\s*(\d{1,2}:\d{2}(?:\.\d+)?)\s*[-—]\s*(\d{1,2}:\d{2}(?:\.\d+)?)\s*[\)）]")
CARD_HEAD_RE = re.compile(r"【角色卡\s*[-—–]\s*([^】]+)】")


def to_sec(ts):
    m, s = ts.split(":")
    return int(m) * 60 + float(s)


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\s（）()]+', "_", name).strip("_")


def parse_shot_ranges(md_text):
    """镜头编号 → (start, end) 秒"""
    ranges = {}
    for m in SHOT_RE.finditer(md_text):
        ranges[int(m.group(1))] = (to_sec(m.group(2)), to_sec(m.group(3)))
    return ranges


def parse_char_cards(md_text):
    """角色卡名 → 出现镜头编号列表"""
    cards = {}
    for m in CARD_HEAD_RE.finditer(md_text):
        name = m.group(1).strip()
        # 在卡块内找"出现镜头：镜头1、镜头4"
        block = md_text[m.end():m.end() + 800]
        mm = re.search(r"出现镜头[：:]\s*([^\n]+)", block)
        if not mm:
            continue
        shots = [int(x) for x in re.findall(r"镜头(\d+)", mm.group(1))]
        if shots:
            cards[name] = shots
    return cards


def pick_timestamps(ranges, shots):
    """为角色选 3 个抽帧时间点：尽量分散在其出场镜头内，避开镜头首尾转场"""
    spans = [ranges[s] for s in shots if s in ranges]
    if not spans:
        return []
    if len(spans) == 1:
        a, b = spans[0]
        return [a + (b - a) * f for f in (0.3, 0.5, 0.7)]
    if len(spans) == 2:
        a, b = spans[0]
        c, d = spans[1]
        return [a + (b - a) * 0.5, c + (d - c) * 0.35, c + (d - c) * 0.65]
    return [a + (b - a) * 0.5 for a, b in spans[:3]]


def extract(video, t, out_path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video,
           "-frames:v", "1", "-q:v", "2", out_path]
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description="角色卡 → 原片候选抽帧（I2I 身份参考）")
    parser.add_argument("--cards", required=True, help="第一步产出的 分镜大纲.md")
    parser.add_argument("--video", required=True, help="原视频路径（或压缩版）")
    parser.add_argument("--output", required=True, help="帧输出目录（如 refs/frames）")
    args = parser.parse_args()

    with open(args.cards, "r", encoding="utf-8") as f:
        md = f.read()
    ranges = parse_shot_ranges(md)
    cards = parse_char_cards(md)
    if not cards:
        print("ERROR: 未解析到任何角色卡的出现镜头。请检查分镜大纲格式。")
        sys.exit(1)
    if not ranges:
        print("ERROR: 未解析到镜头时间区间。请检查分镜大纲格式。")
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)
    frames_index = {}
    for name, shots in cards.items():
        ts = pick_timestamps(ranges, shots)
        if not ts:
            print(f"! {name}: 出现镜头无对应时间区间，跳过")
            continue
        paths = []
        for i, t in enumerate(ts, 1):
            out = os.path.join(args.output, f"帧_{sanitize(name)}_{i}.jpg")
            if not os.path.exists(out):
                extract(args.video, t, out)
            print(f"✓ {name} 候选帧{i}: {out} (t={t:.1f}s)")
            paths.append(out)
        frames_index[f"角色卡-{name}"] = paths

    idx_path = os.path.join(args.output, "frames.json")
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(frames_index, f, ensure_ascii=False, indent=2)
    print(f"\n帧索引已保存: {idx_path}（共 {len(frames_index)} 个角色）")
    print("下一步：把每个角色的候选帧拼对比图给用户挑选，将选定路径写入 selected.json（{卡key: 单路径}）")


if __name__ == "__main__":
    main()
