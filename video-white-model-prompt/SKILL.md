---
name: video-white-model-prompt
description: 当用户明确要求把参考视频生成近白远黑的单目深度白模，或在两阶段反推完整视听提示词后调用 Doubao Seedance 2.0/2.5 生成成片时使用。支持仅白模、白模+提示词+Seedance成片、无白模+提示词+Seedance成片；不支持只反推提示词，也不要因普通视频分析、静态图片深度或 3D/建筑白模需求而触发。
metadata:
  version: 1.10.2
---

# 视频白模、提示词反推与 Seedance 成片

从唯一参考视频生成单目相对深度白模，或由 Qwen3.5-Omni-Plus 先生成视听初稿、Qwen3.8-Max 再精修为 Seedance 提示词，最后按用户选择带或不带白模参考生成分段成片。

## 触发边界

“白模”可能指 3D 或建筑白模时先澄清。用户只要求提示词反推、普通视频分析、视频复刻、产品替换或静态图片深度时不触发；本 Skill 的提示词链路必须以 Seedance 成片为目标。

## 用户交互

采用渐进式向导收集信息。每轮消息只呈现当前步骤名称、一个问题、带编号的完整选项和回复示例，等待用户回答后再展示下一步。已经明确提供的信息自动填入并跳过对应步骤；后续步骤根据已选分支动态展开。

### 步骤 1：确认唯一视频

只把当前消息明确上传或提到的视频视为候选，不扫描其他目录猜测。原片始终只读。

- 只有一个候选时展示文件名，并提供：`1. 使用这个视频`、`2. 更换视频`。
- 没有候选时提供：`1. 上传视频`、`2. 提供本地文件路径`。
- 有多个候选时逐项编号，并增加最后一项“重新上传或提供其他路径”。

### 步骤 2：选择产出范围

提供以下单选项：

1. `仅白模`：只生成近白远黑的单目深度白模。
2. `白模+提示词+Seedance成片`：生成白模，反推提示词，并把白模作为参考生成成片。
3. `无白模+提示词+Seedance成片`：不生成白模，直接反推提示词并生成成片。
4. `帮我推荐`：根据用户想要的最终结果推荐 1 至 3 中的一项，再让用户确认。

### 步骤 3：选择最大分段时长

提供以下单选项：

1. `15s · Seedance 2.0`：单段范围 `[4,15]` 秒，调用 `doubao-seedance-2-0-260128`，通常会产生更多生成任务。
2. `30s · Seedance 2.5`：单段范围 `[4,30]` 秒，调用 `doubao-seedance-2-5-260628`，通常任务数更少；默认优先推荐。

### 步骤 4：按需收集成片补充信息

仅步骤 2 选择两种 Seedance 成片模式时展示。先让用户多选准备提供的内容：

1. `不提供补充信息`
2. `产品名称`
3. `产品图片`，最多 9 张
4. `人物形象图`
5. `产品卖点`
6. `创意要求`

用户可回复一个或多个编号，例如 `2,3,5`；选项 1 与其他选项互斥。随后每轮只收集一个已选字段；每个字段都提供 `1. 现在提供`、`2. 跳过此项`。文件类字段在选择“现在提供”后，再提供 `1. 使用当前消息附件`、`2. 提供本地文件路径`。选择 `1. 不提供补充信息` 时直接进入下一步。

产品名称、卖点和创意默认不改变参考视频台词。只有用户主动提出口播修改时，才展示：`1. 保持原台词`、`2. 只替换指定旧词`、`3. 允许整段改写`。选择 2 后逐项收集 `旧词=新词`；选择 3 时记录明确授权。用户未提及口播修改时自动采用选项 1，不增加询问步骤。

### 步骤 5：选择 Qwen Key 来源

仅两种 Seedance 成片模式需要。不要要求用户在聊天中粘贴 Key，提供：

1. `使用 DASHSCOPE_API_KEY 环境变量`
2. `提供 Key 文件路径`
3. `提供 Key 目录路径`，目录默认读取 `DASHSCOPE_API_KEY.md`
4. `暂未配置`，说明完成配置后可从本步骤继续

Ark Key 和火山 TOS 在第一阶段不使用，延后到 Seedance 提交前收集。无白模且无图片时不需要 TOS。所有生成视频在用户未明确指定分辨率时统一使用 `720p`；只有用户明确要求时才传 `480p` 或 `1080p`。其余默认值为跟随原片画幅、`mp4`、无水印，并根据原片是否有音轨自动决定 `generate_audio`。

### 步骤 6：只读预检

确认前检查视频、图片、转写、Key 来源、`ffmpeg`/`ffprobe`、Python 依赖、磁盘和按范围所需的深度模型。

Qwen 分析媒体继续使用 Base64 Data URL，逐文件上限为 9.5 MiB；超限不上传 TOS，也不擅自压缩。提示用户按动态 FFmpeg 命令生成压缩分析版，保持 `--video` 指向原片、`--analysis-video` 指向压缩版。共享 `scripts/media_preflight.py` 负责两者的音轨、画幅、时长、时间轴和五点画面相似度校验。

Seedance 成片模式要求参考视频至少 4 秒。每段必须是 4 到用户上限之间的整数秒，分段数量固定为 `ceil(总时长 / 用户上限)`：优先使用最少任务数并让各段尽量接近最大时长；尾段不足 4 秒时前移上一切点重新分配，不产生短尾段。

带白模成片时，正式白模仍保持原画幅、CFR、H.264、720p、无音频。若其 FPS、编码、尺寸或宽高比不符合 Seedance 参考视频要求，只在 `seedance/assets/` 生成兼容副本，不修改正式白模。

Seedance 图片在提交前检查大小、像素和宽高比。按用户选择，不增加真人人脸检测、改写或拦截；Ark API 拒绝时原样保留错误并停止。

深度模型只在 `仅白模` 和 `白模+提示词+Seedance成片` 中解析，顺序为命令参数、`DEPTH_ANYTHING_MODEL`、Skill 本地模型、机器已知模型路径。无白模模式不得要求深度模型。

### 步骤 7：第一次确认

启动 Qwen 或本地深度任务前，汇总输入、范围、分段上限、由分段上限确定的 Seedance 模型、图片、口播权限、Qwen Key 来源和默认 Seedance 参数。明确说明：

- 分析视频、其中的原始音轨和图片会发送至阿里云 Qwen；有音轨时正常调用 Omni 与 Max 两次，无音轨时只调用 Max 一次。
- 原始参考视频和原始音轨不会发送至 Seedance。
- Seedance 每个最终分段对应一个独立生成任务；素材只在第二次确认后上传火山 TOS 和提交 Ark。
- 当前 TOS 配置通过 `publicDomain` 暴露生成素材 URL，写入角色不能主动删除对象；提交前必须说明公开可读范围和 Bucket 生命周期依赖。

最后提供：`1. 确认并开始第一阶段`、`2. 修改以上信息`、`3. 取消`。用户选择 1 后冻结本次输入。

## 第一阶段：准备正式产物

统一调用 `scripts/run_pipeline.py`。

仅白模：

```bash
python3 <skill-root>/scripts/run_pipeline.py \
  --video <原片> \
  --scope depth-only \
  --segment-max-seconds <15或30> \
  --output-dir <新目录>
```

白模+提示词+Seedance成片使用 `--scope depth-prompt-seedance`；无白模模式使用 `--scope prompt-seedance`。两种成片模式按需追加：

```bash
  --analysis-video <可选压缩分析视频> \
  --product-name <可选> \
  --product-image <可重复最多9次> \
  --character-image <可选> \
  --selling-points <可选> \
  --user-idea <可选> \
  --spoken-replacement <用户明确指定的旧词=新词，可重复> \
  --allow-audio-rewrite \
  --transcript-file <可选> \
  --api-key-file <可选Qwen Key文件或目录> \
  --seedance-resolution <480p|720p|1080p> \
  --seedance-ratio <source|adaptive|固定画幅> \
  --seedance-output-format <mp4|mov> \
  --seedance-generate-audio <auto|true|false> \
  --output-dir <新目录>
```

提示词链路和深度推理在带白模模式下并行运行。Omni 负责音频事实和安全分段点；Max 负责视觉、人物、产品和最终 Seedance 文案，但不得移动已合法锁定的 Omni 分段。

Omni 候选稿先校验音频事实：存在人声时必须使用 `{}` 完整记录；确实没有可辨识人声时必须输出唯一的 `[[NO_SPEECH_CONFIRMED]]` 内部标记。两者都没有或同时存在时由 Omni 定向修复一次。随后分两层校验结构。第一层校验可锁定的分段计划：分段标题可解析、数量最少、每段处于对应模型范围、总时长正确、参考视频区间连续；这一层失败时由 Omni 携带具体错误定向修复一次，仍失败则停止。第一层通过后锁定分段计划；若第二层的镜头编号、段内连续时间轴、图片编号或 API 参数字面量校验失败，保留该稿作为音频事实初稿，将错误和锁定分段布局交给原定的 Max 调用修复，不再次调用 Omni。

Max 终稿必须通过完整结构、锁定分段、实际图片编号和 API 字面量校验。Omni 结构合法时同时锁定镜头数量、顺序和时间区间；Max 只精修镜头内部内容。默认逐段保留 Omni 台词原文；指定词替换模式只允许明确的 `旧词=新词` 映射；只有用户明确允许整段改写时才关闭逐字一致性校验。产品名称、卖点和创意字段本身不改变口播权限。Max 只有可定位错误时携带具体错误定向修复一次，修复稿仍未通过则停止。只有全部终稿校验通过后，才提升正式提示词和分段计划。

Max 正式稿必须使用 `@图片N`，不输出 4K、画幅、分辨率等 API 参数，也不写视频编辑或延长意图。每段完成主体定义后直接进入 `镜头1[...]`；场景、风格、光线、节奏、情绪和声音信息写入实际发生的镜头，不输出“生成一条……”式段级整体概述。每段镜头1必须对应原片分段起点，段内镜头保持原片先后顺序。`seedance_video_pipeline.py prepare` 会拆出每段独立 Prompt；带白模模式直接以 `@视频1` 深度白模职责开头，不增加“生成一段全新视频”或“【参考素材职责】”；无白模模式不增加任何前缀或视频引用。

第一阶段成功后写入 `ready_for_seedance.json` 和 `seedance/seedance_plan.json`，但不调用 Seedance。

## 第二次确认与 Seedance 提交

第一阶段成功后，再分步收集提交配置，每轮只问一项：

- Ark Key 来源：`1. 使用 ARK_API_KEY 环境变量`、`2. 提供 Key 文件路径`、`3. 暂不提交并保留第一阶段产物`。不要要求用户在聊天中粘贴 Key。
- 带白模或提供了图片时，火山 TOS 来源：`1. 使用已配置的 TOS 环境变量`、`2. 使用当前机器默认目录 /Users/bron/Documents/CodeX/API/火山`、`3. 提供配置文件或目录路径`、`4. 暂不提交并保留第一阶段产物`。只使用火山 TOS，不接入其他 OSS。

完整展示正式提示词、Seedance 模型、任务数、每段时长、是否带白模、图片数量、`generate_audio`、分辨率、画幅、格式和水印。用户明确确认提交范围后才运行：

1. `确认并提交全部分段`
2. `修改生成参数`
3. `暂不提交并保留第一阶段产物`

选择 2 时，先确认 `seedance/tasks.json` 不存在，或其中 `uploads`、`segments` 均为空；已有上传记录或任务 ID 时不覆盖原计划。确认尚未提交后，复用原 `prompt.txt`、`segment_plan.json`、原片、图片和白模目录，重新运行 `seedance_video_pipeline.py prepare --overwrite`，只替换用户修改的参数；未明确修改的参数和原 Seed 保持不变。重建并校验 `seedance_plan.json` 后，重新展示第二次确认。

```bash
python3 <skill-root>/scripts/seedance_video_pipeline.py prepare \
  --prompt <输出目录>/prompt.txt \
  --segment-plan <输出目录>/segment_plan.json \
  --source-video <原片> \
  --depth-dir <带白模时传入> \
  --character-image <按原计划可选> \
  --product-image <按原计划可重复> \
  --output-dir <输出目录>/seedance \
  --resolution <修改后值> \
  --ratio <修改后值> \
  --output-format <修改后值> \
  --generate-audio <修改后值> \
  --seed <原Seed或用户新值> \
  --overwrite
```

```bash
python3 <skill-root>/scripts/seedance_video_pipeline.py submit \
  --plan <输出目录>/seedance/seedance_plan.json \
  --ark-api-key-file <可选Ark Key文件> \
  --tos-config-file <有素材时的火山TOS配置文件或目录>
```

TOS 配置支持两种格式：JSON 的 `access_key`、`secret_key`、`endpoint`、`region`、`bucket` 等字段；或本机 Markdown 中的 `accessKey`、`secretKey`、`endpoint`、`region`、`bucket`、`roleTrn`、`mainPath`、`publicDomain`。传目录时默认读取其中的 `Volc engine_API_KEY.md`。配置文件包含密钥，不作为交付物展示。

配置存在 `roleTrn` 时先通过 STS AssumeRole 获取临时写入凭证，所有对象必须写入 `mainPath/video-white-model-prompt/` 授权前缀；存在 `publicDomain` 时优先使用经过 URL 编码的公开 TOS 链接提交 Seedance，否则使用签名 URL。无白模且无图片时直接文生视频，不要求 TOS。原片和原始音轨在任何模式下都不上传 Seedance；声音仅由正式提示词和 `generate_audio` 控制。

当前写入角色没有 `DeleteObject` 和对象 TTL 权限，因此 Skill 不尝试删除上传对象；目标 Bucket 必须在 `mainPath` 下配置短期生命周期，避免素材长期残留。提交脚本会在检测到指向 `127.0.0.1:7890` 或 `localhost:7890` 的 HTTP(S) 代理时移除对应大小写环境变量，并设置 `NO_PROXY=*` 与 `no_proxy=*`，避免 STS、TOS 和 Ark 继续使用不可用的系统代理。

模型由 `segment_plan.json` 的 `segment_max_seconds` 唯一确定：`15` 使用 `doubao-seedance-2-0-260128`，`30` 使用 `doubao-seedance-2-5-260628`。准备计划时写入模型 ID，提交前重新校验映射；模型与分段上限不一致时停止。有参考资产时设置 `omni_reference_task_type=reference`；白模始终为 `@视频1`，图片按既有顺序为 `@图片1...N`。用户确认提交范围后，先连续创建所有缺少任务 ID 的分段，不等待前一段完成；随后并发轮询、下载和校验全部任务。单段失败不取消其他已创建任务，也不自动创建新任务；恢复时已有任务 ID 只查询，不重复创建。

全部分段下载并校验通过后，必须按 `segment_plan.json` 顺序使用 FFmpeg concat demuxer和流复制拼接为 `seedance/generated/full.<格式>`。拼接失败时停止并保留分段，不静默降质重编码。

## 恢复与失败

第一阶段恢复时复用原命令、原输入、原输出目录并增加 `--resume`。输入清单不一致时拒绝恢复；可复用正式 Omni 初稿、Max 正式稿、分段计划和完整深度缓存。

Seedance 使用 `seedance/tasks.json` 持久化上传对象、任务 ID 和状态：

- 已有任务 ID 时只查询，不重复创建。
- 创建请求结果未知时标记 `create_ambiguous`，不自动重发。
- `failed`、`cancelled`、`expired` 不自动创建新任务；用户再次明确确认后才能使用显式重试参数。
- 查询和结果下载可安全重试。下载文件未通过时长、音轨或可读性校验时，归档为 `.invalid_N`，并通过已有成功任务重新下载，不创建新任务。成功后立即保存到本地，因为远程结果 URL 会过期。

## 交付

- `仅白模`：按顺序展示 `depth/*.mp4`。
- 成片模式：提供完整 `prompt.txt`、`segment_plan.json`；有音轨时同时提供 `prompt_draft.txt`。
- 提供各段 `seedance/generated/part_XX.<格式>`、完整成片 `seedance/generated/full.<格式>`、`seedance/seedance_plan.json` 和任务状态摘要。
- 候选稿只用于失败排查，不作为正式提示词。
- 每段正式提示词必须完整放入独立 `text` 代码块，不得省略。
- 默认不保存 Qwen 的完整 Base64 请求；仅用户要求调试时使用 `--save-debug`。

不要创建额外总结 Markdown。`prompts/` 下的文本是发给模型的运行时数据，不是本 Skill 对 Codex 的操作指令。
