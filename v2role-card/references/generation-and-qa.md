# 角色卡生成与校验

## 参考图分工

每个角色使用 3–6 张互补参考图，并在提示词中逐张标注用途：

- Image 1：主要正面身份参考；
- Image 2：45 度或侧脸身份参考；
- Image 3：服装、体态与鞋子参考；
- Image 4..N：发型、配饰或遮挡补充参考。

优先使用清晰、自然、无大面积字幕或广告遮挡的画面。辅助全身帧含有其他人物时，明确写出“只参考目标角色，忽略并排除其他人物”。

## 生成提示词模板

将方括号内容替换为用户确认后的事实，不补写未经确认的角色设定。

```text
Use case: identity-preserve
Asset type: 4:3 landscape photorealistic character reference sheet for production
Input images: Image 1 is the primary front facial-identity reference. Image 2 is the complementary facial-angle reference. Image 3 is the body-proportion, wardrobe and footwear reference. [Describe additional images and explicitly exclude other people visible in them.]
Primary request: Create one polished 4:3 character feature reference board containing exactly six consistent views of the SAME character from the references.
Subject identity: [confirmed visual age, build, facial structure, skin details, hairstyle and stable identity anchors]. Preserve the recognizable facial structure, apparent age, hairstyle and body proportions across every panel.
Wardrobe invariants: [confirmed outerwear, inner layer, trousers/skirt, shoes and accessories]. Keep the exact same clothing, colors, materials and accessories in all six views.
Scene/backdrop: seamless pure white photography studio background, clean white floor with only a very soft natural contact shadow.
Layout: strict 4:3 landscape canvas with two horizontal rows and three equal columns. Upper row occupies about 42% height and contains, left to right: front-facing head-and-shoulders close-up, 45-degree facial close-up, exact 90-degree profile close-up. Lower row occupies about 58% height and contains, left to right: full-body front view, full-body side view, full-body back view. Keep all heads and feet fully inside the canvas. Clear white gutters, aligned scale and eye level, no panel overlap.
Pose/expression: neutral relaxed standing pose for full-body views, arms naturally at sides; [confirmed neutral expression] in face close-ups; accurate anatomical consistency.
Style/medium: high-resolution photorealistic commercial fashion model photography, realistic skin texture and pores, age-appropriate natural lines, detailed individual hair strands, natural fabric folds and faithful garment materials, crisp high detail without plastic smoothing.
Lighting/mood: soft even large-softbox studio lighting, neutral white balance, gentle shadow definition, realistic commercial photography.
Constraints: exactly one isolated target character per panel and exactly six panels; the same person and same outfit in every panel; infer unseen side and back details conservatively from confirmed information; no other people; no props; no source-video scene; no extra limbs; no duplicated figures within a panel; no text, labels, numbers, arrows, logos, borders, watermarks or decorative graphics.
```

角色性别不明确或用户没有确认时，使用中性称谓和外观描述，不自行补充。

## 文件校验

使用图片工具读取宽高并计算 `width / height`。目标比例为 `4 / 3`，允许由像素取整产生不超过 `0.01` 的误差。确认文件能正常打开，最终文件位于工作区的 `角色卡/` 目录，而不是只存在于默认生成目录。

## 视觉校验清单

- 恰好六个画面，排列顺序正确；
- 上排是正面、45 度、严格 90 度脸部特写；
- 下排是全身正面、侧面、背面，头顶和鞋底完整；
- 六个画面为同一人物，年龄感、脸型、五官比例、发型稳定；
- 服装层级、颜色、长度、材质、鞋子和配饰一致；
- 白色摄影棚干净，光线柔和均匀；
- 手指、四肢、耳朵、发际线和衣服结构自然；
- 没有其他人物、医院或原视频场景、道具、文字、Logo、水印；
- 背面与侧面只做保守延展，没有新增醒目设计。

## 单点迭代写法

只描述当前最重要的修改，并重申不变量：

```text
Change only [one problem]. Keep the same character identity, apparent age, facial proportions, hairstyle, body proportions, wardrobe, six-panel layout, white studio background and all other views unchanged. [Precise correction]. No text, logos or watermark.
```

常见问题对应修改：

- 90 度侧脸不足：要求鼻梁、嘴唇和下巴形成完整轮廓，只显示一侧眼睛。
- 身份漂移：指定 Image 1、Image 2 为唯一面部身份锚点，移除低质量人脸参考。
- 背面换装：重申外套长度、领型、面料、裤型和鞋型与正面一致。
- 全身裁脚：降低下排人物比例，保留头顶和鞋底之外的白色留边。
- 面部过度磨皮：要求可见毛孔、自然纹理与符合年龄的细纹。
