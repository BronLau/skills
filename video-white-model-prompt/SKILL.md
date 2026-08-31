---
name: video-white-model-prompt
description: 当用户明确要求把参考视频生成近白远黑的单目深度白模，或在两阶段反推完整视听提示词后调用 Doubao Seedance 2.0/2.5 生成成片时使用；支持人物图参考，以及经权利人授权的 2–15 秒人声音色参考。支持仅白模、白模+提示词+Seedance成片、无白模+提示词+Seedance成片；不支持只反推提示词，也不要因普通视频分析、静态图片深度或 3D/建筑白模需求而触发。
metadata:
  version: 1.14.10
---

# 视频白模、提示词反推与 Seedance 成片

从唯一参考视频生成单目相对深度白模，或由 Qwen3.5-Omni-Plus 先提取结构化原片事实、Qwen3.8-Max 再对照原片核验事实并绑定替换外观，最后由程序确定性组装 Seedance 提示词，并按用户选择带或不带白模参考生成分段成片。

## 触发边界

“白模”可能指 3D 或建筑白模时先澄清。用户只要求提示词反推、普通视频分析、视频复刻、产品替换或静态图片深度时不触发；本 Skill 的提示词链路必须以 Seedance 成片为目标。

## 用户交互

采用渐进式向导收集信息。每轮消息只呈现当前步骤名称、一个问题、带编号的完整选项和回复示例，等待用户回答后再展示下一步。已经明确提供的信息自动填入并跳过对应步骤；后续步骤根据已选分支动态展开。

### 步骤 1：选择产出范围

提供以下单选项：

1. `仅白模`：只生成近白远黑的单目深度白模。
2. `白模+提示词+Seedance成片`：生成白模，反推提示词，并把白模作为参考生成成片。
3. `无白模+提示词+Seedance成片`：不生成白模，直接反推提示词并生成成片。
4. `帮我推荐`：根据用户想要的最终结果推荐 1 至 3 中的一项，再让用户确认。

### 步骤 2：选择最大分段时长

提供以下单选项：

1. `15s · Seedance 2.0`：单段范围 `[4,15]` 秒，调用 `doubao-seedance-2-0-260128`，图片类型素材合计最多 9 张，通常会产生更多生成任务。
2. `30s · Seedance 2.5`：单段范围 `[4,30]` 秒，调用 `doubao-seedance-2-5-260628`，图片类型素材合计最多 30 张，通常任务数更少；默认优先推荐。

### 步骤 3：按需收集成片补充信息

仅步骤 1 选择两种 Seedance 成片模式时展示。先让用户多选准备提供的内容：

1. `不提供补充信息`
2. `产品名称`
3. `产品图片`，数量占用所选模型的图片类型素材总额度
4. `人物形象图`
5. `口播音色参考`，使用 2 至 15 秒 MP3/WAV，或从唯一参考视频抽取连续口播
6. `产品卖点`
7. `创意要求`

用户可回复一个或多个编号，例如 `2,3,6`；选项 1 与其他选项互斥。随后每轮只收集一个已选字段；每个字段都提供 `1. 现在提供`、`2. 跳过此项`。文件类字段在选择“现在提供”后，再提供 `1. 使用当前消息附件`、`2. 提供本地文件路径`。选择 `1. 不提供补充信息` 时直接进入下一步。

产品名称、卖点和创意默认不改变参考视频台词。只有用户主动提出口播修改时，才展示：`1. 保持原台词`、`2. 只替换指定旧词`、`3. 允许整段改写`。选择 2 后逐项收集 `旧词=新词`；选择 3 时记录明确授权。用户未提及口播修改时自动采用选项 1，不增加询问步骤。

选择口播音色参考时，必须由用户明确确认已获得声音权利人的授权，同意抽取、上传并用于音色参考。参考音频只约束全片人声的音色、发声质感、语速与韵律，台词仍以正式 Prompt 为准；不得把参考音频中的原台词、背景音乐或环境声当作成片内容。用户要求从参考视频抽取时，优先选择连续清晰口播并输出恰好 15 秒的单声道 WAV；原视频保持只读。声音授权不继承人物图片的虚拟人像权利确认，两者分别记录。

选择人物形象图时，读取 [私域人像资产链路](references/virtual_portrait_assets.md)。人物图上传后默认记录为 `character_image_type=virtual` 并创建新的私域 Asset，不再单独询问人物类型、Asset 创建或复用方式、素材权利；记录默认值后直接继续收集其他信息。该默认值是工作流路由，不是自动人脸判断。用户主动说明是真人肖像时切换为 `real`，必须提供已经完成真人授权的 Asset ID，不把真人肖像上传到私域虚拟人像库；用户主动提供已有 Asset ID 时改为复用。默认虚拟人像的素材权利声明合并到步骤 6 的第一次总确认中。

### 步骤 4：选择 Qwen Key 来源

仅两种 Seedance 成片模式需要。不要要求用户在聊天中粘贴 Key，提供：

1. `使用 DASHSCOPE_API_KEY 环境变量`
2. `提供 Key 文件路径`
3. `暂未配置`，说明完成配置后可从本步骤继续

Ark Key 和火山 TOS 在第一阶段不使用，延后到 Seedance 提交前收集。无白模且无图片时不需要 TOS。所有生成视频在用户未明确指定分辨率时统一使用 `720p`；只有用户明确要求时才传 `480p` 或 `1080p`。其余默认值为跟随原片画幅、`mp4`、无水印，并根据原片是否有音轨自动决定 `generate_audio`。

### 步骤 5：预检与分析媒体准备

确认前以只读方式检查视频、图片、转写、Key 来源、`ffmpeg`/`ffprobe`、Python 依赖、磁盘和按范围所需的深度模型。

Qwen 分析媒体继续使用 Base64 Data URL，逐文件上限为 9.5 MiB；视频超限时不上传 TOS，也不增加用户确认步骤，直接按动态 FFmpeg 参数生成压缩分析副本。原片始终只读；压缩副本写入本次运行的独立暂存目录，保持 `--video` 指向原片、`--analysis-video` 指向压缩版。共享 `scripts/media_preflight.py` 负责两者的音轨、画幅、时长、时间轴和五点画面相似度校验；压缩或一致性校验失败时停止，不回退为上传超限原片。图片超限时不得擅自改变用户提供的外观素材，仍应提示用户提供合规图片。

Seedance 成片模式要求参考视频至少 4 秒。每段必须是 4 到用户上限之间的整数秒，分段数量固定为 `ceil(总时长 / 用户上限)`：优先使用最少任务数并让各段尽量接近最大时长；尾段不足 4 秒时前移上一切点重新分配，不产生短尾段。

带白模成片时，正式白模仍保持原画幅、CFR、H.264、720p、无音频。若其 FPS、编码、尺寸或宽高比不符合 Seedance 参考视频要求，只在 `seedance/assets/` 生成兼容副本，不修改正式白模。用户明确要求用 OpenPose/DWPose 关键点视频替换白模参考时，保持原分段数量与顺序，校验参考视频与对应分段的画幅、帧率、帧数、时长、编码和无音频约束，并在准备计划时设置 `--motion-reference-type openpose`；OpenPose 只负责人体、面部、手部关键点的位置、姿态与时序，不得被描述成深度图，也不得把黑色背景、彩色骨架线、关键点或连线生成到成片。

Seedance 图片在提交前检查总数、大小、像素和宽高比。图片类型素材总数包含人物图和产品图：15 秒分段对应 Seedance 2.0，合计不得超过 9 张；30 秒分段对应 Seedance 2.5，合计不得超过 30 张。人物形象图还必须具有明确的人像类型和 Asset 路由；产品图不进入人像素材库。使用人物 Asset 前确认账号已开通私域素材库能力，Ark 配置中的 AK/SK 具备所需 IAM 权限，且 Asset、Asset Group 与 Ark API Key 都属于固定的 `ProjectName=default`。创建新虚拟 Asset 时，先按人物图 SHA-256 稳定名称调用 `ListAssets` 查找可复用素材；该查询也作为上传前的访问预检。Ark API 拒绝时原样保留错误并停止。

音色参考在提交前检查格式、大小、时长和纯音频流：仅 MP3/WAV、小于 15 MB、时长 `[2,15]` 秒。正式分段 Prompt 使用 `@音频1`，明确只参考统一人声音色，不复用参考音频中的台词或背景声。音色参考作为 `audio_url`、`role=reference_audio` 提交；它不是人物图片 Asset，也不混入虚拟人像素材组。

深度模型只在 `仅白模` 和 `白模+提示词+Seedance成片` 中解析，顺序为命令参数、`DEPTH_ANYTHING_MODEL`、Skill 本地模型、机器已知模型路径。无白模模式不得要求深度模型。

### 步骤 6：第一次确认

启动 Qwen 或本地深度任务前，汇总输入、范围、分段上限、由分段上限确定的 Seedance 模型、图片、口播权限、Qwen Key 来源和默认 Seedance 参数。存在人物图时还要明确展示人物类型与 Asset 创建或复用方式。人物图按默认虚拟人像创建新 Asset 时，在本次总确认中声明：用户确认该图片为虚拟形象、合法拥有完整权利、不与自然人肖像雷同且不侵害第三方权益。明确说明：

- 分析视频及其中的音轨会发送至 Omni，用于提取结构化原片事实；分析视频、Omni 事实和用户提供的图片会发送至 Max，用于对照原片纠正事实并绑定静态外观。正常流程调用 Omni 与 Max 各一次；任一阶段 JSON 或事实契约未通过时，该阶段最多定向修复一次。
- 原始参考视频和完整原始音轨不会发送至 Seedance；选择音色参考时，仅把用户确认的 2 至 15 秒音频片段发送至 Seedance。
- Seedance 每个最终分段对应一个独立生成任务；素材只在第二次确认后上传火山 TOS 和提交 Ark。
- 虚拟人物图只在第二次确认后上传 TOS、创建或查询私域 Asset，并在状态为 `Active` 后用于 Seedance；真人肖像只查询用户提供的已授权 Asset ID。
- 当前 TOS 配置通过 `publicDomain` 暴露生成素材 URL，写入角色不能主动删除对象；提交前必须说明公开可读范围和 Bucket 生命周期依赖。

最后提供：`1. 确认并开始第一阶段`、`2. 修改以上信息`、`3. 取消`。存在默认虚拟人像时，选项 1 改为 `确认以上信息与人物素材权利声明，并开始第一阶段`；用户选择 1 后才记录权利确认、追加 `--confirm-virtual-portrait-rights` 并冻结本次输入。

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
  --product-image <可重复；与人物图合计最多9或30张> \
  --character-image <可选> \
  --character-image-type <virtual|real> \
  --character-asset-id <已有Asset时传入> \
  --confirm-virtual-portrait-rights \
  --selling-points <可选> \
  --user-idea <可选> \
  --spoken-replacement <用户明确指定的旧词=新词，可重复> \
  --allow-audio-rewrite \
  --transcript-file <可选> \
  --reference-audio <可选2至15秒MP3或WAV> \
  --confirm-voice-rights \
  --api-key-file <可选Qwen Key文件> \
  --seedance-resolution <480p|720p|1080p> \
  --seedance-ratio <source|adaptive|固定画幅> \
  --seedance-output-format <mp4|mov> \
  --seedance-generate-audio <auto|true|false> \
  --seedance-suppress-text-overlays \
  --seedance-strip-dialogue-for-visual-only \
  --output-dir <新目录>
```

提示词链路和深度推理在带白模模式下并行运行。Omni 只依据分析视频输出主体、分段、连续镜头时间轴、镜头内连续动作阶段 `beats`、景别、机位、运镜、构图、可见身体范围、人物动作、操作人员与产品动作、进出场、场景光线和音频事实。Max 同时接收原片、Omni 事实和替换图片，对照原片核验每个视觉字段与 beat，并输出事实修正、核验后事实、静态外观绑定及按权限开放的音频覆盖。

Omni 结构化事实必须使用最少分段数，以整数秒连续覆盖完整目标时长；段内镜头编号和时间轴连续。每个镜头的 `beats` 使用段内相对整数时间，连续覆盖该镜头；动作、表情、操作人员或产品动作发生阶段变化时拆分 beat，但不得虚构切镜。存在人声时逐字写入 `{}`，没有可辨识人声时设置 `no_speech_confirmed=true`。人物图、产品图、名称、卖点和创意不发送给 Omni，避免替换素材污染原片动作理解。Omni JSON 不通过时携带具体错误定向修复一次，仍失败则停止。

Max 不直接输出最终 Prompt。它必须保持 Omni 的段级数量、顺序、边界、时长和音频总内容；可以依据原片纠正主体清单、段内镜头与 beat 的数量、顺序、连续整数时间区间及视觉字段，但每类变化都必须在 `fact_review.corrections` 中逐项写明字段路径、完整 Omni 原值、完整修正值、证据时间和证据说明。correction 路径与校验粒度保持一致：主体清单变化使用 `subjects`；镜头数量、顺序或起止时间变化使用 `segments[i].shot_plan`，并以完整镜头计划数组作为原值和修正值，不得拆成单个 `start_seconds` 或 `end_seconds` 路径；镜头结构变化同时带来视觉内容变化时另用 `segments[i].shot_visuals`，其数组项只包含允许的视觉字段和 `beats`，不重复包含 `index`、起止时间或 `audio`；镜头结构不变时才使用逐镜头视觉字段路径，beat 时间计划变化使用 `beat_plan`，beat 动作数组变化独立使用 `beat_actions`，两者同时变化时分别记录。聚合镜头修正可以从程序为该段汇总的段、镜头和 beat 起点、中点、终点中选取证据；每项证据时间至少包含两个不同的有限数，并且必须来自对应 correction 路径的 `allowed_evidence_times`。任何未解释的事实变化、越权字段、NaN/Infinity 或无对应时间证据的修改都会被机器拒绝。

人物图和产品图只进入结构化 `appearance_bindings`，每项只能包含主体标签和图片编号数组，不允许 Max 输出自由文本主体定义。人物图只能绑定 `kind=character`，产品图只能绑定 `kind=product`，每张图片只能绑定一个主体；程序为每张图片生成逐项素材绑定，明确对应主体、只参考的静态外观维度以及不参考的姿态、动作、景别、机位或拼版布局。因此替换素材不能通过定义文本夹带动作、姿态变化、景别、运镜、身体可见范围或进出场。用户在事实锁之后明确要求多个原片人物统一为同一人物形象时，不让 Max 重写动作事实，而是使用受限静态覆盖把这些人物标签合并为一个中性目标标签，再把人物图绑定到该目标标签；原标签中的发型、服装等外观词不得继续进入正式 Prompt。

默认音频直接采用 Omni 事实；指定词替换由程序确定性执行；只有用户明确允许整段改写时，Max 才能通过 `audio_overrides` 修改对应镜头音频。每个音频覆盖项必须且只能包含整数 `segment_index`、整数 `shot_index` 和完整字符串 `audio`，并指向已存在的镜头。改写后的声音描述必须自包含且可直接执行，不得引用不会提交给 Seedance 的原片、原视频、原始音轨、原曲或参考视频。最终 Prompt 由程序使用 Max 核验事实、静态外观绑定和授权音频覆盖确定性组装，Max 无法在组装阶段新增镜头动作。

已锁定计划因音频策略失败，而用户只授权修改音频时，保留原失败计划和任务状态；Max 新结果只提供通过契约校验的 `audio_overrides`。使用 `scripts/merge_verified_audio_override.py` 把这些音频覆盖确定性合并到上一版已锁定的视觉事实与外观绑定中，拒绝新 Max 结果带来的任何视觉字段、主体、分段或时间轴漂移，并为新计划生成独立事实锁。

程序确定性组装的正式 Prompt 必须使用实际存在的 `@图片N`，不保留泛化占位符；不输出 4K、画幅、分辨率等 API 参数，也不写视频编辑或延长意图。每段先输出逐项素材与主体绑定；人物图若包含同一人物的多视图或拼版，必须声明这些视图共同定义同一主体且不生成多个副本。随后输出一条只汇总锁定事实的 `生成目标`，再进入 `镜头1[...]`。每个镜头先写景别、机位、构图、场景等镜头级信息，再按连续整数时间写 `动作阶段N[...]`，最后单独写声音；末尾使用明确的全片一致性和字幕、水印、品牌文字约束。每段镜头1对应原片分段起点，段内镜头和 beat 沿用 Max 根据原片核验后的顺序与时间。`seedance_video_pipeline.py prepare` 会拆出每段独立 Prompt；带白模模式以 `@视频1` 的唯一镜头结构与运动职责开头，并说明逐项绑定的图片主体对应白模中的同名主体。存在人物图时，白模只锁定动作骨架、姿态变化、遮挡、空间、机位、运镜和时序，不负责人物身份、脸型、五官、发型轮廓、头身比、体型细节或服装外观；这些信息冲突时以绑定人物图为准。OpenPose 模式则以 `@视频1` 的人体、面部、手部关键点位置、姿态、构图与时序职责开头，明确不采用黑底、骨架线、关键点或连线，且人物身份与全部静态外观只由绑定人物图或静态定义决定。无白模模式不增加任何视频引用。

第一阶段成功后写入 `omni_facts.json`、`max_verification.json`、`fact_lock.json`、`ready_for_seedance.json` 和 `seedance/seedance_plan.json`，但不调用 Seedance。`fact_lock.json` 绑定分析视频、Omni 事实、Max 核验结果、正式 Prompt、分段计划、运行时 Prompt、模型与 FPS；任一产物变化时拒绝准备或提交 Seedance。

## 第二次确认与 Seedance 提交

第一阶段成功后，再分步收集提交配置，每轮只问一项：

- Ark 配置来源：`1. 使用 ARK_API_KEY 环境变量`、`2. 提供 Key 文件路径`、`3. 暂不提交并保留第一阶段产物`。不要要求用户在聊天中粘贴 Key。人物 Asset 流程还要求同一 Ark 配置来源提供素材库 AK/SK；文件模式从同一文件读取 `accessKey`、`secretKey`，环境变量模式读取 `ARK_ACCESS_KEY`、`ARK_SECRET_KEY`。Ark API Key 只用于 Seedance，Ark AK/SK 只用于人物素材库。
- 带白模、产品图、音色参考或需要创建新虚拟 Asset 时，火山 TOS 来源：`1. 使用已配置的 TOS 环境变量`、`2. 提供配置文件路径`、`3. 暂不提交并保留第一阶段产物`。只使用火山 TOS，不接入其他 OSS。TOS 配置仅用于 STS 与 TOS 上传，不得为人物素材库提供或回退 AK/SK；仅查询已有人物 Asset 且没有其他待上传素材时不要求 TOS 配置。
- 使用人物 Asset 时不再询问 `ProjectName`，固定使用 `default`。Asset Group、Asset 和 Ark API Key 必须都属于 `default` 项目。

完整展示正式提示词、Max 事实修正摘要、Seedance 模型、任务数、每段时长、是否带白模、图片数量、音色参考及授权状态、人物类型、人物 Asset 创建或复用方式、固定的 `ProjectName=default`、`generate_audio`、分辨率、画幅、格式和水印。用户明确确认提交范围后才运行：

1. `确认并提交全部分段`
2. `修改生成参数`
3. `暂不提交并保留第一阶段产物`

选择 2 时，先确认 `seedance/tasks.json` 不存在，或其中 `uploads`、`segments` 均为空；已有上传记录或任务 ID 时不覆盖原计划。确认尚未提交后，复用原 `prompt.txt`、`segment_plan.json`、原片、图片和白模目录，重新运行 `seedance_video_pipeline.py prepare --overwrite`，只替换用户修改的参数；未明确修改的参数和原 Seed 保持不变。重建并校验 `seedance_plan.json` 后，重新展示第二次确认。

用户在尚未上传或创建任务时明确要求修改人物静态服饰、场景或构图的，不能直接手改已锁定 Prompt。把用户确认后的覆盖项写入独立 JSON，并使用 `scripts/apply_static_visual_overrides.py` 重建 Prompt 和事实锁；覆盖范围只允许主体静态定义、受限 `subject_aliases` 以及逐镜头 `composition`、`scene_light`，不得改变动作、景别、机位、运镜、进出场、时间轴或音频。`subject_aliases` 只用于把用户指定的多个人物标签合并成一个中性人物标签；合并目标必须提供显式静态定义，程序同步替换视觉事实中的标签并合并图片绑定，不修改音频。已有上传记录或任务 ID 时不得覆盖原计划，必须在独立输出目录创建新计划。随后按原参数重新运行 `seedance_video_pipeline.py prepare --overwrite`，校验新计划并再次展示第二次确认。

已完成成片出现字幕、台词文字或乱码，而用户要求无字幕版本时，不重跑 Qwen、不覆盖原计划。复用原 Prompt、事实锁、分段计划、白模、图片、音色参考和 Seed，在独立输出目录重新运行 `seedance_video_pipeline.py prepare`，追加 `--suppress-text-overlays`。该参数把“全片纯净无字、口播只以声音呈现”的硬约束放在每段运行时 Prompt 首部，并写入 Seedance 计划参数；重新展示第二次确认后才创建新任务。

若使用 `--suppress-text-overlays` 重新生成后，逐段抽帧仍发现烧录字幕，不继续重复创建 Seedance 任务。使用 `scripts/remove_burned_subtitles.py` 在本地逐帧识别下半画面中带深色描边的白色字幕并局部修复，重新编码 H.264 视频，同时直接复用原 AAC 音轨；输出到新文件，不覆盖 Seedance 原始成片。必须抽取覆盖全片的采样帧做视觉 QA，确认字幕确实移除且人物与场景没有明显修复破坏后再交付。

若逐帧擦除样例在人物或产品上留下明显修复伪影，停止批量擦除。改用独立的无声视觉计划：准备计划时不传音色参考，设置 `--generate-audio false`、`--suppress-text-overlays` 和 `--strip-dialogue-for-visual-only`。程序从运行时 Prompt 中确定性移除大括号台词与独立声音行，只保留人物说话动作，并在首部声明音轨将在本地后期封装。无声视觉成片通过抽帧无字幕 QA 后，使用 FFmpeg 直接复用上一版统一音色 AAC 音轨，不重编码音频；最终文件还需重新校验时长和音轨。

```bash
python3 <skill-root>/scripts/seedance_video_pipeline.py prepare \
  --prompt <输出目录>/prompt.txt \
  --segment-plan <输出目录>/segment_plan.json \
  --fact-lock <输出目录>/fact_lock.json \
  --source-video <原片> \
  --depth-dir <带白模时传入> \
  --motion-reference-type <depth|openpose> \
  --character-image <按原计划可选> \
  --character-image-type <virtual|real> \
  --character-asset-id <按原计划可选> \
  --confirm-virtual-portrait-rights \
  --product-image <按原计划可重复> \
  --reference-audio <按原计划可选> \
  --confirm-voice-rights \
  --output-dir <输出目录>/seedance \
  --resolution <修改后值> \
  --ratio <修改后值> \
  --output-format <修改后值> \
  --generate-audio <修改后值> \
  --suppress-text-overlays \
  --strip-dialogue-for-visual-only \
  --seed <原Seed或用户新值> \
  --overwrite
```

```bash
python3 <skill-root>/scripts/seedance_video_pipeline.py submit \
  --plan <输出目录>/seedance/seedance_plan.json \
  --ark-api-key-file <可选Ark Key文件> \
  --tos-config-file <有素材时的火山TOS配置文件> \
  --asset-project-name default
```

只有用户针对人物 Asset 再次明确确认后，才追加 `--retry-failed-character-asset` 或 `--allow-recreate-ambiguous-character-asset`。这两个参数不由视频任务的 `--retry-failed` 或 `--allow-recreate-ambiguous` 代替。

Ark 配置文件同时承载 Seedance 的 `ARK_API_KEY`（兼容 `Volcengine_API_KEY` 标签）以及人物素材库的 `accessKey`、`secretKey`；素材库区域固定为 `cn-beijing`。TOS 配置支持 JSON 的 `access_key`、`secret_key`、`endpoint`、`region`、`bucket` 等字段，或 Markdown 中的 `accessKey`、`secretKey`、`endpoint`、`region`、`bucket`、`roleTrn`、`mainPath`、`publicDomain`。字段可使用普通 Markdown、加粗标签、ASCII 冒号或全角冒号。TOS 的 `accessKey`、`secretKey` 专用于 STS AssumeRole 与 TOS 上传，不得传给 Ark 人物素材 API；Ark 与 TOS 凭证之间不得互相回退。配置文件包含密钥，不作为交付物展示。

配置存在 `roleTrn` 时先通过 STS AssumeRole 获取临时写入凭证，所有对象必须写入 `mainPath/video-white-model-prompt/` 授权前缀；存在 `publicDomain` 时优先使用经过 URL 编码的公开 TOS 链接提交 Seedance，否则使用签名 URL。无白模、无图片且无音色参考时直接文生视频，不要求 TOS。原片和完整原始音轨不上传 Seedance；选择音色参考时仅上传用户已授权的音频片段，声音由正式提示词、`@音频1` 和 `generate_audio` 共同控制。

虚拟人物图没有已有 Asset ID 时，提交阶段先用图片 SHA-256 派生的稳定名称调用 `ListAssets`。命中 `Active` 或 `Processing` 素材时校验远端图片与本地人物图一致并复用，不上传原图；没有命中时才上传该图获取可访问 URL，再依次调用 `CreateAssetGroup`、`CreateAsset` 和 `GetAsset`。只有 `Status=Active` 才把 `asset://<Asset ID>` 作为人物图片 URL 写入 Seedance 请求。`GetAsset` 的临时查询错误有限重试；`Processing` 持续查询，`Failed` 或超时停止并保留状态。提示词仍使用 `@图片N`，不写 Asset ID。产品图继续使用普通 TOS URL。

当前写入角色没有 `DeleteObject` 和对象 TTL 权限，因此 Skill 不尝试删除上传对象；目标 Bucket 必须在 `mainPath` 下配置短期生命周期，避免素材长期残留。提交脚本会在检测到指向 `127.0.0.1:7890` 或 `localhost:7890` 的 HTTP(S) 代理时移除对应大小写环境变量，并设置 `NO_PROXY=*` 与 `no_proxy=*`，避免 STS、TOS 和 Ark 继续使用不可用的系统代理。

模型由 `segment_plan.json` 的 `segment_max_seconds` 唯一确定：`15` 使用 `doubao-seedance-2-0-260128`，`30` 使用 `doubao-seedance-2-5-260628`。准备计划时写入模型 ID，提交前重新校验映射；模型与分段上限不一致时停止。有参考资产时设置 `omni_reference_task_type=reference`；白模始终为 `@视频1`，图片按既有顺序为 `@图片1...N`。用户确认提交范围后，先连续创建所有缺少任务 ID 的分段，不等待前一段完成；随后并发轮询、下载和校验全部任务。单段失败不取消其他已创建任务，也不自动创建新任务；恢复时已有任务 ID 只查询，不重复创建。

全部分段下载并校验通过后，必须按 `segment_plan.json` 顺序使用 FFmpeg concat demuxer和流复制拼接为 `seedance/generated/full.<格式>`。拼接失败时停止并保留分段，不静默降质重编码。

## 恢复与失败

第一阶段恢复时复用原命令、原输入、原输出目录并增加 `--resume`。输入清单不一致时拒绝恢复；可复用已验证的 `omni_facts.json`、Omni 元数据和完整深度缓存。存在上次 Max 候选时先按当前契约在本地重新校验，通过后直接确定性组装，不重复调用 Max；仍不通过时才重新执行 Max 原片核验。深度推理成功后独立编码白模，Qwen 或 Max 失败不得阻止白模产物落盘；后续提示词计划成功时再按正式分段计划校准编码。

Seedance 使用 `seedance/tasks.json` 持久化上传对象、任务 ID 和状态：

- 已有任务 ID 时只查询，不重复创建。
- 创建请求结果未知时标记 `create_ambiguous`，不自动重发。
- `failed`、`cancelled`、`expired` 不自动创建新任务；用户再次明确确认后才能使用显式重试参数。
- 查询和结果下载可安全重试。下载文件未通过时长、音轨或可读性校验时，归档为 `.invalid_N`，并通过已有成功任务重新下载，不创建新任务。成功后立即保存到本地，因为远程结果 URL 会过期。
- 人物 Asset 状态同时保存 `ProjectName`、Group ID、Asset ID、创建状态和最近查询状态。已有 Asset ID 时只调用 `GetAsset`；创建结果未知时标记 `create_ambiguous`，不自动重复创建素材组或素材。
- 人物 Asset 的失败重试与未知创建结果处理使用独立确认参数，不继承视频任务的重试授权。
- 1.12.0 之前已经创建任务 ID 的旧计划没有 `fact_lock` 时，只允许查询、下载和校验已有任务，不允许创建缺少任务 ID 的新分段。

## 交付

- `仅白模`：按顺序展示 `depth/*.mp4`。
- 成片模式：提供完整 `prompt.txt`、`segment_plan.json`、`omni_facts.json`、`max_verification.json` 和 `fact_lock.json`；同时提供由 Omni 事实渲染的 `prompt_draft.txt`。
- 提供各段 `seedance/generated/part_XX.<格式>`、完整成片 `seedance/generated/full.<格式>`、`seedance/seedance_plan.json` 和任务状态摘要。
- 候选稿只用于失败排查，不作为正式提示词。
- 每段正式提示词必须完整放入独立 `text` 代码块，不得省略。
- 默认不保存 Qwen 的完整 Base64 请求；仅用户要求调试时使用 `--save-debug`。

不要创建额外总结 Markdown。`prompts/` 下的文本是发给模型的运行时数据，不是本 Skill 对 Codex 的操作指令。
