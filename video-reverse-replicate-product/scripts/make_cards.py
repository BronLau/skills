#!/usr/bin/env python3
"""
视频逆向复刻 - 角色卡/场景卡/产品卡精修（qwen3.8-max）

在 omni 完成第一步（分镜大纲+初版卡）后，用强文本模型 qwen3.8-max
把角色卡/场景卡改写成对文生图更友好的高质量 Prompt；产品卡只做事实保真与格式整理。
分镜大纲部分原样保留（程序级拼接，模型碰不到分镜正文的写回）。

用法:
  python3 make_cards.py --step1-file 分镜大纲.md --output 分镜大纲_精修.md

环境变量: DASHSCOPE_API_KEY（必须）
依赖: pip install openai
"""

import os
import re
import sys
import argparse

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai SDK 未安装。请运行: pip install openai -i https://mirrors.aliyun.com/pypi/simple/")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(SCRIPT_DIR, "..", "prompts")

# 匹配"第二部分"标题行（### 第二部分 / **第二部分** 等变体）
PART2_RE = re.compile(r"^[#*\s]*第二部分", re.MULTILINE)
CARD_HEAD_RE = re.compile(r"【(角色卡|场景卡|产品卡)\s*[-—–]\s*([^】]+)】")


def load_system_prompt():
    path = os.path.join(PROMPT_DIR, "card_maker_system.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def safe_output_path(path):
    """不覆盖已有文件：存在则自动加 _1/_2 后缀"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


def card_names(text):
    return [(m.group(1), m.group(2).strip()) for m in CARD_HEAD_RE.finditer(text)]


def refine(step1_text, model):
    client = OpenAI(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    user_prompt = (
        "以下是视频理解模型产出的第一步完整结果（分镜大纲+初版角色卡+初版场景卡，可能含产品卡），"
        "其中的卡描述可能已被用户手工修正过（须视为最高优先级事实）：\n\n"
        "===== 第一步产出 开始 =====\n"
        f"{step1_text}\n"
        "===== 第一步产出 结束 =====\n\n"
        "请按系统提示词要求，改写所有角色卡与场景卡为高质量文生图 Prompt；产品卡只做忠实压缩。"
        "输出'### 第二部分：角色卡'、'### 第三部分：场景卡'，以及输入中存在的'### 第四部分：产品卡'，"
        "卡名、卡数及标签/出现镜头/适用镜头/替换对象/替换镜头/状态映射行严格保持原样。"
    )
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": load_system_prompt()},
            {"role": "user", "content": user_prompt},
        ],
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"enable_thinking": False},  # qwen3 系默认开 thinking，此处关闭
    )
    text = ""
    for chunk in completion:
        if chunk.choices:
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                print(delta.content, end="", flush=True)
                text += delta.content
        elif chunk.usage:
            print(f"\n\n--- tokens: in={chunk.usage.prompt_tokens} out={chunk.usage.completion_tokens} ---")
    return text.strip()


def main():
    parser = argparse.ArgumentParser(description="用 qwen3.8-max 精修角色卡/场景卡/产品卡")
    parser.add_argument("--step1-file", required=True, help="omni 第一步产出的分镜大纲.md")
    parser.add_argument("--output", required=True, help="精修后完整文档保存路径（不覆盖已有文件）")
    parser.add_argument("--model", default="qwen3.8-max")
    args = parser.parse_args()

    if not os.getenv("DASHSCOPE_API_KEY"):
        print("ERROR: DASHSCOPE_API_KEY 环境变量未设置")
        sys.exit(1)
    if not os.path.exists(args.step1_file):
        print(f"ERROR: 文件不存在: {args.step1_file}")
        sys.exit(1)

    with open(args.step1_file, "r", encoding="utf-8") as f:
        step1_text = f.read()

    m = PART2_RE.search(step1_text)
    if not m:
        print("ERROR: 未在第一步产出中找到'第二部分'标题，无法拆分卡片区")
        sys.exit(1)
    head = step1_text[: m.start()].rstrip()  # 视频概况 + 分镜大纲，原样保留

    orig_cards = card_names(step1_text)
    if not orig_cards:
        print("ERROR: 第一步产出中未解析到任何角色卡/场景卡/产品卡")
        sys.exit(1)

    refined = refine(step1_text, args.model)
    if not refined:
        print("\nERROR: 未获得模型输出")
        sys.exit(1)
    # 去掉模型可能包裹的代码围栏
    refined = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", refined.strip())

    new_cards = card_names(refined)
    if new_cards != orig_cards:
        print("\nWARNING: 精修后卡类型/卡名/卡数与初版不一致！")
        print(f"  初版: {orig_cards}")
        print(f"  精修: {new_cards}")
        print("  下游按卡名引用，请人工核对后再继续。")

    merged = head + "\n\n---\n\n" + refined + "\n"
    out = safe_output_path(args.output)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(merged)
    print(f"\n===== 精修结果已保存到: {out}（分镜大纲原样保留，卡片区已替换）=====")


if __name__ == "__main__":
    main()
