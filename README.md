[![AiImg Counter](https://count.getloli.com/get/@Inoryu7z.aiimg?theme=miku)](https://github.com/Inoryu7z/astrbot_plugin_aiimg)

# 🎨 AiImg · 万象绘

多服务商、多模态的 AI 图像与视频生成插件，让 Bot 拥有完整的视觉创作能力。

**AiImg** 是一个统一的图像/视频生成网关，专注于让 Bot 在对话中自然地 **画图、改图、自拍、生成视频**——而不需要用户关心背后用的是哪个服务商。

---

## ✨ 它能做什么

### 🖼️ 文生图

用户只需一句话，Bot 即可生成对应图片。支持多种服务商和模型，按链路顺序自动兜底切换。

### ✏️ 改图 / 图生图

用户发送或引用图片后，Bot 可以根据提示词编辑图片——换背景、换风格、修细节，统统支持。

### 🤳 自拍参考照

上传 Bot 的参考照后，Bot 可以"自拍"——基于参考人像生成新的图片。支持 WebUI 上传和聊天内设置两种方式。

### 🎬 视频生成

支持 Grok 和豆包 Seedance 等视频生成后端，从图片或纯文本生成视频。

### 🧠 LLM 工具调用

所有功能均可通过 LLM 工具调用自然触发，用户无需记忆指令。Bot 会根据对话语义自动选择合适的模式。

### 🔗 多服务商链路

配置多个服务商实例，按优先级排列。主用失败时自动切换到备用，确保生成成功率。

---

## 🎮 可用指令

| 指令 | 说明 |
|------|------|
| `/aiimg [@provider_id] <提示词> [比例]` | 文生图 |
| `/aiedit [@provider_id] <提示词>` | 改图（需发送/引用图片） |
| `/自拍 [@provider_id] <提示词>` | 自拍参考照模式 |
| `/视频 [@provider_id] <提示词>` | 视频生成 |
| `/重发图片` | 重发最近一次生成的图片 |
| `/自拍参考 设置` | 设置自拍参考照 |
| `/自拍参考 清除` | 清除自拍参考照 |
| `/预设列表` | 查看改图预设列表 |
| `/视频预设列表` | 查看视频预设列表 |

预设命令会根据配置动态注册（如 `/手办化`、`/动漫化` 等）。

---

## 🧩 LLM 工具

| 工具名 | 说明 |
|--------|------|
| `aiimg_generate` | 统一图片生成/改图/自拍工具（mode: auto/text/edit/selfie_ref） |
| `aiimg_draw` | 纯文生图快捷入口 |
| `aiimg_edit` | 改图快捷入口 |
| `aiimg_video` | 视频生成 |

---

## ⚙️ 核心配置

### 服务商实例（providers）

在配置面板底部添加服务商实例，每个实例需要唯一的 `id`。

支持的模板：

| 模板 | 适用场景 |
|------|---------|
| Gemini 原生（generateContent） | 直连 Gemini 官方 |
| Vertex AI Anonymous | 无 Key，依赖 Google 访问 |
| Gemini OpenAI 兼容 | Gemini 的 OpenAI 兼容接口 |
| OpenAI 兼容通用（Images） | 标准 `/v1/images/generations` 接口 |
| OpenAI 兼容（Chat 出图） | Chat 回复中返回图片的接口 |
| OpenAI 兼容-完整路径 | 自定义完整 endpoint URL |
| Flow2API（Chat SSE 出图） | SSE 流式出图 |
| Grok2API | `/v1/images/generations` |
| Gitee | Gitee AI Images |
| 即梦/豆包聚合 | jimeng 聚合接口 |
| Grok 视频 | Grok 视频生成 |
| 豆包 Seedance | 豆包异步任务视频生成 |

### 功能链路（features）

| 配置项 | 说明 |
|--------|------|
| `features.draw.chain` | 文生图链路 |
| `features.edit.chain` | 改图链路 |
| `features.selfie.chain` | 自拍链路（留空可复用改图链路） |
| `features.video.chain` | 视频链路 |

链路按顺序兜底：主用失败自动切换到下一个 provider。

### LLM 工具行为

| 配置项 | 说明 |
|--------|------|
| `llm_tool.image_context_mode` | 图片生成后返回给 LLM 上下文的方式：`image`（压缩图）/ `text`（提示词文字描述）/ `none`（不返回） |

---

## 📝 使用说明

1. 必须先配置至少一个 provider 实例，否则所有功能不可用。
2. 链路为空时插件会提示去 WebUI 补配置。
3. `@provider_id` 仅临时指定一次使用哪个 provider，不改变默认链路。
4. 网关是否支持某个接口取决于服务商实现，插件会自动兜底。
5. `image_context_mode` 默认为 `image`，LLM 会收到压缩图并基于图片内容回复；设为 `text` 则 LLM 收到提示词文字描述；设为 `none` 则 LLM 不知道图片已发送。

---

## 🙏 致谢

本项目 fork 自 [astrbot_plugin_gitee_aiimg](https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg)，由 **木有知** 和 **Zhalslar** 开发。

感谢原作者的优秀工作，为本项目奠定了坚实的基础。

---

## 📜 开源协议

本项目基于 [MIT License](LICENSE) 开源。

原项目 [astrbot_plugin_gitee_aiimg](https://github.com/muyouzhi6/astrbot_plugin_gitee_aiimg) 未附带明确的开源协议声明，本 fork 在此基础上以 MIT 协议发布，并保留原作者的版权声明。
