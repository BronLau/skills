#!/usr/bin/env python3
"""
视频逆向复刻（创量版）- 参考图生成（默认 qwen-image-3.0-pro，角色卡走抽帧 I2I）

自动解析第一步产出（分镜大纲.md）里的【角色卡 - XXX】/【场景卡 - XXX】，
逐卡调用生图生成参考图，并输出 index.json（卡名 → 图片路径映射）。
角色卡默认配合 extract_char_frames.py 抽出的原片帧做 I2I（面部/发型/服装细节以帧为准，
文字与帧冲突时一律以帧为准）；selected.json 的值可为单帧路径或 ≤3 帧路径列表（多帧同时作参考图，
给模型更多服装证据）。场景卡仍走纯文生图。

用法:
  python3 generate_ref_images.py --cards 分镜大纲.md --output ./refs --frames-index refs/frames/selected.json
  python3 generate_ref_images.py --cards 分镜大纲.md --output ./refs --only "猪头妖" --frames-index refs/frames/selected.json
  python3 generate_ref_images.py --cards 分镜大纲.md --output ./refs --ref-image xxx.jpg   # 手动指定统一参考图（覆盖 frames-index）

环境变量: DASHSCOPE_API_KEY（必须）
依赖: pip install dashscope
"""

import os
import re
import sys
import json
import time
import base64
import argparse
import urllib.request

try:
    import dashscope
    from dashscope import MultiModalConversation
    from dashscope.aigc.image_generation import ImageGeneration
    from dashscope.api_entities.dashscope_response import Message
except ImportError:
    print("ERROR: dashscope SDK 未安装。请运行: pip install dashscope -i https://mirrors.aliyun.com/pypi/simple/")
    sys.exit(1)

dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"

CARD_HEAD_RE = re.compile(r"【(角色卡|场景卡)\s*[-—–]\s*([^】]+)】")
STOP_PREFIXES = ("标签：", "标签:", "出现镜头", "适用镜头", "---", "===")

# size 表达因模型家族而异：wan 系用档位（如 2K），qwen-image 系用 宽*高
QWEN_IMAGE_SIZE_MAP = {"2K": "2048*2048", "1K": "1328*1328"}

# I2I 模式指令前缀：帧锁定身份与服装细节，文本只锁构图/姿态/背景/表情
I2I_PREFIX = ("参考图是该角色在原片中的真实截图帧：面部特征、发型、服装必须与参考图中的人物严格一致，"
              "不得美化或替换长相；当以下文字描述与参考帧在服装细节上不一致时（款式、纹理、针织粗细、颜色、"
              "开合状态、纽扣数量与样式、配饰等），一律以参考帧为准，文字只规定构图、姿态、背景与表情。"
              "构图、姿态、背景与画面风格按以下文字描述执行。")


def is_qwen_image(model):
    return model.startswith("qwen-image")


def load_ref_image(path_or_url):
    """参考图转 base64 data URL（比外链稳定）；单边 <240px 时自动放大（模型硬要求 ≥240px）"""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    if path_or_url.startswith("data:"):
        return path_or_url
    if not os.path.exists(path_or_url):
        print(f"ERROR: 参考图不存在: {path_or_url}")
        sys.exit(1)
    with open(path_or_url, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(path_or_url)[1].lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp"}.get(ext, "image/png")
    try:
        from PIL import Image
        import io
        img = Image.open(path_or_url)
        w, h = img.size
        if min(w, h) < 240:
            scale = 240 / min(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            raw = buf.getvalue()
            mime = "image/jpeg"
            print(f"  ! 参考图过小({w}x{h})，已放大至 {img.size[0]}x{img.size[1]}")
    except ImportError:
        pass
    return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"


def parse_cards(md_text):
    """解析角色卡/场景卡：返回 [{type, name, prompt}]"""
    cards = []
    matches = list(CARD_HEAD_RE.finditer(md_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        block = md_text[m.end():end]
        prompt_lines = []
        for line in block.splitlines():
            line = line.strip().strip("*").strip("`").strip()
            if not line:
                if prompt_lines:  # prompt 已开始，空行后遇到标签行才停，这里继续容忍
                    continue
                continue
            if line.startswith(STOP_PREFIXES) or line.startswith("#"):
                break
            if line == "[完整文生图 Prompt]":  # 范例占位行，跳过
                continue
            prompt_lines.append(line)
        prompt = " ".join(prompt_lines).strip()
        if prompt:
            cards.append({"type": m.group(1), "name": m.group(2).strip(), "prompt": prompt})
    return cards


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\s（）()]+', "_", name).strip("_")


def safe_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


def gen_one(api_key, prompt, model, n, size, ref_image=None):
    """文生图单次调用，返回图片 URL 列表（按模型家族路由不同接口；ref_image 仅 qwen-image 系走 I2I）"""
    try:
        if is_qwen_image(model):
            # qwen-image 系列走 MultiModalConversation；size 用 宽*高
            qsize = QWEN_IMAGE_SIZE_MAP.get(size, size if "*" in size else "2048*2048")
            if not (model.startswith("qwen-image-2.0") or model.startswith("qwen-image-3")):
                n = 1  # 老的 max/plus/基础版固定单张，靠外层补抽凑数；2.0/3.0 支持 n=1~6
            if ref_image:
                refs = ref_image if isinstance(ref_image, list) else [ref_image]
                content = [{"image": r} for r in refs[:3]] + [{"text": prompt}]  # I2I：1-3 图 + 恰好 1 文本
            else:
                content = [{"text": prompt}]
            rsp = MultiModalConversation.call(
                api_key=api_key, model=model,
                messages=[{"role": "user", "content": content}],
                result_format="message", watermark=False,
                n=min(n, 6), size=qsize,
                **({"prompt_extend": False} if ref_image else {}),
            )
        else:
            if ref_image:
                print("  ! wan 系模型未接参考图 I2I，忽略 --ref-image")
            rsp = ImageGeneration.call(
                model=model, api_key=api_key,
                messages=[Message(role="user", content=[{"text": prompt}])],
                watermark=False, n=n, size=size,
            )
        if rsp.status_code == 200:
            urls = []
            for choice in rsp.output.choices:
                content = choice["message"]["content"] if not hasattr(choice, "message") else choice.message.content
                for item in content:
                    if item.get("type") == "image" or "image" in item:
                        urls.append(item["image"])
            return urls
        print(f"  ✗ API 错误: status={rsp.status_code}, message={rsp.message}")
    except Exception as e:
        print(f"  ✗ 调用异常: {e}")
    return []


def gen_many(api_key, prompt, model, n, size, ref_image=None):
    """确保每张卡拿到 n 张候选图：单次 API 返回不足时自动补抽"""
    urls = []
    fails = 0
    while len(urls) < n and fails < 3:
        got = gen_one(api_key, prompt, model, n - len(urls), size, ref_image=ref_image)
        if got:
            urls.extend(got)
            fails = 0
        else:
            fails += 1
        if len(urls) < n:
            time.sleep(2)  # 频控间隔
    if len(urls) < n:
        print(f"  ! 补抽后仍只拿到 {len(urls)}/{n} 张")
    return urls[:n]


def main():
    parser = argparse.ArgumentParser(description="角色卡/场景卡 → 参考图")
    parser.add_argument("--cards", required=True, help="第一步产出的 markdown 文件（含角色卡/场景卡）")
    parser.add_argument("--output", required=True, help="参考图输出目录")
    parser.add_argument("--only", default=None, help="只生成卡名包含该关键词的卡")
    parser.add_argument("--model", default="qwen-image-3.0-pro")
    parser.add_argument("--n", type=int, default=3, help="每张卡抽几张候选图（默认 3，供用户挑选）")
    parser.add_argument("--size", default="2K")
    parser.add_argument("--ref-image", default=None,
                        help="角色换脸/换角色参考图（本地路径或URL）：仅角色卡走 I2I，外观以参考图为准；场景卡不受影响")
    parser.add_argument("--frames-index", default=None,
                        help="抽帧定稿映射 selected.json（{\"角色卡-名\": 帧路径 或 [帧路径列表(≤3)]}），"
                             "角色卡按各自帧走 I2I（多帧时全部作为参考图）；--ref-image 优先于它")
    args = parser.parse_args()

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY 环境变量未设置")
        sys.exit(1)

    ref_image = load_ref_image(args.ref_image) if args.ref_image else None
    frames_map = {}
    if args.frames_index:
        with open(args.frames_index, "r", encoding="utf-8") as f:
            frames_map = json.load(f)

    with open(args.cards, "r", encoding="utf-8") as f:
        cards = parse_cards(f.read())
    if args.only:
        cards = [c for c in cards if args.only in c["name"]]
    if not cards:
        print("ERROR: 未解析到任何角色卡/场景卡（或 --only 无匹配）。请检查文件格式：卡头须为【角色卡 - 名字】/【场景卡 - 名字】")
        sys.exit(1)

    print(f"共解析到 {len(cards)} 张卡：" + "、".join(f"{c['type']}-{c['name']}" for c in cards))
    os.makedirs(args.output, exist_ok=True)

    index_path = os.path.join(args.output, "index.json")
    index = {}
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)

    for i, card in enumerate(cards):
        card_ref = None
        prompt = card["prompt"]
        key = f"{card['type']}-{card['name']}"
        frame_paths = None
        if card["type"] == "角色卡":
            frame_val = args.ref_image or frames_map.get(key)
            if frame_val:
                frame_paths = frame_val if isinstance(frame_val, list) else [frame_val]
                frame_paths = frame_paths[:3]  # qwen-image I2I 参考图上限 3 张
                card_ref = [load_ref_image(p) for p in frame_paths]
                prompt = I2I_PREFIX + prompt
        suffix = f"（I2I 参考帧: {'、'.join(frame_paths)}）" if card_ref else ""
        print(f"\n[{i+1}/{len(cards)}] {card['type']} - {card['name']}{suffix}")
        urls = gen_many(api_key, prompt, args.model, args.n, args.size, ref_image=card_ref)
        paths = []
        for j, url in enumerate(urls):
            save = safe_path(os.path.join(args.output, f"{card['type']}_{sanitize(card['name'])}_{j+1}.png"))
            try:
                urllib.request.urlretrieve(url, save)
                print(f"  ✓ 已保存: {save}")
                paths.append(save)
            except Exception as e:
                print(f"  ✗ 下载失败: {e}\n    URL(24h有效，可手动下载): {url}")
        if paths:
            index[f"{card['type']}-{card['name']}"] = paths
        if i < len(cards) - 1:
            time.sleep(2)  # 频控间隔

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"\n索引已更新: {index_path}（共 {len(index)} 张卡有图）")


if __name__ == "__main__":
    main()
