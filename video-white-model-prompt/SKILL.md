---
name: video-white-model-prompt
description: 当用户明确要求把参考视频生成近白远黑的单目深度白模，或在两阶段反推完整视听提示词后调用 Doubao Seedance 2.0/2.5 生成成片时使用；支持人物图参考，以及经权利人授权的 2–15 秒人声音色参考。支持仅白模、白模+提示词+Seedance成片、无白模+提示词+Seedance成片；不支持只反推提示词，也不要因普通视频分析、静态图片深度或 3D/建筑白模需求而触发。
metadata:
  version: 1.18.5
---

# 视频白模、提示词反推与 Seedance 成片

从唯一参考视频生成单目相对深度白模，或由 Qwen 3.8 Omni Flash（`qwen3.8-omni-flash`） 先提取结构化原片事实、Qwen3.8-Max 再对照原片核验事实并绑定替换外观，最后由程序确定性组装 Seedance 提示词，并按用户选择带或不带白模参考生成分段成片。

## 触发边界

“白模”可能指 3D 或建筑白模时先澄清。用户只要求提示词反推、普通视频分析、视频复刻、产品替换或静态图片深度时不触发；本 Skill 的提示词链路必须以 Seedance 成片为目标。

## 用户交互

采用渐进式向导收集信息。每轮消息围绕当前步骤提出一个问题，提供带编号的完整选项和回复示例。已经明确提供的信息自动填入并跳过对应步骤；后续步骤根据已选分支动态展开。成片凭证是一个完整步骤：补充信息收集完成后，在同一条消息中一次收集本次所需的 Qwen、Ark 和 TOS 配置来源，用户可一次回复全部缺项；已提供的信息自动沿用。

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
8. `匿名化视觉效果参考图`，只约束用户明确指定的色彩、材质、纹理、光泽或前后效果，不定义人物或产品身份

用户可回复一个或多个编号，例如 `2,3,6`；选项 1 与其他选项互斥。随后每轮只收集一个已选字段；每个字段都提供 `1. 现在提供`、`2. 跳过此项`。文件类字段在选择“现在提供”后，再提供 `1. 使用当前消息附件`、`2. 提供本地文件路径`。选择 `1. 不提供补充信息` 时直接进入下一步。

产品名称、卖点和创意默认不改变参考视频台词。只有用户主动提出口播修改时，才展示：`1. 保持原台词`、`2. 只替换指定旧词`、`3. 允许整段改写`。选择 2 后逐项收集 `旧词=新词`；选择 3 时记录明确授权。用户未提及口播修改时自动采用选项 1，不增加询问步骤。

选择口播音色参考时，必须由用户明确确认已获得声音权利人的授权，同意抽取、上传并用于音色参考。参考音频只约束全片人声的音色、发声质感、语速与韵律，台词仍以正式 Prompt 为准；不得把参考音频中的原台词、背景音乐或环境声当作成片内容。用户要求从参考视频抽取时，优先选择连续清晰口播并输出恰好 15 秒的单声道 WAV；原视频保持只读。声音授权单独记录，不因人像素材免确认而跳过。

选择人物形象图时，读取 [私域人像资产链路](references/virtual_portrait_assets.md)。人像素材默认统一走火山私域 Asset 路由，记录 `character_image_type=virtual`；这是默认工作流路由，不是自动人脸判断。补充信息收集完成后，先按图片 SHA-256 稳定名称查询已有素材，命中且校验一致时直接复用，未命中时才创建。人物类型、素材权利声明和创建或复用方式不增加独立确认步骤；默认传入 `--skip-portrait-confirmation`，记录 `confirmation_skipped=true` 并保持 `virtual_rights_confirmed=false`，不把跳过问答写成已取得权利确认。已有明确素材权利授权时可以沿用 `--confirm-virtual-portrait-rights`，两种参数互斥。用户主动提供 Asset ID 时复用；用户主动说明是真人肖像时切换为 `real` 并使用已经完成真人授权的 Asset ID，遵守素材库的类型和授权约束。查重与访问预检不触发上传、资产创建或视频任务；实际创建在 Seedance 提交范围确认后执行。

选择匿名化视觉效果参考图时，逐张收集文件，并用一句不超过 500 字的 `effect_reference_scope` 明确只参考的效果维度。图片中若含自然人五官且只需要肤色、材质或光影效果，先匿名化身份特征并验证目标区域的色彩与纹理没有被编辑破坏。效果图不进入人物 Asset、不进入 `appearance_bindings`，按普通 TOS 图片上传；运行时 Prompt 使用独立的 `@图片N` 约束，明确不参考其身份、五官、脸型、发型、表情、姿态、动作、构图、背景、马赛克或裁切。

### 步骤 4：一次收齐成片凭证来源

仅两种 Seedance 成片模式需要。补充信息收集完成后，在同一条消息中列出本次全部凭证缺项，不把 Qwen、Ark 和 TOS 配置拆成多轮询问：

- **Qwen**：`DASHSCOPE_API_KEY` 环境变量或 Key 文件路径。
- **Ark**：`ARK_API_KEY` 环境变量或配置文件路径；使用人物 Asset 时，同一来源还需素材库 AK/SK，即文件中的 `accessKey`、`secretKey` 或环境变量 `ARK_ACCESS_KEY`、`ARK_SECRET_KEY`。
- **TOS**：本次有白模、产品图、效果图、音色参考或可能新建的人像等上传素材时，提供 TOS 环境变量或配置文件路径；明确无需上传素材时省略。

统一提供以下选项：

1. `使用已配置的环境变量`
2. `提供配置文件路径`：一次回复所需的 Qwen、Ark 和 TOS 路径，各项可分别使用文件或环境变量
3. `暂未配置`：配置完成后从本步骤继续

回复示例：`同意本次 Qwen 分析发送；Qwen=/本地路径/DASHSCOPE_API_KEY.md；Ark=/本地路径/Volcengine_API_KEY.md；TOS=/本地路径/TOS_Config.md`。已提供的路径或已选择的环境变量来源直接沿用；用户只补充部分信息时，在同一轮列出全部仍缺少的必要配置，不重新收集已完成项。不要要求用户在聊天中粘贴密钥；集中校验收到的配置，一次报告发现的缺项。

同一条消息说明第一阶段将分析视频及其音轨发送至百炼 Qwen Omni，并将分析视频、Omni 事实及本次人物或产品图发送至 Qwen Max 的具体范围（默认目的地 `dashscope.aliyuncs.com`）；已有明确发送授权时直接沿用。配置收集与素材发送授权不扩展为 Seedance 上传或付费任务创建授权。Ark 与 TOS 配置提前收集，只在各自所需阶段使用；凭证齐全、预检通过后直接执行第一阶段，白模完成不形成独立确认节点。

所有生成视频在用户未明确指定分辨率时统一使用 `720p`；只有用户明确要求时才传 `480p` 或 `1080p`。其余默认值为跟随原片画幅、`mp4`、无水印，并根据原片是否有音轨自动决定 `generate_audio`。

### 步骤 5：预检与分析媒体准备

以只读方式检查视频、图片、转写、Key 来源、`ffmpeg`/`ffprobe`、Python 依赖、磁盘和按范围所需的深度模型。

Qwen 分析媒体继续使用 Base64 Data URL，逐文件上限为 9.5 MiB；视频超限时不上传 TOS，也不增加用户确认步骤，直接按动态 FFmpeg 参数生成压缩分析副本。原片始终只读；压缩副本写入本次运行的独立暂存目录，保持 `--video` 指向原片、`--analysis-video` 指向压缩版。共享 `scripts/media_preflight.py` 负责两者的音轨、画幅、时长、时间轴和五点画面相似度校验；压缩或一致性校验失败时停止，不回退为上传超限原片。图片超限时不得擅自改变用户提供的外观素材，仍应提示用户提供合规图片。

Seedance 成片模式要求参考视频至少 4 秒。每段必须是 4 到用户上限之间的整数秒，分段数量固定为 `ceil(总时长 / 用户上限)`：优先使用最少任务数并让各段尽量接近最大时长；尾段不足 4 秒时前移上一切点重新分配，不产生短尾段。

带白模成片时，正式白模仍保持原画幅、CFR、H.264、720p、无音频。若其 FPS、编码、尺寸或宽高比不符合 Seedance 参考视频要求，只在 `seedance/assets/` 生成兼容副本，不修改正式白模。用户明确要求用 OpenPose/DWPose 关键点视频替换白模参考时，保持原分段数量与顺序，校验参考视频与对应分段的画幅、帧率、帧数、时长、编码和无音频约束，并在准备计划时设置 `--motion-reference-type openpose`；OpenPose 只负责人体、面部、手部关键点的位置、姿态与时序，不得被描述成深度图，也不得把黑色背景、彩色骨架线、关键点或连线生成到成片。

Seedance 图片在提交前检查总数、大小、像素和宽高比。图片类型素材总数包含人物图、产品图和效果参考图：15 秒分段对应 Seedance 2.0，合计不得超过 9 张；30 秒分段对应 Seedance 2.5，合计不得超过 30 张。人物形象图还必须具有明确的人像类型和 Asset 路由；产品图与效果参考图不进入人像素材库。使用人物 Asset 前确认账号已开通私域素材库能力，Ark 配置中的 AK/SK 具备所需 IAM 权限，且 Asset、Asset Group 与 Ark API Key 都属于固定的 `ProjectName=default`。创建新虚拟 Asset 时，先按人物图 SHA-256 稳定名称调用 `ListAssets` 查找可复用素材；该查询也作为上传前的访问预检。Ark API 拒绝时原样保留错误并停止。

音色参考在提交前检查格式、大小、时长和纯音频流：仅 MP3/WAV、小于 15 MB、时长 `[2,15]` 秒。正式分段 Prompt 使用 `@音频1`，明确只参考统一人声音色，不复用参考音频中的台词或背景声。音色参考作为 `audio_url`、`role=reference_audio` 提交；它不是人物图片 Asset，也不混入虚拟人像素材组。

深度模型只在 `仅白模` 和 `白模+提示词+Seedance成片` 中解析，顺序为命令参数、`DEPTH_ANYTHING_MODEL`、Skill 本地模型、机器已知模型路径。无白模模式不得要求深度模型。

## 第一阶段：准备正式产物

所需输入与凭证来源明确、预检通过后，自动串联深度任务、Omni、Max 和 Seedance 计划准备，不再设置启动前总确认或白模完成后的确认。用简短进度说明告知本次范围、分段和模型后直接执行；只处理仍缺少的必要信息、音色参考授权或实际预检失败，不重复询问已确定的配置。

成片模式将分析视频及其音轨发送给 Omni，并将分析视频、Omni 事实及人物或产品图片发送给 Max。正常各调用一次，失败修复仍遵循下述有限重试契约。仅白模模式直接执行本地深度任务。Seedance 素材上传、人物 Asset 创建和视频任务创建仍在单独的提交确认之后执行；自动启动第一阶段不代替这些提交授权。

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
  --effect-image <可重复；匿名化视觉效果参考图> \
  --effect-reference-scope <只参考的色彩、材质、纹理、光泽或效果维度> \
  --character-image <可选> \
  --character-image-type <virtual|real> \
  --character-asset-id <已有Asset时传入> \
  --skip-portrait-confirmation \
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
  --confirm-max-conflicts <恢复时、Max纠正通过校验后自动追加> \
  --output-dir <新目录>
```

提示词链路和深度推理在带白模模式下并行运行。Omni 只依据分析视频输出全局视听设定 `overall_av`、主体、分段、连续镜头时间轴、镜头内连续动作阶段 `beats`、景别、机位、运镜、构图、可见身体范围、人物动作、操作人员与产品动作、进出场、场景光线和音频事实。Max 同时接收原片、Omni 事实和替换图片，对照原片核验每个视觉字段与 beat，并输出事实修正、核验后事实、静态外观绑定及按权限开放的音频覆盖。

Omni 新任务返回 Schema v3：`overall_av.visual` 记录全片共用表现形式、风格、光影与氛围，`overall_av.audio` 记录语种、音色、配乐与环境声基底，不包含台词全文。分段使用最少任务数和整数秒任务边界；段内镜头及 `beats` 使用可含小数的精确相对秒数，按真实切镜和动作边界连续覆盖各自范围，不为正文的整秒显示合并快切或重分台词。动作、表情、操作人员或产品动作发生阶段变化时拆分 beat，但不得虚构切镜。Schema v2/v3 的每组人声事实必须在同一 `audio` 字符串中紧邻绑定说话人、画内/画外位置和口型状态，使用 `<说话人>（画内、口型同步）说：{逐字台词}` 或画外对应形式；多人依次说话时同时记录其他画内人物自然闭口聆听，原片确有同时说话时明确标记重叠发声。Schema v3 中音乐用 `()`、音效与环境声用 `<>` 完整包裹，台词用 `{}`；同镜头同声源的连续发言合组，跨镜保留各镜头实际承载的原文。内部主体 ID 仍为 `<稳定自然称谓>`，正文输出时去掉主体 ID 的尖括号，避免与声音标记混淆。程序拒绝无说话人归属的 `{}`、用引号代替 `{}`、画内说话不同步口型，以及多人对话中未说明闭口或重叠关系的事实。Schema v1/v2 旧事实仍可用于恢复，保持原整数时间契约；旧音频类型前缀在正文输出时转为公共标记。没有可辨识人声时设置 `no_speech_confirmed=true`。人物图、产品图、名称、卖点和创意不发送给 Omni，避免替换素材污染原片动作理解。Omni JSON 不通过时携带具体错误定向修复一次；修复后仍存在可解析的音频或人声矛盾时写入 `omni_conflicts.json` 并继续进入 Max 仲裁。JSON 无法解析，或主体、分段、镜头或有效时间轴不完整时才停止，因为此时没有可供 Max 核验的稳定事实骨架。

Max 不直接输出最终 Prompt。它必须保持 Omni 的段级数量、顺序、边界和时长；Qwen 3.8 Max 是矛盾事实的仲裁结果，可以依据原片和用户 transcript 纠正全局视听设定、主体清单、镜头与 beat、连续时间区间、视觉字段、原片音频事实和 `no_speech_confirmed`。用户在 `user_idea` 中明确指定视觉效果时，Max 必须把强度、色调、材质、纹理、状态边界和持续时间完整落实到对应镜头或 beat，不得压缩成概括性形容词；持续效果在后续 beat 中继续明确。每类变化应在 `fact_review.corrections` 中逐项写明字段路径、完整 Omni 原值、完整修正值、证据时间和证据说明。correction 路径与校验粒度保持一致：全局视听变化使用 `overall_av`；主体清单变化使用 `subjects`；人声存在性变化使用 `no_speech_confirmed`；镜头数量、顺序或起止时间变化使用 `segments[i].shot_plan`；镜头结构变化同时带来视觉内容变化时另用 `segments[i].shot_visuals`，带来音频变化时使用 `segments[i].audio_plan`；镜头结构不变时，单镜头音频变化使用 `segments[i].shots[j].audio`，视觉字段和 beat 仍使用对应逐镜头路径。若 Max 的 verified facts 已通过严格校验，但 corrections 遗漏、状态不匹配或证据格式不合格，程序不丢弃 Max 结果，而是从 Omni 与 Max 的实际差异确定性补齐 correction，使用对应路径允许的原片采样点，并在矛盾报告中保留修正记录；通过严格校验后自动采纳。无法通过严格 Schema、时间轴、图片绑定或授权边界校验的 Max 结果仍停止。

人物图和产品图只进入结构化 `appearance_bindings`，每项只能包含主体标签和图片编号数组，不允许 Max 输出自由文本主体定义。人物图只能绑定 `kind=character`，产品图只能绑定 `kind=product`，每张图片只能绑定一个主体；程序为每张图片生成逐项素材绑定，明确对应主体、只参考的静态外观维度以及不参考的姿态、动作、景别、机位或拼版布局。效果参考图不进入 `appearance_bindings`，由 Seedance 准备阶段在人物图和产品图之后编号，并把用户确认的独立作用域写入运行时 Prompt。因此替换素材与效果素材都不能通过定义文本夹带越权的身份、动作、姿态、景别、运镜、身体可见范围或进出场。用户在事实锁之后明确要求多个原片人物统一为同一人物形象时，不让 Max 重写动作事实，而是使用受限静态覆盖把这些人物标签合并为一个中性目标标签，再把人物图绑定到该目标标签；原标签中的发型、服装等外观词不得继续进入正式 Prompt。

默认音频采用 Max 对原片核验后的事实；没有矛盾时应与 Omni 保持一致。Max 对台词格式、说话人、口型或 `no_speech_confirmed` 的修正属于事实仲裁，必须进入 corrections，不视为用户授权的创意改写。指定词替换仍由程序确定性执行；只有用户明确允许整段改写时，Max 才能通过 `audio_overrides` 修改对应镜头音频。每个音频覆盖项必须且只能包含整数 `segment_index`、整数 `shot_index` 和完整字符串 `audio`，并指向已存在的镜头。改写后的声音描述必须自包含且可直接执行，沿用对应事实 Schema 的声音契约；不得引用不会提交给 Seedance 的原始媒体。最终 Prompt 由程序使用已通过严格校验并自动采纳的 Max 核验事实、静态外观绑定和授权音频覆盖确定性组装。

已锁定计划因音频策略失败，而用户只授权修改音频时，保留原失败计划和任务状态；Max 新结果只提供通过契约校验的 `audio_overrides`。使用 `scripts/merge_verified_audio_override.py` 把这些音频覆盖确定性合并到上一版已锁定的视觉事实与外观绑定中，拒绝新 Max 结果带来的任何视觉字段、主体、分段或时间轴漂移，并为新计划生成独立事实锁。

正式正文遵循 [视频提示词公共规范](references/video_prompt_output_rules.md)，由 `scripts/video_prompt_format.py` 统一渲染、校验。每个独立生成分段严格按顺序包含三个独立标题：`主体与场景定义：`、`整体视听设定：`、`分镜与声音：`。产品、人物、场景的稳定特征与实际图片绑定集中定义；后续使用稳定自然称谓。全局视听设定来自已核验事实，不写死真实摄影，不将局部状态扩大为全片约束。每个镜头使用 `镜头N｜MM:SS - MM:SS`，以自然语言串联起始画面、构图运镜、连续动作、状态与衔接，随后单独成段交代声音；`beats` 保留在事实中，正文不显示“动作阶段”字段表。

每段时间从 `00:00` 开始，编号从 1 递增。展示起点向下取整、终点向上取整，不足一秒的真实镜头可共享显示区间，不能反过来按显示值合并镜头或增加时长。Schema v3 的精确镜头时间保留在 `segment_plan.json.shot_timelines` 与锁定事实中；节目分段仍使用原整数边界。分段包装标题仅用于拆分文件，不提交给 Seedance。正文不含代码围栏；在对话里交付时可以使用外层 `text` 代码块。

默认在整体视听设定中明确“全片不生成字幕”；保留有效包装、场景标牌、界面和独立剧情或业务文字，不补猜模糊文字。只有用户明确要求字幕时才写入 `〖字幕原文〗` 并注明位置、样式和时间。水印、账号标识、批文、认证、免责声明及附属非剧情前贴不进入正文。字幕过滤不删减真实人声或切镜。每段使用实际存在的 `@图片N`，不保留泛化占位符，不写 4K、画幅、分辨率等 API 参数，也不写视频编辑或延长意图。

`seedance_video_pipeline.py prepare` 在公共三段结构内补充运行时素材职责：白模或 OpenPose 的 `@视频1`、匿名化效果图进入主体与场景定义；`@音频1` 的音色职责进入整体视听设定，不在首个标题前追加说明。人物拼版共同定义一个主体，不生成多个副本。白模只锁定动作骨架、姿态变化、遮挡、空间、机位、运镜与时序，不采用灰阶深度材质；人物身份与静态外观以绑定人物图为准。OpenPose 只负责人形关键点位置、姿态、构图与时序，不采用黑底、骨架线和关键点标记。效果图只按确认的作用域使用；音色参考不复用其台词或背景声。无白模模式不增加视频引用。

公共规范在 Skill 内保留可移植副本；本次对齐的是正文契约，修改规范文件不会自动改写 Python 渲染逻辑。后续格式变更需同步公共渲染器与契约测试。

没有事实矛盾时，第一阶段写入 `omni_facts.json`、`max_verification.json`、`fact_lock.json`、`ready_for_seedance.json` 和 `seedance/seedance_plan.json`，但不调用 Seedance。存在 Omni 契约矛盾或 Max corrections 时，先写入 `omni_conflicts.json`、`max_verification.json`、`max_conflict_report.json` 和 `awaiting_max_conflict_confirmation.json`；此时不生成正式 Prompt、事实锁或 Seedance 计划，也不视为执行失败。

### Max 纠正自动采纳

Max 纠正结果通过 Schema、时间轴、素材绑定及音频改写权限校验后，直接采纳并继续，不向用户请求单独确认。简短说明实际修正内容即可。程序返回 `awaiting_user_confirmation` 或写入 `max_conflict_report.json` 时，将其视为待自动接受的内部状态：核对原片、transcript、Max 候选和报告未变化后，复用原命令、原输出目录，自动增加 `--resume --confirm-max-conflicts`，复用缓存生成正式 Prompt、事实锁和 Seedance 计划，不重复调用 Omni 或 Max。

`fact_lock.json` 继续绑定已接受的修正报告；内容变化时重新校验最新候选，校验通过后自动接受，不沿用旧报告的接受状态。严格校验失败或缺少实质输入时才处理对应问题；自动采纳事实纠正不授予整段台词创意改写权限。

## 提交确认与 Seedance 提交

第一阶段成功且 Max 纠正结果已通过校验并自动采纳后，直接复核步骤 4 已收集的 Qwen、Ark 和 TOS 配置。只有路径失效、凭证字段缺失或配置要求实际变化时，才在同一轮列出全部新增缺项并集中校验；已齐全时直接展示完整正式提示词与生成范围，进入提交确认，不再重复收集凭证或确认白模。Ark API Key 只用于 Seedance，Ark AK/SK 只用于人物素材库；TOS 凭证只用于 STS 与 TOS 上传，三类凭证互不回退。

使用人物 Asset 时不再询问 `ProjectName`，固定使用 `default`。Asset Group、Asset 和 Ark API Key 必须都属于 `default` 项目。

完整展示正式提示词、Max 事实修正摘要、Seedance 模型、任务数、每段时长、是否带白模、图片数量、音色参考及授权状态、人物类型、人物 Asset 创建或复用方式、固定的 `ProjectName=default`、`generate_audio`、分辨率、画幅、格式和水印。说明每段是独立生成任务；使用 `publicDomain` 时素材链接公开可读，当前写入角色不能主动删除对象，存留依赖 Bucket 生命周期。原始参考视频和完整原始音轨不提交 Seedance；音色参考仅提交用户授权的片段。用户明确确认提交范围后才运行：

1. `确认并提交全部分段`
2. `修改生成参数`
3. `暂不提交并保留第一阶段产物`

选择 2 时，先确认 `seedance/tasks.json` 不存在，或其中 `uploads`、`segments` 均为空；已有上传记录或任务 ID 时不覆盖原计划。确认尚未提交后，复用原 `prompt.txt`、`segment_plan.json`、原片、图片和白模目录，重新运行 `seedance_video_pipeline.py prepare --overwrite`，只替换用户修改的参数；未明确修改的参数和原 Seed 保持不变。重建并校验 `seedance_plan.json` 后，重新展示提交确认。

用户在尚未上传或创建任务时明确要求修改人物静态服饰、场景或构图的，不能直接手改已锁定 Prompt。把用户确认后的覆盖项写入独立 JSON，并使用 `scripts/apply_static_visual_overrides.py` 重建 Prompt 和事实锁；覆盖范围只允许主体静态定义、受限 `subject_aliases` 以及逐镜头 `composition`、`scene_light`，不得改变动作、景别、机位、运镜、进出场、时间轴或音频。`subject_aliases` 只用于把用户指定的多个人物标签合并成一个中性人物标签；合并目标必须提供显式静态定义，程序同步替换视觉事实中的标签并合并图片绑定，不修改音频。已有上传记录或任务 ID 时不得覆盖原计划，必须在独立输出目录创建新计划。随后按原参数重新运行 `seedance_video_pipeline.py prepare --overwrite`，校验新计划并再次展示提交确认。

任何 Seedance 任务一旦成功下载并通过时长、音轨和可读性校验，立即向用户展示已生成的每个分段和完整成片，默认在直接交付后结束，不进行成片内容分析。只有用户主动表达分析、质量检查或针对具体成片问题修复的意图时，才按其请求范围进行字幕、人物、产品、场景或声音等分析与 QA；初始生成要求中的“无字幕”等参数不自动触发生成后检查。该触发条件同样适用于下方补救流程。用户请求的 QA 发现问题时，把原成片标记为“已生成、未通过 QA”并保持可见与可下载；不得因质量问题延迟、隐藏或扣留用户已付费生成的视频。补救版本作为新任务或新文件另行确认和交付，不覆盖原成片。

用户在收到成片后指出字幕、台词文字或乱码，并明确要求修正时，先按上述规则展示原成片，再按本次修复意图检查相关问题并进入补救。不重跑 Qwen、不覆盖原计划。复用原 Prompt、事实锁、分段计划、白模、图片、音色参考和 Seed，在独立输出目录重新运行 `seedance_video_pipeline.py prepare`，追加 `--suppress-text-overlays`。该参数是禁止全部可读文字的严格补救模式，只有用户明确接受连同包装、标牌、品牌文字一并禁止时才使用；普通无字幕要求采用公共正文默认规则。严格约束写入整体视听设定和 Seedance 计划参数；重新展示提交确认后才创建新任务。

若使用 `--suppress-text-overlays` 重新生成后，逐段抽帧仍发现烧录字幕，不继续重复创建 Seedance 任务。使用 `scripts/remove_burned_subtitles.py` 在本地逐帧识别下半画面中带深色描边的白色字幕并局部修复，重新编码 H.264 视频，同时直接复用原 AAC 音轨；输出到新文件，不覆盖 Seedance 原始成片。必须抽取覆盖全片的采样帧做视觉 QA，确认字幕确实移除且人物与场景没有明显修复破坏后再交付。

若逐帧擦除样例在人物或产品上留下明显修复伪影，停止批量擦除。改用独立的无声视觉计划：准备计划时不传音色参考，设置 `--generate-audio false`、`--suppress-text-overlays` 和 `--strip-dialogue-for-visual-only`。程序按公共正文的视觉段与声音段分离结构，移除全局声音设定、逐镜声音段及人声文案，保留人物说话动作，并在整体视听设定中声明音轨将在本地后期封装；旧稿继续兼容独立声音行。无声视觉成片通过抽帧无字幕 QA 后，使用 FFmpeg 直接复用上一版统一音色 AAC 音轨，不重编码音频；最终文件还需重新校验时长和音轨。

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
  --skip-portrait-confirmation \
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

第一阶段恢复时复用原命令、原输入、原输出目录并增加 `--resume`。输入清单不一致时拒绝恢复；可复用已验证的 `omni_facts.json`、`omni_conflicts.json`、Omni 元数据和完整深度缓存。存在上次 Max 候选时先按当前契约在本地重新校验，通过后不重复调用 Max。已创建 Seedance 任务的旧计划保持其锁定 Prompt 与任务 ID；升级不自动重写或重提已有任务。存在待接受的 Max 纠正时，核对候选、报告与输入并通过校验后自动追加 `--confirm-max-conflicts`；若内容变化，重新校验并接受最新结果，不向用户追加确认。深度推理成功后独立编码白模，Qwen 或 Max 失败不得阻止白模产物落盘；后续提示词计划成功时再按正式分段计划校准编码。

Seedance 使用 `seedance/tasks.json` 持久化上传对象、任务 ID 和状态：

- 已有任务 ID 时只查询，不重复创建。
- 创建请求结果未知时标记 `create_ambiguous`，不自动重发。
- `failed`、`cancelled`、`expired` 不自动创建新任务；用户再次明确确认后才能使用显式重试参数。
- 查询和结果下载可安全重试。下载文件未通过时长、音轨或可读性校验时，归档为 `.invalid_N`，并通过已有成功任务重新下载，不创建新任务。成功后立即保存到本地，因为远程结果 URL 会过期。
- 人物 Asset 状态同时保存 `ProjectName`、Group ID、Asset ID、创建状态和最近查询状态。已有 Asset ID 时只调用 `GetAsset`；创建结果未知时标记 `create_ambiguous`，不自动重复创建素材组或素材。
- 人物 Asset 的失败重试与未知创建结果处理使用独立确认参数，不继承视频任务的重试授权。
- 1.12.0 之前已经创建任务 ID 的旧计划没有 `fact_lock` 时，只允许查询、下载和校验已有任务，不允许创建缺少任务 ID 的新分段。

## 交付

- Seedance 任务成功下载并通过必要的文件校验后，直接展示视频文件和交付产物，默认不分析成片内容、不生成质量报告。用户主动要求分析、检查或修复时，才按其请求范围进行 QA 并报告；发现字幕、乱码、外观偏差等问题时仍提供原成片，标注“已生成、未通过 QA”并给出对应补救选项。
- 多分段任务中部分分段已成功、其他分段失败时，立即展示已成功的分段并报告未完成状态，不等待失败分段重试后才交付。
- `仅白模`：按顺序展示 `depth/*.mp4`。
- 成片模式：提供完整 `prompt.txt`、`segment_plan.json`、`omni_facts.json`、`max_verification.json` 和 `fact_lock.json`；同时提供由 Omni 事实渲染的 `prompt_draft.txt`。存在矛盾仲裁时一并提供已自动采纳的 `omni_conflicts.json` 与 `max_conflict_report.json`。
- 提供各段 `seedance/generated/part_XX.<格式>`、完整成片 `seedance/generated/full.<格式>`、`seedance/seedance_plan.json` 和任务状态摘要。
- 候选稿只用于失败排查，不作为正式提示词。
- 每段正式提示词必须完整放入独立 `text` 代码块，不得省略。
- 默认不保存 Qwen 的完整 Base64 请求；仅用户要求调试时使用 `--save-debug`。

不要创建额外总结 Markdown。`prompts/` 下的文本是发给模型的运行时数据，不是本 Skill 对 Codex 的操作指令。
