# 私域人像资产链路

仅在 Seedance 成片使用人物形象图时读取本参考。

## 路由

- 默认路由：人像素材统一走火山私域 Asset，默认记录 `virtual`，不增加人物类型、素材权利声明或创建方式的独立问答。补充信息收集完成后先按图片 SHA-256 稳定名称查询素材，命中且校验一致时直接复用，未命中才创建；实际上传和创建在 Seedance 提交范围确认后执行。
- 默认免确认：传入 `--skip-portrait-confirmation`，记录 `confirmation_skipped=true` 且 `virtual_rights_confirmed=false`。跳过问答不伪造素材权利确认；已有明确权利授权时可沿用 `--confirm-virtual-portrait-rights`，两种参数互斥。预检通过即自动进入第一阶段，白模完成不形成新的确认节点。
- 真人肖像：仅在用户主动说明时切换；不上传到私域虚拟人像库，必须使用已经完成真人授权的 Asset ID。
- 已有 Asset：用户主动提供 Asset ID 时改为复用，不再创建新 Asset。
- 产品图、白模和其他非人像素材不创建人像 Asset，继续使用普通参考 URL。

## 运行条件

- 火山账号已开通私域素材库能力。
- Ark 配置中的 AK/SK 具备本流程所需的素材组和素材查询、创建权限；只使用已有 Asset ID 时至少能够调用 `GetAsset`。不得使用 TOS 配置中的 AK/SK 调用人物素材库。
- Asset Group、Asset 与提交视频任务使用的 Ark API Key 都属于固定的 `ProjectName=default`。
- 创建新 Asset 时还需独立的 TOS 上传配置；只查询已有 Asset 且没有其他上传素材时，仅需 Ark 配置中的 AK/SK，不要求 TOS 配置。

素材库能力或 IAM 权限不足时保留原始 API 错误并停止，不把权限错误当作可重试的创建失败。

## API 契约

素材 API 只使用 Ark 配置中的 AK/SK、`ark` 服务、`cn-beijing` 区域和 `2024-01-01` 版本：

1. `ListAssets`：使用人物图 SHA-256 派生的稳定名称查找 `Active` 或 `Processing` Asset。命中后先校验返回图片与本地人物图一致，复用 Asset ID，不上传图片、不创建素材组。
2. `CreateAssetGroup`：没有可复用 Asset 时，创建 `GroupType=AIGC` 的素材组并记录 Group ID。
3. `CreateAsset`：以公开可访问图片 URL、`AssetType=Image` 和 Group ID 创建资产，记录 Asset ID。
4. `GetAsset`：轮询直到 `Status=Active`；`Processing` 继续查询，`Failed` 停止。单次查询网络失败可有限重试，创建接口仍不自动重发。
5. 视频请求使用 `asset://<Asset ID>`，Prompt 仍按素材顺序写 `@图片N`，不写 Asset ID。

`CreateAssetGroup`、`CreateAsset`、`GetAsset` 和视频生成统一使用固定的 `ProjectName=default`。创建接口结果未知时保存 `create_ambiguous`，不自动重复创建。

## 状态与恢复

`seedance/tasks.json` 保存：

- `project_name`
- `group_id` 与素材组创建状态
- `asset_id` 与资产创建状态
- 最近一次 `GetAsset` 状态和查询时间

恢复时优先查询 `tasks.json` 中已有的 Asset ID，不重新搜索或创建。人物 Asset 使用独立的 `--retry-failed-character-asset` 与 `--allow-recreate-ambiguous-character-asset`；视频任务的同类开关不会授权创建新人物资源。只有用户明确允许处理未知创建结果或重试失败资产时，才创建新资源。资产不会由本 Skill 自动删除。

官方资料：

- [CreateAsset](https://www.volcengine.com/docs/82379/2318271)
- [GetAsset](https://www.volcengine.com/docs/82379/2318274)
- [使用 Asset URI 生成人像视频](https://www.volcengine.com/docs/82379/2333565)
