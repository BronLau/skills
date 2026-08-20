#!/usr/bin/env python3
"""
视频逆向复刻 - 分镜视频生成（参考生视频 R2V，默认 wan3.0-video，--model 可覆盖）

用角色/产品/场景参考图 + 分镜 Prompt 生成单个生成段的视频。
注意：Prompt 中须用「图1」「图2」引用参考图，序号 = --refs 传入顺序。

用法:
  python3 generate_video.py --prompt "图1（猪头妖）在图2（盘丝洞广场）中奔跑..." \
      --refs refs/角色卡_猪头妖_1.png refs/场景卡_盘丝洞广场_1.png \
      --duration 9 --output shots/shot1.mp4
  python3 generate_video.py --prompt-file shot1.txt --refs a.png b.png --duration 5 --output shot1.mp4

环境变量: DASHSCOPE_API_KEY（必须）
依赖: 仅标准库（urllib）
"""

import os
import sys
import json
import time
import base64
import mimetypes
import argparse
import urllib.request
import urllib.error

CREATE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
POLL_INTERVAL = 15  # 官方建议 15s


def encode_image(path):
    """本地图片 → base64 data URI（比外链 URL 稳定）；http(s) 链接原样返回"""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not os.path.exists(path):
        print(f"ERROR: 参考图不存在: {path}")
        sys.exit(1)
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def http_json(url, method="GET", headers=None, body=None, timeout=120):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: HTTP {e.code}: {detail}")
        sys.exit(1)


def safe_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}_{i}{ext}"):
        i += 1
    return f"{root}_{i}{ext}"


def main():
    parser = argparse.ArgumentParser(description="参考图 + 分镜Prompt → 参考生视频（R2V），默认 wan3.0-video")
    parser.add_argument("--model", default="wan3.0-video",
                        help="视频生成模型名（默认 wan3.0-video，可覆盖）")
    parser.add_argument("--prompt", default=None, help="分镜生视频 Prompt（与 --prompt-file 二选一）")
    parser.add_argument("--prompt-file", default=None, help="从文件读取 Prompt")
    parser.add_argument("--refs", nargs="+", default=[], help="角色/产品/场景参考图路径或URL（≤10张，顺序即 图1/图2/...）")
    parser.add_argument("--duration", type=int, default=5, help="输出时长秒 [2,30]，按镜头时长向上取整；-1=智能时长")
    parser.add_argument("--resolution", default="1080P", choices=["1080P", "720P", "480P"])
    parser.add_argument("--ratio", default="adaptive")
    parser.add_argument("--no-audio", action="store_true", help="不输出音轨")
    parser.add_argument("--output", required=True, help="视频保存路径（不覆盖已有文件）")
    parser.add_argument("--timeout", type=int, default=3600, help="轮询总超时秒")
    parser.add_argument("--task-id", default=None, help="跳过创建，直接轮询/下载已有任务（task_id 24h 内有效）")
    args = parser.parse_args()

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY 环境变量未设置")
        sys.exit(1)

    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
    if not prompt and not args.task_id:
        print("ERROR: 需要 --prompt 或 --prompt-file")
        sys.exit(1)
    if len(args.refs) > 10:
        print("ERROR: reference_image 最多 10 张")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",  # 必须，缺失会报不支持同步调用
    }

    task_id = args.task_id
    if not task_id:
        media = [{"type": "reference_image", "url": encode_image(p)} for p in args.refs]
        body = {
            "model": args.model,
            "input": {"prompt": prompt, "media": media} if media else {"prompt": prompt},
            "parameters": {
                "resolution": args.resolution,
                "ratio": args.ratio,
                "duration": args.duration,
                "audio": not args.no_audio,
                "watermark": False,
            },
        }
        print(f"创建任务：{len(args.refs)} 张参考图, duration={args.duration}s, {args.resolution}")
        rsp = http_json(CREATE_URL, "POST", headers, body)
        task_id = rsp.get("output", {}).get("task_id")
        if not task_id:
            print(f"ERROR: 未拿到 task_id: {rsp}")
            sys.exit(1)
        # 立即打印 task_id：中断后可用 --task-id 恢复（24h 内有效）
        print(f"task_id: {task_id}")

    # 轮询（约10分钟/段属正常）
    start = time.time()
    poll_headers = {"Authorization": f"Bearer {api_key}"}
    while True:
        if time.time() - start > args.timeout:
            print(f"ERROR: 超时（{args.timeout}s）。稍后可用 --task-id {task_id} 恢复下载")
            sys.exit(1)
        rsp = http_json(TASK_URL.format(task_id=task_id), "GET", poll_headers)
        status = rsp.get("output", {}).get("task_status")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            print(f"ERROR: 任务{status}: {json.dumps(rsp.get('output', {}), ensure_ascii=False)}")
            sys.exit(1)
        print(f"  [{int(time.time()-start)}s] {status} ...")
        time.sleep(POLL_INTERVAL)

    video_url = rsp["output"].get("video_url") or rsp["output"].get("results", [{}])[0].get("url")
    if not video_url:
        print(f"ERROR: 未找到视频 URL: {json.dumps(rsp['output'], ensure_ascii=False)}")
        sys.exit(1)

    out = safe_path(args.output)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    print(f"下载视频（URL 24h 有效）...")
    urllib.request.urlretrieve(video_url, out)
    print(f"✓ 已保存: {out}  (task_id: {task_id})")


if __name__ == "__main__":
    main()
