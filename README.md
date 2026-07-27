[![AiImg Counter](https://count.getloli.com/get/@Inoryu7z.aiimg?theme=miku)](https://github.com/Inoryu7z/astrbot_plugin_aiimg)

# 🎨 AiImg · 万象绘

多服务商、多模态的 AI 图像与视频生成插件，让 Bot 拥有完整的视觉创作能力。

**AiImg** 是一个统一的图像/视频生成网关，专注于让 Bot 在对话中自然地 **画图、改图、自拍、补拍、生成视频**——而不需要用户关心背后用的是哪个服务商。

---

## ✨ 它能做什么

### 🖼️ 文生图

用户只需一句话，Bot 即可生成对应图片。支持多种服务商和模型，按链路顺序自动兜底切换。

### ✏️ 改图 / 图生图

用户发送或引用图片后，Bot 可以根据提示词编辑图片——换背景、换风格、修细节，统统支持。支持改图预设（如 `/手办化`、`/动漫化`），命令末尾可带比例参数（如 `/改图 换背景 16:9`）。

### 🤳 自拍参考照

上传 Bot 的参考照后，Bot 可以"自拍"——基于参考人像生成新的图片。支持 WebUI 上传和聊天内设置两种方式。开启衣橱联动后，自拍时可自动从 [Wardrobe 衣橱](#-与-wardrobe-衣橱联动) 检索参考图。

### 🤳 补拍系统（每日定时自拍）

AiImg 的核心差异化能力。Bot 可按人格、按服务商配置每日定时自拍计划，到点自动跑一条「创意设计 → 提示词构建 → 生图 → 发空间」的完整流水线，把生成的图静默存入衣橱、并发到 QQ 空间。详见下方 [🤳 补拍系统](#-补拍系统每日定时自拍) 章节。

### 🎬 视频生成

支持 Grok（multipart 中转站 / 官方 / 级联三种）和豆包 Seedance 等视频生成后端，从图片或纯文本生成视频。支持多模型级联（同一后端按顺序尝试多个模型，失败自动切换）。

### 🧠 LLM 工具调用

所有功能均可通过 LLM 工具调用自然触发，用户无需记忆指令。Bot 会根据对话语义自动选择合适的模式。支持后台生成模式（图片在后台生成完成后再发送，期间可继续聊天）。

### 🔗 多服务商链路

配置多个服务商实例，按优先级排列。主用失败时自动切换到备用，确保生成成功率。每个服务商可单独配置 API Key 池、超时、代理、自定义 User-Agent、独立输出分辨率。

---

## 🤳 补拍系统（每日定时自拍）

补拍是 AiImg 最重度的功能，专为"让 Bot 像真人一样每天自己发发自拍、养号经营人设"而设计。它把"自拍"从一个被动指令升级为一条每日自动跑的创意流水线。

### 流水线架构（四轮 + 算法选风格）

每张补拍图会经过以下环节，每个环节的 LLM 提供商均可全局独立配置：

| 轮次 | 环节 | 职责 |
|------|------|------|
| r0 | 算法选风格 | 不调 LLM，按"近期去重 + 加权随机"策略从风格池选风格，避免 LLM 训练数据导致的风格刻板化 |
| r1 | 场景生成（LLM） | 为每套搭配生成拍摄场景 |
| r2 | 创意设计（LLM） | 设计师按"色彩哲学 / 廓形语言 / 材质情绪"三要素产出服装+外观+姿势+场景的结构化设计 |
| r3 | 服装审核（LLM） | 审核师从风格纯度 / 色彩和谐 / 材质对话 / 廓形比例 / 视觉焦点 / 单品必要性 6 维度审查，未通过直接给改进版 |
| r4 | 提示词翻译（LLM） | 把设计稿翻译成最终生图提示词 |

r0 与 r1 并行执行 → 配对 → 搜衣橱参考图 → r2 → r3 → r4 → 生图。r2/r3/r4/画图按批次错开启动，避免瞬间并发打满 provider。

### 人格级定制

每个人格（最多 3 个）可独立配置：

- **风格池**：调用 Wardrobe 的 `get_style_pool_for_persona` 获取该人格专属风格池，留空回退全局池
- **创意设计师系统提示词**：`costume_designer_system_prompt`，预填完整默认提示词，可在 WebUI 全屏编辑
- **审核师系统提示词**：`reviewer_system_prompt`，可按角色调整审核尺度（如某角色对发型约束更严格）
- **提示词工程师系统提示词**：`prompt_engineer_system_prompt`
- **自拍服务商链路**：`selfie_persona_N.chain`，每个服务商可单独设置输出分辨率
- **视频服务商链路**：`video_provider_ids`

### 服务商级额度管理

补拍额度是**服务商级共享资源**（不是人格级）。多个人格配置同一服务商时额度共享。

- `daily_selfie_providers`：每个服务商可独立设置 `daily_limit`（每日额度）和 `schedule_time`（触发时间）
- 调度优先级：服务商级 > 全局
- 手动自拍也会计入补拍额度，按实际成功的 provider 计数（而非链路第一个）
- 失败自动重试，单批次设计失败延迟 10 分钟重试，最多 2 次，23:30 跨日保护截止线

### 自动发 QQ 空间

补拍完成后可自动将图片发布到 QQ 空间说说，由多模态 LLM 看图生成第一人称日常分享配文。

- `daily_selfie_qzone_publish_enabled`：人格级开关
- `daily_selfie_qzone_chat_provider_id`：独立的多模态 LLM 提供商，与补拍对话模型分开
- 每个服务商完成后分别发布（不是所有图合并一次性发）
- 配文图片自动压缩（最长边 1024px，JPEG Q80），限制 9 张/请求

### 与衣橱联动

补拍全链路与 [Wardrobe 衣橱](#-与-wardrobe-衣橱联动) 深度联动：

- **搜参考图**：r1 后调用 `wardrobe.get_reference_image(daily_selfie_mode=True)`，启用补拍衰减过滤（近期用过的图权重降低）
- **风格池**：r0 从衣橱人格级风格池选风格
- **自动存图**：补拍生成的图静默存入衣橱对应人格目录，附带生成时所用提示词（`ai_prompt` 字段）
- **参考强度**：补拍有参考图时一律使用 `reimagine` 强度（仅借服装款式，姿势构图完全重新设计）
- **Ark Seedream 单图模式**：检测到 provider 是 `ark_seedream` 时，只传人设参考图，不传衣橱图

---

## 🎮 可用指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/aiimg [@provider_id] <提示词> [比例]` | 所有人 | 文生图（别名：`/文生图` `/生图` `/画图` `/绘图` `/出图`） |
| `/aiedit [@provider_id] <提示词> [比例]` | 所有人 | 改图（别名：`/图生图` `/改图` `/修图`），需发送/引用图片 |
| `/自拍 [@provider_id] <提示词>` | 所有人 | 自拍参考照模式 |
| `/视频 [@provider_id] <提示词>` | 所有人 | 视频生成 |
| `/补拍 [@人格名] [@提供商ID]` | 管理员 | 立即触发补拍（可限定人格或单个提供商） |
| `/补拍状态` | 管理员 | 查看当日补拍进度与各服务商额度 |
| `/补拍debug` | 管理员 | 查看当日补拍事件流（触发人格数、超时次数、延迟重试次数等） |
| `/重发图片` | 所有人 | 重发最近一次生成的图片 |
| `/自拍参考 设置` | 管理员 | 设置自拍参考照 |
| `/自拍参考 清除` | 管理员 | 清除自拍参考照 |
| `/预设列表` | 所有人 | 查看改图预设列表 |
| `/视频预设列表` | 所有人 | 查看视频预设列表 |
| `/<provider_id> <提示词>` | 所有人 | 快捷命令：直接用服务商 ID 作为命令，图片类自动转 `/aiedit`，视频类自动转 `/视频` |

预设命令会根据配置动态注册（如 `/手办化`、`/动漫化` 等）。比例参数支持 `1:1` `4:3` `3:4` `3:2` `2:3` `16:9` `9:16`，中文冒号 `16：9` 也会自动识别。

---

## 🧩 LLM 工具

| 工具名 | 说明 |
|--------|------|
| `aiimg_generate` | 统一图片生成/改图/自拍工具。参数：`prompt` / `mode`（auto/text/edit/selfie_ref）/ `backend`（auto 或指定服务商）/ `output`（尺寸如 2048x2048 或 4K）/ `use_wardrobe`（仅自拍生效，是否用衣橱参考图） |
| `aiimg_draw` | 纯文生图快捷入口 |
| `aiimg_edit` | 改图快捷入口 |
| `aiimg_video` | 视频生成。参数：`prompt` / `image_url`（图生视频时传图）/ `backend`（auto 或指定视频服务商） |
| `aiimg_wardrobe_preview` | **自拍专用**。从衣橱检索一张参考图并返回其文字描述，用于指导自拍提示词构建。不发送图片给用户，是自拍流程的预处理步骤。需开启 `features.selfie.wardrobe_ref_enabled` |

---

## ⚙️ 核心配置

### 服务商实例（providers）

在配置面板底部添加服务商实例，每个实例需要唯一的 `id`。支持的模板：

#### 图片类

| 模板 | 适用场景 |
|------|---------|
| OpenAI Images | 标准 `/v1/images/generations` 接口，适用于大多数 OpenAI 兼容网关（含 gpt-image-2） |
| OpenAI ImagesURL | 自定义完整 endpoint URL 的 OpenAI 兼容接口 |
| Ark Seedream | 字节 Ark Seedream 系列专用（改图时永不发送 `sequential_image_generation` 参数，修复 5.0 pro HTTP 400） |
| OpenAI Chat 图 | Chat 回复中返回图片的接口（含 Gemini OpenAI Chat 兼容） |
| Gemini Images | Gemini 官方 `generateContent` 接口 |
| Gitee Images | Gitee AI Images 同步接口 |
| Gitee 异步改图 | Gitee AI 异步任务接口 |
| 即梦（豆包） | jimeng 聚合接口 |

#### 视频类

| 模板 | 适用场景 |
|------|---------|
| Grok Video（multipart 中转站） | 基于 multipart/form-data 协议，适用于 PoloAI / s.apifox 等兼容接口，支持多模型级联 |
| 官方 Grok（视频） | xAI 官方 API 格式，`image` 字段为 `{"url": ...}` 对象，支持 `reference_images` 多参考图 |
| True Grok（级联） | 组合模板，配置 `fallback_chain`（最多 3 个），按顺序尝试，失败自动切换。典型用法：真 Grok（便宜/不稳定）→ 假 Grok（贵/稳定） |
| 豆包 Seedance | 豆包异步任务视频生成 |

每个图片/视频服务商均可单独配置：`api_keys`（Key 池轮询）、`timeout`、`user_agent`（自定义 UA 绕过中转站拦截）、`default_output`（独立输出分辨率）等。

### 功能链路（features）

| 配置项 | 说明 |
|--------|------|
| `features.draw.chain` | 文生图链路 |
| `features.edit.chain` | 改图链路 |
| `features.selfie.chain` | 自拍链路（留空可复用改图链路） |
| `features.selfie.wardrobe_ref_enabled` | 自拍时是否启用衣橱参考图检索 |
| `features.video.chain` | 视频链路 |

链路按顺序兜底：主用失败自动切换到下一个 provider。

### LLM 工具行为

| 配置项 | 说明 |
|--------|------|
| `llm_tool.image_context_mode` | 图片生成后返回给 LLM 上下文的方式：`image`（压缩图）/ `text`（提示词文字描述）/ `none`（不返回） |
| `llm_tool.background_generate` | 后台生成模式开关（默认开启）。开启时图片在后台生成完成后自动发送，期间可继续聊天；关闭后恢复阻塞模式，LLM 可在上下文中看到图片 |

### 补拍配置

补拍配置分两层：全局 + 人格级（最多 3 个人格）。

**全局**

| 配置项 | 说明 |
|--------|------|
| `daily_selfie_enabled` | 补拍总开关 |
| `daily_selfie_schedule_time` | 全局默认触发时间（如 `23:00`），服务商级 / 人格级可覆盖 |
| `daily_selfie_retry_on_fail` | 失败自动重试开关（默认开启） |
| `daily_selfie_chat_provider_id` | 补拍对话模型默认提供商 |
| `daily_selfie_scene_provider_id` | r1 场景生成提供商（留空回退 chat） |
| `daily_selfie_designer_provider_id` | r2 设计师提供商（留空回退 chat） |
| `daily_selfie_reviewer_provider_id` | r3 审核师提供商（留空回退 chat） |
| `daily_selfie_prompt_engineer_provider_id` | r4 提示词翻译提供商（留空回退 chat） |
| `daily_selfie_ref_min_similarity` | 补拍搜参考图的向量相似度阈值，设为 0 用全局阈值，0.6~0.7 可增加"无参考图自由发挥"比例 |

**人格级（`selfie_persona_1/2/3`）**

| 配置项 | 说明 |
|--------|------|
| `persona_name` | 人格规范名（与 Wardrobe 人格配置一致） |
| `chain` | 自拍服务商链路（template_list，每个服务商可独立设置 output） |
| `video_provider_ids` | 视频服务商链路 |
| `costume_designer_system_prompt` | r2 设计师系统提示词（留空回退默认） |
| `reviewer_system_prompt` | r3 审核师系统提示词（留空回退默认） |
| `prompt_engineer_system_prompt` | r4 提示词工程师系统提示词（留空回退默认） |
| `daily_selfie_qzone_publish_enabled` | 该人格补拍完成后是否自动发 QQ 空间 |
| `daily_selfie_qzone_chat_provider_id` | 空间配文生成模型（多模态） |

**服务商级（`daily_selfie_providers` 每项）**

| 配置项 | 说明 |
|--------|------|
| `provider_id` | 服务商 ID |
| `daily_limit` | 每日额度（多人格共享同一服务商时额度共享） |
| `schedule_time` | 该服务商独立触发时间（留空回退全局） |

---

## 🔗 与 Wardrobe 衣橱联动

AiImg 与 [Wardrobe 衣橱图鉴](https://github.com/Inoryu7z/astrbot_plugin_wardrobe) 是一套双子插件，单向安装任一方都能用，但两者一起安装时会自动启用深度联动。联动方向有四个：

### ① 衣橱 → AiImg：自拍参考图（`get_reference_image`）

LLM 自拍时可调用 `aiimg_wardrobe_preview` 工具，从衣橱检索一张参考图并返回其文字描述，用于指导提示词构建。生图时 AiImg 调用 `wardrobe.get_reference_image()` 拿到参考图文件路径 + 描述 + 参考强度（`full` / `style` / `reimagine`）。
- 参考强度由衣橱存图时自动评估，决定参考图对生成图的指导力度
- 自动排除当前人格的图库，避免同质化
- 支持 `min_similarity` 参数收紧搜图条件

### ② 衣橱 → AiImg：补拍风格池（`get_style_pool_for_persona`）

补拍 r0 算法选风格时，优先调用衣橱的人格级风格池。每个人格可有自己的专属风格列表，留空回退衣橱全局风格池。

### ③ 衣橱 → AiImg：补拍衰减过滤（`daily_selfie_use_count`）

补拍搜参考图时传入 `daily_selfie_mode=True`，衣橱对近期被补拍选中的图做指数衰减（0.6^n，权重低于 0.05 直接排除），避免短期内重复用同一张参考图。每周一凌晨 4 点所有图的计数减 1。仅影响补拍，手动自拍和手动取图不受影响。

### ④ AiImg → 衣橱：自动存图

开启 `auto_save_aiimg_enabled`（在 Wardrobe 侧配置）后，AiImg 生成的**自拍模式**图片会自动存入衣橱对应人格目录：
- 仅自拍模式（`/自拍` 命令及 LLM `aiimg_generate` 的 selfie 路径）自动存入，文生图/改图不存
- 自动调用衣橱视觉模型分析图片属性
- 自拍模式下生成时所用提示词会写入衣橱的 `ai_prompt` 字段，WebUI 详情页可查看
- 自动存视频：AiImg 生成的视频也会自动存入衣橱视频库

---

## 📝 使用说明

1. 必须先配置至少一个 provider 实例，否则所有功能不可用。
2. 链路为空时插件会提示去 WebUI 补配置。
3. `@provider_id` 仅临时指定一次使用哪个 provider，不改变默认链路。
4. 网关是否支持某个接口取决于服务商实现，插件会自动兜底。
5. `image_context_mode` 默认为 `image`，LLM 会收到压缩图并基于图片内容回复；设为 `text` 则 LLM 收到提示词文字描述；设为 `none` 则 LLM 不知道图片已发送。
6. 补拍功能需要同时安装 Wardrobe 衣橱插件才能发挥完整能力（参考图、风格池、衰减过滤、自动存图）。
7. 补拍自动发空间需要安装 [QZone 插件](https://github.com/Inoryu7z/astrbot_plugin_qzone_Inoryu7z)。

---

## 🙏 致谢

本项目 fork 自 [astrbot_plugin_gitee_aiimg](https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg)，由 **木有知** 和 **Zhalslar** 开发。

感谢原作者的优秀工作，为本项目奠定了坚实的基础。

---

## 📜 开源协议

本项目基于 [MIT License](LICENSE) 开源。

原项目 [astrbot_plugin_gitee_aiimg](https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg) 未附带明确的开源协议声明，本 fork 在此基础上以 MIT 协议发布，并保留原作者的版权声明。
