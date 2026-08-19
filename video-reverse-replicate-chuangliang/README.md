# video-reverse-replicate-chuangliang · 视频逆向复刻（创量版）

输入一段成片视频，输出一套「可复刻的生产素材包」：分镜大纲、角色/场景参考图、可直接投喂视频生成模型的段级提示词，以及（可选的）复刻成片。面向创量客户的真人素材复刻场景定制。

## 与底座版的三点差异

**① 角色参考图 = 原片抽帧 + 角色卡 Prompt 的图生图（I2I）**

底座版用文生图"重画"角色——再准也是"长得像但不是"。创量复刻对人物还原度要求极高，因此本版本改为：按角色卡"出现镜头"从原片抽 3 张候选帧 → 用户挑 1 张定稿 → 把定稿帧和角色卡 Prompt 一起喂给 qwen-image-3.0-pro 做图生图。**帧锁身份**（面部/发型/服装严格照原片真人），**卡 Prompt 锁构图/姿态/背景**（脚本自动在卡 Prompt 前拼接身份锁定指令，并关闭 prompt_extend 防改写）。场景卡仍走文生图。

**② 成片全程无字幕**

即使原片带字幕压屏，复刻成片不叠加任何字幕：第二步时间轴与第三步 ★Prompt 均不写字幕字段（台词只作为音频对白保留），★Prompt 首行统一声明"画面全程无字幕、无任何叠加文字"——无字幕状态依靠提示词控制，不靠后期裁剪。

**③ 生成段上限放宽到 30 秒**

创量链路的视频模型支持单次直接生成 30 秒，生成段时长上限由底座的 ≤15s 放宽到 ≤30s：剧情连贯的连续镜头尽量打包满（接近 30s），段越少跨段衔接点越少、拼接越稳；但不为凑时长把场景/时空跨度大的镜头硬塞进同一段。第二步规划/复核与第三步时间轴规则均已按 30s 调整。

其余流程与底座 `video-reverse-replicate` 完全一致：三步分工（选角置景 → 运镜调度 → 提示词撰写）、omni+max 两阶段精修、音频事实保护、参考图候选挑选检查点；生成段方案沿用底座逻辑，仅段时长上限不同。

## 快速开始

前置要求：

- 环境变量 `DASHSCOPE_API_KEY`（需开通 qwen3.8-max、qwen3.5-omni-plus、qwen-image-3.0-pro、wan3.0-video）
- `pip install openai dashscope pillow -i https://mirrors.aliyun.com/pypi/simple/`
- ffmpeg（视频压缩、抽帧、候选图拼接、成片拼接）
- 本地视频 base64 编码后需 < 10MB（约 7MB 原文件），超限先用 ffmpeg 压缩（抽帧仍用原片高清版）

对 Agent 说一句"用创量版复刻这个视频"并附上视频文件，即可按下述流程走完全程。

## 工作流程

```
原片视频
  │
  ├─ ① step1 选角与置景（qwen3.8-max 看片）
  │     → 分镜大纲.md：逐镜头大纲 + 角色卡 + 场景卡（文生图 Prompt）
  │
  ├─ ② 角色抽帧（extract_char_frames.py，每角色 3 张候选帧）
  │     → refs/frames/：帧_<角色>_1..3.jpg + frames.json
  │     → 用户每角色挑 1 张定稿 → frames/selected.json
  │
  ├─ ③ 参考图生成（generate_ref_images.py --frames-index）
  │     角色卡：定稿帧 I2I；场景卡：文生图。每卡 3 张候选
  │     → refs/：候选图 + index.json（人工每卡挑 1 张定稿）
  │
  ├─ ★ 检查点：人工确认卡片与参考图（角色图重点比对定稿帧长相）后才继续
  │
  ├─ ④ step2 运镜与调度（两阶段，无字幕，生成段≤30s）
  │     A. qwen3.5-omni 听音+看片 → 详细分镜初稿.md
  │     B. qwen3.8-max 看片精修  → 详细分镜.md（只写事实，不写提示词，不写字幕）
  │
  ├─ ⑤ step3 提示词撰写（qwen3.8-max 纯文本，无字幕）
  │     → 生视频提示词.md：每段 参考图清单（段首，作为编号承诺）+ ★Prompt（首行声明无字幕）+ 首帧合成指引
  │
  └─ ⑥ 可选：wan3.0-video（默认，可换）按生成段出片（约 10 分钟/段），ffmpeg 拼接成片
```

对应命令：

```bash
# ① 第一步分析
python3 scripts/analyze_video.py step1 --video 原片.mp4 --output 分镜大纲.md

# ② 角色抽帧（按角色卡"出现镜头"抽候选帧）
python3 scripts/extract_char_frames.py --cards 分镜大纲.md --video 原片.mp4 --output refs/frames
# 用户定稿后写 refs/frames/selected.json：{"角色卡-妻子": "refs/frames/帧_妻子_2.jpg", ...}
# 值也可写 ≤3 帧路径列表（多帧同时作 I2I 参考，服装细节难还原时用）

# ③ 参考图（角色卡走定稿帧 I2I、场景卡文生图；每卡 3 候选；--only 可单独重出某张卡）
python3 scripts/generate_ref_images.py --cards 分镜大纲.md --output refs --frames-index refs/frames/selected.json

# ④ 第二步两阶段
python3 scripts/analyze_video.py step2  --video 原片.mp4 --step1-file 分镜大纲.md --output 详细分镜初稿.md
python3 scripts/analyze_video.py refine --video 原片.mp4 --step1-file 分镜大纲.md --draft-file 详细分镜初稿.md --output 详细分镜.md

# ⑤ 第三步提示词
python3 scripts/analyze_video.py step3 --step2-file 详细分镜.md --step1-file 分镜大纲.md --refs-index refs/index.json --output 生视频提示词.md

# ⑥ 可选出片（一段一次生成，默认 wan3.0-video 可用 --model 换；提示词与参考图顺序照抄第⑤步产物）
python3 scripts/generate_video.py --prompt "<该段★Prompt>" --refs refs/图1.png refs/图2.png \
  --duration <段总时长向上取整，≤30> --resolution 1080P --ratio 9:16 --output shots/seg1.mp4
```

两个硬检查点：参考图定稿前**必须人工确认**（卡名会被后续所有环节严格引用，错误会一路传导；角色参考图必须与定稿帧并排比对长相）；生视频**默认不执行**（慢且耗额度，需明确要求）。

## 产物清单

| 文件 | 内容 |
| --- | --- |
| `分镜大纲.md` | 逐镜头大纲 + 角色卡 + 场景卡（文生图 Prompt） |
| `refs/frames/` | 每角色 3 张候选抽帧 + frames.json + selected.json（定稿帧映射） |
| `refs/` + `index.json` | 每卡 3 张候选参考图与定稿映射（数组第一张为定稿） |
| `详细分镜.md` | 生成段规划表 + 分幕镜头细节块（布景站位/光影/逐秒时间轴/切镜标注，无字幕） |
| `生视频提示词.md` | 每生成段：参考图清单（段首，作为编号承诺）+ ★视频生成 Prompt（首行声明无字幕）+ 首帧合成指引 |
| `shots/`（可选） | 各生成段视频与拼接成片 |

## 设计思路

### 为什么角色参考图要抽帧 I2I，而不是纯文生图

文生图按文字描述"重画"角色，即使描述精确，生成的脸也是模型理解后的近似——对真人素材复刻来说，"近似"就是不可用。图生图把原片真实帧作为参考输入，面部/发型/服装由图像直接锚定，文字只补充"这一张要什么构图、姿态、背景"，产出的人与原片严格一致。抽帧环节让用户先定"用哪一张脸"，再生图环节保证"就用这张脸"。

### 为什么无字幕靠提示词而不是后期

字幕进提示词会让视频模型把台词当画面元素渲染（且渲染质量差、样式不可控）；后期裁剪/修复又会损失画质。本版本从源头控制：时间轴不记录字幕、★Prompt 首行声明"画面全程无字幕、无任何叠加文字"，台词全部走音频对白——成片干净，字幕需求由投放侧自行叠加。

### 其余设计（与底座一致）

三步分工（选角置景/运镜调度/提示词撰写各司一职）、生成段方案（沿用底座逻辑，本版段上限放宽到 ≤30s，剧情连贯镜头尽量打包满）、omni+max 两阶段（omni 唯一能听音频，max 终审视觉与格式，音频事实保护禁止精修改台词）——详见底座版 README。

## 踩坑沉淀（已固化进提示词与脚本）

- **定稿帧质量决定角色还原度**：选近景/正面/人脸完整帧；远景、背影、遮挡、运动模糊、字幕压脸的帧不选。帧单边 <240px 脚本自动放大，但清晰度仍以原片高清版为准。
- **I2I 服装还原三层保险**：①身份锁定前缀内置仲裁——服装细节（款式/纹理/颜色/开合/纽扣）文字与帧冲突时一律以帧为准；②卡 Prompt 仍要显式写明真实服装状态（"敞开不系扣"等）；③细节仍漂移时，selected.json 该角色值改为 ≤3 帧列表做多帧参考再 `--only` 重出（实测开衫纹理/纽扣漂移靠多帧收敛）。
- **wan 系生图不接参考图**：抽帧 I2I 必须用 qwen-image 系（默认 qwen-image-3.0-pro）；`--model` 切 wan 系时脚本会警告并忽略帧输入。
- **I2I 须关 prompt_extend**：否则模型改写提示词导致色值/构图漂移——脚本已内置。
- **多段拼接必须显式 `--ratio`**（与原片一致，竖屏 9:16）：默认 adaptive 可能出方形段，导致 ffmpeg concat 分辨率不匹配。
- **wan3.0-video 账号并发约 2 任务**：批量提交错开数秒即可，超额任务服务端排队不会失败；task_id 与视频 URL 有效期 24h，中断可凭 task_id 恢复下载。
- **所有脚本不覆盖已有文件**：同名自动加 `_1/_2` 后缀，重跑安心，但取用时注意拿最新文件。
- **无字幕/无水印是硬规则**：任何产物混入字幕或水印描述，按源头治理修 system prompt 后重跑，不手动逐处修补。

## 目录结构

```
video-reverse-replicate-chuangliang/
├── SKILL.md                        # Agent 执行手册（完整流程与核对清单）
├── README.md                       # 本文档
├── prompts/
│   ├── step1_system.md             # 第一步：选角与置景
│   ├── step2_system.md             # 第二步A：omni 听音初稿（无字幕）
│   ├── step2_refine_system.md      # 第二步B：max 看片精修（音频事实保护+剔除字幕）
│   ├── step3_system.md             # 第三步：提示词无损翻译（五要素、首行声明无字幕）
│   └── card_maker_system.md        # 备用：卡片文本单独精修
└── scripts/
    ├── analyze_video.py            # step1 / step2 / refine / step3 四合一 CLI
    ├── extract_char_frames.py      # 创量定制：按角色卡出现镜头抽候选帧
    ├── generate_ref_images.py      # 参考图生成：--frames-index 角色卡抽帧 I2I
    ├── generate_video.py           # R2V 视频生成（默认 wan3.0-video，异步+断点恢复）
    └── make_cards.py               # 备用：旧版大纲卡片精修
```

## 默认模型一览

| 环节 | 模型 | 备注 |
| --- | --- | --- |
| step1 选角置景 | qwen3.8-max | 看片直出，`--model` 可覆盖 |
| step2 初稿 | qwen3.5-omni-plus | 唯一音频理解，听音+看片 |
| refine 精修 | qwen3.8-max | 看片校对，音频以初稿为准 |
| step3 提示词 | qwen3.8-max | 纯文本，不吃视频 |
| 参考图 | qwen-image-3.0-pro | 角色卡抽帧 I2I + 场景卡文生图，每卡 3 候选 |
| 生视频 | wan3.0-video | R2V，≤30s/段（创量链路上限），1080P；`--model` 可换 |
