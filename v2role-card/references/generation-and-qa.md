# 角色卡生成与校验

## 参考图分工

每个角色使用 3–6 张互补参考图，并在提示词中逐张标注用途：

- Image 1：主要正面身份参考；
- Image 2：45 度或侧脸身份参考；
- Image 3：服装、体态与鞋子参考；
- Image 4..N：发型、配饰或遮挡补充参考。

优先使用清晰、自然、无大面积字幕或广告遮挡的画面。辅助全身帧含有其他人物时，明确写出“只参考目标角色，忽略并排除其他人物”。

身份参考与风格参考默认使用同一组视频帧。如果某些帧来自广告、片头、转场或画中画，只用于它明确承担的参考作用，不让其覆盖用户已经确认的角色风格。

## 风格继承原则

把用户确认的风格拆成可观察的视觉属性写入提示词，而不是依赖单一标签：

- 保持媒介或渲染方式一致；
- 保持人物比例、五官概括方式和轮廓语言一致；
- 保持线条、边缘、阴影层级、材质与纹理一致；
- 保持色彩、对比度、色温、镜头或光照语言一致；
- 只把背景整理为干净、明亮、无缝的纯白角色档案背景，不把角色本身转换为另一种媒介。

真人视频可以使用摄影、皮肤、毛孔、镜头和布光术语；二维动画或漫画使用线稿、平涂、网点、笔触和阴影层级术语；三维内容使用建模、材质、着色与渲染术语；像素内容明确像素尺寸观感、有限色板和硬边，不引入抗锯齿或摄影质感。其他风格按相同原则描述其实际可见属性。

## 生成提示词模板

将方括号内容替换为用户确认后的事实，不补写未经确认的角色设定。

```text
Use case: identity-preserve
Asset type: 16:9 landscape character reference sheet for production, rendered in the confirmed source-video style
Input images: Image 1 is the primary front facial-identity and style reference. Image 2 is the complementary facial-angle and style reference. Image 3 is the body-proportion, wardrobe and footwear reference. [Describe additional images, their exact roles, and explicitly exclude other people or packaging visuals visible in them.]
Primary request: Create one polished 16:9 single-row character reference board containing exactly four consistent views of the SAME character from the references: one large front-facing head-and-shoulders close-up on the left, followed by full-body front, exact 90-degree full-body side, and full-body back views on the right.
Subject identity: [confirmed visual age, build, facial structure, face/surface treatment, hairstyle and stable identity anchors]. Preserve the recognizable facial structure, apparent age, hairstyle and style-appropriate body proportions across every panel.
Wardrobe invariants: [confirmed outerwear, inner layer, trousers/skirt, shoes and accessories]. Keep the exact same clothing, colors, materials and accessories in all four views.
Confirmed source-video style: [confirmed medium/rendering method; character proportions and shape language; line/edge treatment; shading, materials and texture; color, contrast and temperature; camera/lighting language]. Match these observable style properties faithfully in all four views. Do not convert the character to photorealism or any other medium unless that is the confirmed source-video style.
Scene/backdrop: one shared clean, bright, seamless pure-white background rendered compatibly with the confirmed style; soft, even, clear high-key presentation lighting; no original scene background, borders, divider lines, card panels, labels or floor clutter.
Layout: strict 16:9 landscape canvas with one continuous horizontal row. The left close-up region occupies about 45% of the width; place a large front-facing head-and-shoulders close-up at its visual center, looking directly at the viewer, framed from the complete top of the head to the shoulders or upper chest. The right region occupies about 55% and contains, from left to right, full-body front, exact 90-degree full-body side, and full-body back views. Show every full-body view completely from head to soles. Keep the three full-body figures at the same scale, top-of-head height and standing baseline, with even spacing, no overlap and sufficient white margin.
Pose/expression: natural relaxed expression in the close-up, no exaggerated emotion. In all full-body views, stand naturally upright with arms relaxed straight down; accurate anatomy or character-design structure for the confirmed style.
Style-specific detail: [translate the confirmed style into concrete detail requirements appropriate to its medium, such as natural skin and fabric for live action, consistent linework and cel shading for 2D, faithful geometry/materials/rendering for 3D, visible brushwork for painting, or hard-edged limited-palette pixels for pixel art]. Preserve the source video's intended finish while removing only incidental compression artifacts.
Lighting/mood: translate [confirmed light/shadow treatment] into soft, even, clear high-key presentation lighting so every view remains readable; preserve the source medium and rendering language while removing directional scene light and heavy cast shadows.
Constraints: exactly four views of one target character: one large front head-and-shoulders close-up plus three full-body views; no extra face angles or additional figures. Keep the same person, outfit and confirmed source-video style in every view; infer unseen side and back details conservatively from confirmed information; no other people; no props; no source-video scene; no extra limbs or broken character structure; no text, labels, numbers, arrows, logos, borders, watermarks, subtitles, interface elements or decorative graphics.
```

角色性别不明确或用户没有确认时，使用中性称谓和外观描述，不自行补充。

## 文件校验

使用图片工具读取宽高并计算 `width / height`。目标比例为 `16 / 9`，允许由像素取整产生不超过 `0.01` 的误差。确认文件能正常打开，最终文件位于工作区的 `角色卡/` 目录，而不是只存在于默认生成目录。

## 视觉校验清单

- 恰好四个视图且全部位于同一横向单行：左侧正面头肩大特写，右侧依次为全身正面、严格 90 度侧面、背面；
- 左侧约占 45%，右侧约占 55%；特写完整保留头顶、头发轮廓和肩颈，人物直视镜头；
- 三个全身视图头顶和鞋底完整，缩放比例、头顶高度和站立基线一致，间距均匀且互不遮挡；
- 四个视图为同一人物，年龄感、脸型、五官比例、发型稳定；
- 服装层级、颜色、长度、材质、鞋子和配饰一致；
- 四个视图的媒介、人物比例、造型语言、线条或边缘、色彩、明暗、材质和纹理与用户确认的视频风格一致；
- 四个视图共享纯白无缝背景和清晰柔和的高调光影，不出现边框、分隔线或独立卡片底板；
- 手指、四肢、耳朵、发际线和衣服结构符合该风格的设计逻辑；
- 没有其他人物、原视频场景、道具、文字、Logo、水印；
- 背面与侧面只做保守延展，没有新增醒目设计。

## 单点迭代写法

只描述当前最重要的修改，并重申不变量：

```text
Change only [one problem]. Keep the same character identity, apparent age, facial proportions, hairstyle, body proportions, wardrobe, confirmed source-video style, 16:9 single-row four-view layout, pure-white shared background and all other views unchanged. [Precise correction]. No text, logos or watermark.
```

常见问题对应修改：

- 全身侧面不足 90 度：要求头部、肩部、躯干、髋部和双脚都呈严格侧向轮廓，不出现三分之二角度。
- 身份漂移：指定 Image 1、Image 2 为唯一面部身份锚点，移除低质量人脸参考。
- 背面换装：重申外套长度、领型、面料、裤型和鞋型与正面一致。
- 全身裁脚：同时降低右侧三个全身人物的比例，继续保持等高、同基线，并在头顶和鞋底外保留白色留边。
- 真人面部过度磨皮：要求可见毛孔、自然纹理与符合年龄的细纹。
- 二维风格漂移为写实：重申线条、五官概括、平涂/阴影层级和原视频色彩，不使用皮肤毛孔或摄影术语。
- 三维风格漂移：重申角色建模比例、材质、着色器、渲染和原视频光照，不转换为真人摄影或二维插画。
- 其他风格漂移：从确认描述中选出偏差最大的一个视觉属性单点修正，不用宽泛风格标签替代可观察属性。
