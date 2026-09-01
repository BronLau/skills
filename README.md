# Codex Skills

个人维护的 Codex Skills 集合。

## Skills

### cl-write-prd

根据当前 Codex 会话中已经确认的产品原型、交互调整和业务决策，为用户提供的飞书云文档编写或更新产品需求文档。

适用于以下工作流：

1. 在当前会话中完成原型页面和交互方案；
2. 提供一个飞书需求文档链接，目标文档通常只有标题；
3. 触发 Skill 汇总会话中的最终需求，并分阶段写入文档。

核心能力：

- 以当前会话中的最新确认结果为事实来源，过滤已经被覆盖的历史方案；
- 读取指定范本文档的最新版本，复用其写作和排版风格；
- 根据当前功能动态设计大纲，不固定需求文档或 Prompt 的内部结构；
- 使用“截图内容描述”文本占位，由用户手动完成原型截图并替换；
- 业务流程使用飞书画板原生节点，验收标准使用文档待办复选框；
- 使用块级编辑更新飞书文档，避免覆盖未授权内容。

调用示例：

```text
使用 $cl-write-prd，根据当前会话中已经确认的原型和需求，为这个飞书文档编写需求文档：<飞书文档链接>
```

### v2role-card

从上传的视频中识别主要人物、抽取关键帧和分析视觉风格，并为用户确认后的每个角色生成一张与源视频风格一致的 16:9 四视图角色卡。

核心流程：

1. 解析视频并生成带时间点的候选帧与联系表；
2. 提议主要角色数量，整理每个角色的外貌、发型、服装、体态、身份识别锚点和视频风格；
3. 等待用户同步确认角色数量、逐角色文本描述和角色卡风格；
4. 分角色整理身份与服装参考图；
5. 生成左侧正面头肩大特写、右侧全身正面/90 度侧面/背面的单行角色卡；
6. 校验身份、服装、角度、构图和 16:9 比例后交付。

调用示例：

```text
使用 $v2role-card 分析我上传的视频，确认角色数量、逐角色描述和视频风格后生成同风格 16:9 四视图角色卡。
```

### video-reverse-replicate-product

输入一段成片视频和可选的 1-3 张同一目标产品图片，输出一套可复刻的生产素材包：分镜大纲、角色/场景/产品参考图、段级生视频提示词，以及可选的复刻成片。提供产品图时保留原片剧情与产品调度，把原片产品替换为用户产品；不提供时保持原流程。

与底座 `video-reverse-replicate` 的四点差异：

- 角色参考图走抽帧 I2I：按角色卡出现镜头从原片抽 3 张候选帧，用户挑 1 张定稿后与角色卡 Prompt 一起图生图，帧锁身份、Prompt 锁构图；
- 成片全程无字幕，由提示词控制而非后期裁剪；
- 生成段时长上限由 ≤15s 放宽到 ≤30s，段越少跨段衔接点越少；
- 可选产品替换：原片控制产品的数量、位置、尺度、朝向和光影，用户图片控制造型、包装、材质与品牌标识，多图共同定义同一个产品。

两个硬检查点：参考图确认前必须停下等用户确认；生视频默认不执行，需用户明确要求。

调用示例：

```text
使用 $video-reverse-replicate-product 复刻这段视频，并把里面的产品替换成我提供的产品图。
```

### video-white-model-prompt

从唯一参考视频生成近白远黑的单目相对深度白模，或经 Qwen3.5-Omni-Plus 出视听初稿、Qwen3.8-Max 精修为提示词后，调用 Doubao Seedance 生成分段成片。

三种产出范围：仅白模、白模+提示词+Seedance 成片、无白模+提示词+Seedance 成片。不支持只反推提示词。

核心特点：

- 渐进式向导交互，每轮只问一项，已提供的信息自动跳过对应步骤；
- 分段时长可选 15s（Seedance 2.0）或 30s（Seedance 2.5）；
- 按官方 Seedance 2.5 结构确定性组装素材绑定、生成目标、镜头、镜头内动作阶段、声音和全片约束；
- 两次确认机制：第一阶段产物确认后才收集提交配置，完整展示提交范围后才真正提交；
- 密钥只从环境变量或本机 Key 文件读取，不要求用户在聊天中粘贴。

调用示例：

```text
使用 $video-white-model-prompt 把这段参考视频生成白模，并反推提示词生成 Seedance 成片。
```

## 本地安装

将 Skill 目录复制或软链接到 Codex Skills 目录：

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/skills/cl-write-prd ~/.codex/skills/cl-write-prd
ln -s /absolute/path/to/skills/v2role-card ~/.codex/skills/v2role-card
ln -s /absolute/path/to/skills/video-reverse-replicate-product ~/.codex/skills/video-reverse-replicate-product
ln -s /absolute/path/to/skills/video-white-model-prompt ~/.codex/skills/video-white-model-prompt
```

安装后可在新的 Codex 对话轮次中使用对应的 `$skill-name` 触发。

## 密钥与配置

两个视频类 Skill 需要模型 API Key，均只从环境变量或本机 Key 文件读取，不在仓库中保存任何凭证：

- `video-reverse-replicate-product`：`DASHSCOPE_API_KEY`，需开通 qwen3.8-max、qwen3.5-omni-plus、qwen-image-3.0-pro、wan3.0-video；依赖 `pip install openai dashscope pillow`。
- `video-white-model-prompt`：`DASHSCOPE_API_KEY` 用于 Qwen；Ark 配置中的 `ARK_API_KEY` 用于 Seedance，人物素材库另使用同一 Ark 配置文件中的 `accessKey`、`secretKey`；带白模或待上传素材时使用独立 TOS 配置文件，TOS 凭证仅用于 STS/TOS 上传；依赖见 `video-white-model-prompt/requirements.txt`。

深度模型路径可通过 `DEPTH_ANYTHING_MODEL` 环境变量或 `--depth-model` 指定，也可放在 `video-white-model-prompt/models/` 下。
