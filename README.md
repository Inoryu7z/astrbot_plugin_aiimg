[![AiImg Counter](https://count.getloli.com/get/@Inoryu7z.aiimg?theme=miku)](https://github.com/Inoryu7z/astrbot_plugin_aiimg)

# 🎨 AiImg · 万象绘

多服务商的 AI 图像与视频生成插件，让 Bot 拥有完整的视觉创作能力。

**AiImg** 是一个统一的图像/视频生成网关，专注于让 Bot 在对话中自然地 **画图、改图、自拍、补拍、生成视频**——而不需要用户关心背后用的是哪个服务商。

---

## ✨ 功能概览

### 🖼️ 文生图 / 改图

一句话生成图片，或基于发送/引用的图片改图（换背景、换风格、修细节）。支持改图预设（`/手办化`、`/动漫化` 等），命令末尾可带比例参数。多服务商链路按顺序自动兜底切换。

### 🤳 自拍与补拍

- **自拍**：上传 Bot 参考照后，Bot 可基于参考人像生成自拍。开启衣橱联动后，自拍时自动从 [Wardrobe 衣橱](https://github.com/Inoryu7z/astrbot_plugin_wardrobe) 检索参考图
- **补拍**：按人格、按服务商配置每日定时自拍计划，到点自动跑一条「创意设计 → 提示词构建 → 生图 → 发空间」的流水线，把图静默存入衣橱、并发到 QQ 空间。详见下方 [🤳 补拍系统](#-补拍系统) 章节

### 🎬 视频生成

支持 Grok（multipart 中转站 / 官方 / 级联三种）和豆包 Seedance 等视频生成后端，从图片或纯文本生成视频。支持多模型级联（同一后端按顺序尝试多个模型，失败自动切换）。

### 🧠 LLM 工具调用

所有功能均可通过 LLM 工具自然触发，用户无需记忆指令。支持后台生成模式（图片在后台生成完成后再发送，期间可继续聊天）。

---

## 🤳 补拍系统

让 Bot 像真人一样每天自己发发自拍、养号经营人设。每张补拍图会经过五环节流水线打磨，每个环节的 LLM 提供商均可独立配置：

| 环节 | 职责 |
|------|------|
| 算法选风格 | 从风格池加权随机选风格，避免 LLM 反复挑同一个风格 |
| 场景生成 | LLM 为每套搭配生成拍摄场景 |
| 创意设计 | LLM 设计师按色彩 / 廓形 / 材质三要素产出结构化设计 |
| 服装审核 | LLM 审核师多维度审查，未通过直接给改进版 |
| 提示词翻译 | 把设计稿翻译成最终生图提示词 |

**人格级定制**：每个人格（最多 3 个）可独立配置风格池、三段系统提示词（设计师/审核师/提示词工程师）、自拍与视频服务商链路。

**额度与调度**：每个服务商可独立设置每日额度和触发时间，失败自动重试。

**自动发 QQ 空间**：补拍完成后可自动发布到 QQ 空间，由多模态 LLM 看图生成第一人称日常分享配文。需安装 [QZone 插件](https://github.com/Inoryu7z/astrbot_plugin_qzone_Inoryu7z)。

**与衣橱联动**：搜参考图（含衰减过滤，避免近期重复）、人格级风格池、自动存图（含生成时所用提示词）。需同时安装 [Wardrobe 衣橱](https://github.com/Inoryu7z/astrbot_plugin_wardrobe)。

---

## 🎮 可用指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/aiimg [@provider_id] <提示词> [比例]` | 所有人 | 文生图（别名：`/文生图` `/生图` `/画图` `/绘图` `/出图`） |
| `/aiedit [@provider_id] <提示词> [比例]` | 所有人 | 改图（别名：`/图生图` `/改图` `/修图`） |
| `/自拍 [@provider_id] <提示词>` | 所有人 | 自拍参考照模式 |
| `/视频 [@provider_id] <提示词>` | 所有人 | 视频生成 |
| `/补拍 [@人格名] [@提供商ID]` | 管理员 | 立即触发补拍（可限定人格或单个提供商） |
| `/补拍状态` | 管理员 | 查看当日补拍进度与各服务商额度 |
| `/补拍debug` | 管理员 | 查看当日补拍事件流 |
| `/重发图片` | 所有人 | 重发最近一次生成的图片 |
| `/自拍参考 设置/清除` | 管理员 | 设置/清除自拍参考照 |
| `/预设列表` / `/视频预设列表` | 所有人 | 查看预设列表 |
| `/<provider_id> <提示词>` | 所有人 | 快捷命令：直接用服务商 ID 作为命令 |

预设命令会根据配置动态注册。比例参数支持 `1:1` `4:3` `3:4` `3:2` `2:3` `16:9` `9:16`，中文冒号 `16：9` 也会自动识别。

---

## 🧩 LLM 工具

| 工具名 | 说明 |
|--------|------|
| `aiimg_generate` | 统一图片生成/改图/自拍。参数：`prompt` / `mode`（auto/text/edit/selfie_ref）/ `backend` / `output` / `use_wardrobe`（仅自拍，是否用衣橱参考图） |
| `aiimg_draw` | 纯文生图快捷入口 |
| `aiimg_edit` | 改图快捷入口 |
| `aiimg_video` | 视频生成。参数：`prompt` / `image_url` / `backend` |
| `aiimg_wardrobe_preview` | 自拍专用：从衣橱检索参考图并返回文字描述，用于指导提示词构建。需开启 `features.selfie.wardrobe_ref_enabled` |

---

## ⚙️ 服务商模板

在配置面板底部添加服务商实例，每个实例需要唯一的 `id`。

**图片类**：OpenAI Images、OpenAI ImagesURL、Ark Seedream（Seedream 5.0 pro 专用，修复该模型改图 HTTP 400）、OpenAI Chat 图、Gemini Images、Gitee Images、Gitee 异步改图、即梦（豆包）

**视频类**：Grok Video（multipart 中转站）、官方 Grok（视频）、True Grok（级联，最多 3 个 fallback）、豆包 Seedance

每个服务商均可单独配置 API Key 池、超时、代理、自定义 User-Agent、独立输出分辨率等。功能链路（文生图/改图/自拍/视频）按顺序兜底，主用失败自动切换。

---

## 📝 使用说明

1. 必须先配置至少一个服务商实例，否则所有功能不可用。
2. `@provider_id` 仅临时指定一次使用哪个服务商，不改变默认链路。
3. `image_context_mode` 控制图片生成后返回给 LLM 上下文的方式：`image`（压缩图）/ `text`（提示词描述）/ `none`。
4. 补拍功能需要同时安装 Wardrobe 衣橱插件才能发挥完整能力。
5. 详细配置项请在 AstrBot WebUI 配置面板中查看，每个字段都有说明文字。

---

## 🙏 致谢

本项目 fork 自 [astrbot_plugin_gitee_aiimg](https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg)，由 **木有知** 和 **Zhalslar** 开发。

感谢原作者的优秀工作，为本项目奠定了坚实的基础。

---

## 📜 开源协议

本项目基于 [MIT License](LICENSE) 开源。

原项目 [astrbot_plugin_gitee_aiimg](https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg) 未附带明确的开源协议声明，本 fork 在此基础上以 MIT 协议发布，并保留原作者的版权声明。
