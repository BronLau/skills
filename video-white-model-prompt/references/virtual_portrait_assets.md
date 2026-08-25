# 私域人像资产链路

仅在 Seedance 成片使用人物形象图时读取本参考。

## 路由

- 虚拟人像：用户确认素材为虚拟形象且拥有完整权利后，可创建或复用私域虚拟人像 Asset。
- 真人肖像：不上传到私域虚拟人像库；必须使用已经完成真人授权的 Asset ID。
- 无法判断类型：停止人像资产步骤，请用户确认类型。
- 产品图、白模和其他非人像素材不创建人像 Asset，继续使用普通参考 URL。

## 运行条件

- 火山账号已开通私域素材库能力。
- AK/SK 具备本流程所需的素材组和素材查询、创建权限；只使用已有 Asset ID 时至少能够调用 `GetAsset`。
- Asset Group、Asset 与提交视频任务使用的 Ark API Key 属于同一 `ProjectName`。
- 创建新 Asset 时还需可用的 TOS 上传配置；只查询已有 Asset 且没有其他上传素材时，仅需 AK/SK 与区域配置，不要求 Bucket 或 Endpoint。

素材库能力或 IAM 权限不足时保留原始 API 错误并停止，不把权限错误当作可重试的创建失败。

## API 契约

素材 API 使用 AK/SK、`ark` 服务、`cn-beijing` 区域和 `2024-01-01` 版本：

1. `ListAssets`：使用人物图 SHA-256 派生的稳定名称查找 `Active` 或 `Processing` Asset。命中后先校验返回图片与本地人物图一致，复用 Asset ID，不上传图片、不创建素材组。
2. `CreateAssetGroup`：没有可复用 Asset 时，创建 `GroupType=AIGC` 的素材组并记录 Group ID。
3. `CreateAsset`：以公开可访问图片 URL、`AssetType=Image` 和 Group ID 创建资产，记录 Asset ID。
4. `GetAsset`：轮询直到 `Status=Active`；`Processing` 继续查询，`Failed` 停止。单次查询网络失败可有限重试，创建接口仍不自动重发。
5. 视频请求使用 `asset://<Asset ID>`，Prompt 仍按素材顺序写 `@图片N`，不写 Asset ID。

`CreateAssetGroup`、`CreateAsset`、`GetAsset` 和视频生成使用相同 `ProjectName`。默认 `default`，非默认项目必须显式传入。创建接口结果未知时保存 `create_ambiguous`，不自动重复创建。

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
