#!/usr/bin/env python3
"""
视频逆向复刻 - 分步视频分析 CLI

  step1: 选角与置景 → 分镜大纲 + 角色卡 + 场景卡（默认 qwen3.8-max 看片直出）
  step2: 运镜与调度 → 生成段规划 + 分幕镜头细节块（布景/光影/逐秒时间轴）+ 切镜标注 初稿（默认 qwen3.5-omni-plus，听音+看片）
  refine: 终审精修 → 以 step2 初稿为底，结合画面校对增强，输出镜头细节块终稿（默认 qwen3.8-max 看片；音频信息以初稿为唯一事实）
  step3: 提示词撰写 → 把 refine 定稿的镜头细节块无损翻译成段级生视频 Prompt + 参考图清单 + 首帧合成指引（默认 qwen3.8-max，纯文本不吃视频）

用法:
  python3 analyze_video.py step1 --video <本地路径或http链接> --output 分镜大纲.md
  python3 analyze_video.py step2 --video <同一视频> --step1-file 分镜大纲.md --output 详细分镜初稿.md
  python3 analyze_video.py refine --video <同一视频> --step1-file 分镜大纲.md --draft-file 详细分镜初稿.md --output 详细分镜.md
  python3 analyze_video.py step3 --step2-file 详细分镜.md --step1-file 分镜大纲.md --refs-index refs/index.json --output 生视频提示词.md

环境变量: DASHSCOPE_API_KEY（必须）
依赖: pip install openai
"""

import os
import sys
import base64
import argparse

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai SDK 未安装。请运行: pip install openai -i https://mirrors.aliyun.com/pypi/simple/")
    sys.exit(1)

try:
    import httpx
    HTTP_TIMEOUT = httpx.Timeout(600.0, connect=60.0)  # 端点 TLS 握手偶发 >5s，默认 connect 超时太短会报 APIConnectionError
except ImportError:
    HTTP_TIMEOUT = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(SCRIPT_DIR, "..", "prompts")
BASE64_LIMIT = 10 * 1024 * 1024  # base64 编码后须 < 10MB

MIME_MAP = {
    ".mp4": "video/mp4", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
}


def load_system_prompt(step):
    fname = "step2_refine_system.md" if step == "refine" else f"{step}_system.md"
    path = os.path.join(PROMPT_DIR, fname)
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


def resolve_video_url(video):
    """http(s) 链接直接用；本地文件编码为 base64 data URI（编码后须 <10MB）"""
    if video.startswith("http://") or video.startswith("https://"):
        return video
    if not os.path.exists(video):
        print(f"ERROR: 视频文件不存在: {video}")
        sys.exit(1)
    with open(video, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    if len(b64) > BASE64_LIMIT:
        print(f"ERROR: 视频 base64 编码后 {len(b64)/1024/1024:.1f}MB，超过 10MB 上限。请先压缩：")
        print(f'  ffmpeg -i "{video}" -vf "scale=-2:480" -b:v 800k -c:a aac -b:a 64k compressed.mp4')
        sys.exit(1)
    mime = MIME_MAP.get(os.path.splitext(video)[1].lower(), "video/mp4")
    return f"data:{mime};base64,{b64}"


def analyze(video_url, system_prompt, user_prompt, model):
    client_kwargs = dict(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    if HTTP_TIMEOUT is not None:
        client_kwargs["timeout"] = HTTP_TIMEOUT
    client = OpenAI(**client_kwargs)
    # step3 为纯文本转写，不吃视频；其余步骤带 video_url
    if video_url:
        user_content = [
            {"type": "video_url", "video_url": {"url": video_url}},
            {"type": "text", "text": user_prompt},
        ]
    else:
        user_content = [{"type": "text", "text": user_prompt}]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    # 两个家族都用 stream=True；omni 必须传 modalities，非 omni 的 qwen3.x 须显式关 thinking
    kwargs = dict(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
    )
    if "omni" in model:
        kwargs["modalities"] = ["text"]
    else:
        kwargs["extra_body"] = {"enable_thinking": False}
    completion = client.chat.completions.create(**kwargs)
    text = ""
    for chunk in completion:
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta.content:
                print(delta.content, end="", flush=True)
                text += delta.content
        elif chunk.usage:
            print(f"\n\n--- tokens: in={chunk.usage.prompt_tokens} out={chunk.usage.completion_tokens} ---")
    return text


def main():
    parser = argparse.ArgumentParser(description="视频逆向复刻·分步分析")
    parser.add_argument("step", choices=["step1", "step2", "refine", "step3"])
    parser.add_argument("--video", default=None, help="本地视频路径或 http(s) 链接（step1/step2/refine 必填；step3 不需要）")
    parser.add_argument("--step1-file", default=None, help="step2/refine/step3：第一步产出文件路径")
    parser.add_argument("--draft-file", default=None, help="refine 必填：step2 omni 初稿文件路径")
    parser.add_argument("--step2-file", default=None, help="step3 必填：第二步定稿《详细分镜》文件路径")
    parser.add_argument("--refs-index", default=None, help="step3 可选：参考图 index.json 路径（提供则参考图清单含真实文件路径）")
    parser.add_argument("--output", required=True, help="结果保存路径（已存在时自动加后缀，不覆盖）")
    parser.add_argument("--model", default=None,
                        help="默认按步骤自动选择：step1=qwen3.8-max，step2=qwen3.5-omni-plus，refine/step3=qwen3.8-max")
    args = parser.parse_args()
    model = args.model or {"step1": "qwen3.8-max", "step2": "qwen3.5-omni-plus",
                           "refine": "qwen3.8-max", "step3": "qwen3.8-max"}[args.step]

    if not os.getenv("DASHSCOPE_API_KEY"):
        print("ERROR: DASHSCOPE_API_KEY 环境变量未设置")
        sys.exit(1)

    system_prompt = load_system_prompt(args.step)

    if args.step == "step1":
        user_prompt = ("请按照系统提示词要求，输出第一步的分镜大纲、角色卡、场景卡。"
                       "角色卡要精炼——抓最具辨识度的核心特征，看不清的不要编造。")
        video_url = resolve_video_url(args.video)
    elif args.step == "step2":
        if not args.video:
            print("ERROR: step2 需要 --video")
            sys.exit(1)
        if not args.step1_file or not os.path.exists(args.step1_file):
            print("ERROR: step2 需要 --step1-file 指向第一步产出文件")
            sys.exit(1)
        with open(args.step1_file, "r", encoding="utf-8") as f:
            step1_result = f.read()
        user_prompt = (
            "以下是第一步已经确定的分镜大纲、角色卡、场景卡"
            "（人物参考图与背景参考图已据此生成完毕）：\n\n"
            "===== 第一步产出 开始 =====\n"
            f"{step1_result}\n"
            "===== 第一步产出 结束 =====\n\n"
            "请严格沿用以上角色卡名与场景卡名，结合视频，按生成段方案输出：\n"
            "1. 先输出【生成段规划表】——把连续镜头打包成生成段（每段总时长≤30秒，禁止一镜一段；剧情连贯的镜头尽量打包满），"
            "列出每段包含的镜头、段总时长、出场角色卡/场景卡；\n"
            "2. 按幕输出：每幕开头【本幕角色形象汇总】，随后为每个镜头输出【布景与站位】+【光影】+"
            "【逐秒时间轴】（动作+对话+声效+光影同步；不写字幕），相邻镜头之间写一行【切镜】标注"
            "（切换方式、末帧→首帧空间关系、动势是否延续）。\n"
            "注意：本步只写镜头细节块，把布景/道具/站位/光影写全写具体；"
            "不要输出 ★视频生成 Prompt，也不要输出首帧合成指引（那是下一步的专职工作）。"
        )
        video_url = resolve_video_url(args.video)
    elif args.step == "step3":
        if not args.step2_file or not os.path.exists(args.step2_file):
            print("ERROR: step3 需要 --step2-file 指向第二步定稿《详细分镜》")
            sys.exit(1)
        with open(args.step2_file, "r", encoding="utf-8") as f:
            step2_result = f.read()
        step1_block = ""
        if args.step1_file and os.path.exists(args.step1_file):
            with open(args.step1_file, "r", encoding="utf-8") as f:
                step1_block = ("===== 第一步产出（仅供核对卡名）开始 =====\n"
                               f"{f.read()}\n"
                               "===== 第一步产出 结束 =====\n\n")
        refs_block = ""
        if args.refs_index and os.path.exists(args.refs_index):
            with open(args.refs_index, "r", encoding="utf-8") as f:
                refs_block = ("===== 参考图索引 index.json（卡名→文件路径，取第一个为定稿）开始 =====\n"
                              f"{f.read()}\n"
                              "===== 参考图索引 结束 =====\n\n")
        else:
            refs_block = "（未提供参考图索引：参考图清单中文件路径一栏写'见 refs/index.json 对应卡名'即可。）\n\n"
        user_prompt = (
            f"{step1_block}"
            "===== 详细分镜（第二步定稿，ground truth）开始 =====\n"
            f"{step2_result}\n"
            "===== 详细分镜 结束 =====\n\n"
            f"{refs_block}"
            "请按照系统提示词，把上述每个生成段的镜头细节块无损翻译成 ★视频生成 Prompt，"
            "并为每段附【参考图清单】与【首帧合成指引】。"
            "严守：台词/声效/字幕原样搬运一字不改；布景/道具/站位/光影从细节块搬全不漏；"
            "角色外观只写'完全按图N参考图'不复述长相；多人同框逐人写位置/朝向/接触/视线。"
            "输出前逐条自检补齐，只交付最终版本。"
        )
        video_url = None  # step3 纯文本转写，不吃视频
    else:  # refine
        if not args.video:
            print("ERROR: refine 需要 --video")
            sys.exit(1)
        if not args.step1_file or not os.path.exists(args.step1_file):
            print("ERROR: refine 需要 --step1-file 指向第一步产出文件")
            sys.exit(1)
        if not args.draft_file or not os.path.exists(args.draft_file):
            print("ERROR: refine 需要 --draft-file 指向 step2 omni 初稿文件")
            sys.exit(1)
        with open(args.step1_file, "r", encoding="utf-8") as f:
            step1_result = f.read()
        with open(args.draft_file, "r", encoding="utf-8") as f:
            draft_result = f.read()
        user_prompt = (
            "以下是第一步已经确定的分镜大纲、角色卡、场景卡：\n\n"
            "===== 第一步产出 开始 =====\n"
            f"{step1_result}\n"
            "===== 第一步产出 结束 =====\n\n"
            "以下是全模态模型（可听音频）产出的《详细分镜》初稿：\n\n"
            "===== 初稿 开始 =====\n"
            f"{draft_result}\n"
            "===== 初稿 结束 =====\n\n"
            "请结合视频画面对初稿做终审精修，直接输出完整终稿。"
            "牢记：初稿中的台词/声效/BGM 是唯一音频事实，一字不改；说话人归属默认保留初稿归属（音频事实），仅在指称对象矛盾等硬情况下才改判；"
            "视觉描述以画面为准校对增强；卡名与身体部位逐字对照第一步卡片；"
            "复核生成段规划（≤30秒/段、禁止一镜一段、剧情连贯镜头不过碎分段、【切镜】行齐全）；"
            "把每个镜头的布景/道具/站位/光影写全写具体。"
            "本步只输出镜头细节块，不要输出 ★视频生成 Prompt，也不要输出首帧合成指引。"
        )
        video_url = resolve_video_url(args.video)

    result = analyze(video_url, system_prompt, user_prompt, model)

    if result:
        out = safe_output_path(args.output)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"\n===== {args.step} 结果已保存到: {out} =====")
    else:
        print("\nERROR: 未获得模型输出")
        sys.exit(1)


if __name__ == "__main__":
    main()
