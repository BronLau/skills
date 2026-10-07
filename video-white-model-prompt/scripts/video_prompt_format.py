"""Public video-prompt presentation; precise facts stay in the fact contract."""

from __future__ import annotations

import math
import re
from typing import Any


FORMAT_VERSION = "public_v1"
HEADINGS = ("主体与场景定义：", "整体视听设定：", "分镜与声音：")
SHOT_PATTERN = re.compile(
    r"(?m)^镜头(?P<number>\d+)｜(?P<start>\d{2}:\d{2}) - (?P<end>\d{2}:\d{2})$"
)
DIALOGUE = re.compile(r"\{[^{}]*\}")
NO_SUBTITLES = "全片不生成字幕。"


def is_public_prompt(text: str) -> bool:
    return any(re.search(r"(?m)^" + re.escape(heading) + r"$", text) for heading in HEADINGS)


def split_sections(text: str) -> tuple[str, str, str]:
    pattern = re.compile(r"(?m)^(?:" + "|".join(map(re.escape, HEADINGS)) + r")$")
    matches = list(pattern.finditer(text))
    if [match.group(0) for match in matches] != list(HEADINGS):
        raise ValueError("公共正文必须且只能按顺序包含三个固定标题。")
    if text[:matches[0].start()].strip():
        raise ValueError("公共正文第一个标题之前不能有额外内容。")
    parts = tuple(
        text[match.end():matches[i + 1].start() if i < 2 else len(text)].strip()
        for i, match in enumerate(matches)
    )
    if not all(parts):
        raise ValueError("公共正文不能包含空章节。")
    return parts[0], parts[1], parts[2]


def join_sections(definitions: str, overall: str, shots: str) -> str:
    return "\n\n".join(
        heading + "\n" + content.strip()
        for heading, content in zip(HEADINGS, (definitions, overall, shots))
    )


def timecode(seconds: float, *, end: bool = False) -> str:
    # Display is deliberately approximate: two quick cuts may share an interval.
    value = math.ceil(seconds) if end else math.floor(seconds)
    return f"{value // 60:02d}:{value % 60:02d}"


def display_interval(start: float, end: float) -> str:
    return f"{timecode(start)} - {timecode(end, end=True)}"


def replace_labels(text: str, labels: list[str]) -> str:
    """Replace known entity tokens only; spoken literal punctuation is untouched."""
    names = {label: label[1:-1] for label in [*labels, "<旁白>"]}
    pattern = re.compile("|".join(map(re.escape, sorted(names, key=len, reverse=True))))
    parts = re.split(r"(\{[^{}]*\})", text)
    return "".join(
        part if part.startswith("{") else pattern.sub(lambda m: names[m.group(0)], part)
        for part in parts
    )


def render_audio(audio: str, labels: list[str]) -> str:
    # Schema 3 supplies sound containers already. This adapter also accepts saved
    # v1/v2 sound prefixes without changing their dialogue or speaker attribution.
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    # Only merge adjacent identical speaking contexts with punctuation between
    # them. An explicit interruption, changed delivery or other speaker stays put.
    continuous = re.compile(
        r"(?P<prefix><[^<>\n]+>（(?:画内|画外)、(?:口型同步|无需口型)）[^：；。{}<>\n]{0,24}：)"
        r"\{(?P<first>[^{}]+)\}[\s。；;，,]*(?P=prefix)\{(?P<second>[^{}]+)\}"
    )
    while True:
        merged = continuous.sub(lambda m: m.group("prefix") + "{" + m.group("first") + m.group("second") + "}", audio)
        if merged == audio:
            break
        audio = merged
    text = DIALOGUE.sub(protect, replace_labels(audio, labels))

    def sound(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        if value in {"无", "没有", "无声"}:
            return ""
        opening, closing = ("(", ")") if match.group("kind") == "音乐" else ("<", ">")
        if value.startswith(opening) and value.endswith(closing):
            return value
        return opening + value + closing

    text = re.sub(
        r"(?P<kind>音乐|音效|环境声)[:：]\s*(?P<value>[^。；\n\x00]+)", sound, text
    )
    for i, value in enumerate(protected):
        text = text.replace(f"\x00{i}\x00", value)
    return text.strip()


def sentence(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).rstrip("。；") + "。" if value.strip() else ""


def render_prompt(
    facts: dict[str, Any], definitions: dict[str, str], overrides: dict[tuple[int, int], str]
) -> str:
    labels = [subject["label"] for subject in facts["subjects"]]
    rank = {"product": 0, "character": 1, "operator": 1, "scene": 2, "object": 3}
    ordered = sorted(facts["subjects"], key=lambda item: rank[item["kind"]])
    definition_block = "\n".join(
        replace_labels(definitions[subject["label"]], labels) for subject in ordered
    )
    overall = facts.get("overall_av") or {}
    # The first paragraph is visual/policy; the second is audio. This lets the
    # existing visual-only remedy remove sound without removing acting beats.
    visual = replace_labels(str(overall.get("visual") or ""), labels)
    global_audio = replace_labels(str(overall.get("audio") or ""), labels)
    sections: list[str] = []
    for segment in facts["segments"]:
        has_captions = any(
            re.search(r"〖[^〖〗]+〗", str(shot.get(field) or ""))
            for shot in segment["shots"]
            for field in ("composition", "scene_light")
        ) or any(
            re.search(r"〖[^〖〗]+〗", beat["action"])
            for shot in segment["shots"] for beat in shot["beats"]
        )
        subtitle_policy = "按分镜中的字幕标记呈现指定字幕，其余不生成字幕。" if has_captions else NO_SUBTITLES
        overall_block = sentence(visual) + "\n" + subtitle_policy
        if global_audio:
            overall_block += "\n\n" + sentence(global_audio)
        shots: list[str] = []
        for shot in segment["shots"]:
            title = f"镜头{shot['index']}｜{display_interval(shot['start_seconds'], shot['end_seconds'])}"
            empty = {"", "无", "没有", "不适用"}
            framing = "，".join(
                shot[key].strip().rstrip("。，")
                for key in ("shot_scale", "camera", "composition")
                if shot[key].strip() not in empty
            )
            static = [framing, shot["scene_light"]]
            if shot["visible_body_range"].strip() not in empty:
                static.append(f"画面呈现{shot['visible_body_range']}")
            if shot["entry_exit"].strip() not in {"无", "没有", "无主体进出场"}:
                static.append(shot["entry_exit"])
            visuals = "".join(sentence(part) for part in static if part.strip() not in empty)
            beats = shot["beats"]
            for beat in beats:
                # Keep timing for meaningful intra-shot stages; never invent a cut.
                timing = display_interval(beat["start_seconds"], beat["end_seconds"]) + "，" if len(beats) > 1 else ""
                visuals += timing + sentence(str(beat["action"]))
            block = title + "\n" + replace_labels(visuals, labels)
            audio = overrides.get((int(segment["index"]), int(shot["index"])), str(shot["audio"]))
            if audio.strip():
                block += "\n\n" + render_audio(audio, labels)
            shots.append(block)
        body = join_sections(definition_block, overall_block, "\n\n".join(shots))
        if len(facts["segments"]) > 1:
            body = (
                f"【第{segment['index']}段提示词（{segment['duration_seconds']}秒，"
                f"对齐参考视频{segment['source_start_seconds']}-{segment['source_end_seconds']}秒）】\n" + body
            )
        sections.append(body)
    return "\n\n".join(sections)


def seconds(value: str) -> int:
    minutes, secs = map(int, value.split(":"))
    if secs >= 60:
        raise ValueError("镜头时间的秒数必须小于 60。")
    return minutes * 60 + secs


def validate_body(body: str, duration: float, segment: dict[str, Any] | None = None) -> None:
    definitions, overall, shots = split_sections(body)
    if "```" in body:
        raise ValueError("提交正文不得包含 Markdown 代码围栏。")
    matches = list(SHOT_PATTERN.finditer(shots))
    if not matches or shots[:matches[0].start()].strip():
        raise ValueError("分镜与声音必须以镜头1开始。")
    cursor, previous_start, previous_end = 0, 0, 0
    for i, match in enumerate(matches, start=1):
        start, end = seconds(match.group("start")), seconds(match.group("end"))
        if int(match.group("number")) != i:
            raise ValueError("镜头编号必须从1连续递增。")
        if start > cursor or start < previous_start or end < previous_end or end <= start or end > math.ceil(duration):
            raise ValueError("镜头展示时间超出范围、倒序或存在缺口。")
        cursor, previous_start, previous_end = max(cursor, end), start, end
        content_end = matches[i].start() if i < len(matches) else len(shots)
        if not shots[match.end():content_end].strip():
            raise ValueError("镜头正文不能为空。")
    if cursor != math.ceil(duration):
        raise ValueError("镜头展示时间未覆盖段尾。")
    if segment is not None:
        expected = [(shot["index"], display_interval(shot["start_seconds"], shot["end_seconds"])) for shot in segment["shots"]]
        actual = [(int(m.group("number")), m.group("start") + " - " + m.group("end")) for m in matches]
        if actual != expected:
            raise ValueError("公共正文的镜头数量、顺序或显示时间与锁定事实不一致。")
    # Spoken content may itself contain punctuation. Validate containers outside
    # the literal content instead of rewriting or rejecting the actual words.
    unspoken = DIALOGUE.sub("{台词}", shots)
    for opening, closing in (("{", "}"), ("(", ")"), ("<", ">"), ("〖", "〗")):
        depth = 0
        for char in unspoken:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
            if depth not in (0, 1):
                raise ValueError("声音或字幕标记不配对。")
        if depth or re.search(re.escape(opening) + r"\s*" + re.escape(closing), unspoken) or (opening == "{" and re.search(r"\{\s*\}", shots)):
            raise ValueError("声音或字幕标记不配对或为空。")
    if re.search(r"(?m)^镜头\d+(?!\d)(?!｜\d{2}:\d{2} - \d{2}:\d{2}$)", shots):
        raise ValueError("镜头标题不符合公共格式。")
    has_captions = re.search(r"〖[^〖〗]+〗", DIALOGUE.sub("", shots))
    if has_captions:
        if "全片不生成字幕" in overall:
            raise ValueError("字幕原文与全片无字幕设置冲突。")
    elif "全片不生成字幕" not in overall:
        raise ValueError("整体视听设定缺少默认无字幕声明。")


def strip_audio(body: str) -> str:
    definitions, overall, shots = split_sections(body)
    overall = overall.split("\n\n", 1)[0]
    matches = list(SHOT_PATTERN.finditer(shots))
    blocks = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(shots)
        block = shots[match.start():end].strip().split("\n\n", 1)[0]
        blocks.append(DIALOGUE.sub("", block).strip())
    return join_sections(definitions, overall, "\n\n".join(blocks))


def add_runtime_references(body: str, materials: list[str], settings: list[str]) -> str:
    definitions, overall, shots = split_sections(body)
    if materials:
        definitions += "\n" + "\n".join(materials)
    if settings:
        overall += "\n\n" + "\n".join(settings)
    return join_sections(definitions, overall, shots)
