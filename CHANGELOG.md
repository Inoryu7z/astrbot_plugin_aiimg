### v1.1.6

**🛡️ Tool Result 增加 send_message_to_user 禁令三明治强调**

* 修改 `_build_llm_tool_text_desc_result`：在 text 模式返回文本首尾加入 `[IMPORTANT]` 标签强调，提醒 LLM 图片已自动发送、严禁使用 `send_message_to_user` 发送图片。
* 修改 `_build_llm_tool_image_result`：在 image 模式中，`ImageContent` 前后各插入一条 `TextContent`，形成三明治结构强化禁令。
* 解决 LLM 在工具调用返回后"忘记"工具描述中禁令、违规调用 `send_message_to_user` 重复发送图片的问题。

---

### v1.1.5

**🐛 LLM 工具调用失败时返回错误信息给对话模型**

* 修复生图/改图/视频工具失败时 LLM 无法感知的问题：之前工具失败返回 `None`，对话模型完全不知道图片生成失败。
* 新增 `_build_llm_tool_failure_result` 方法，在工具失败时返回包含错误原因的 `CallToolResult`，使 LLM 能获知失败原因并告知用户。
* 覆盖所有失败场景：功能未启用、请求频率限制、并发限制、图片缺失、生成异常（如敏感内容检测）、图片发送失败等。
* 保留原有的 emoji/戳一戳用户反馈机制不变。

---

### v1.1.4

**🖼️ OpenAI Full URL 后端多图支持优化**

* 修改 `edit` 方法的默认 `edit_mode` 行为，所有 OpenAI 兼容提供商默认使用 `json_image_array` 模式传递多图。
* 之前非火山引擎 ARK 的提供商会将多张图片拼接成一张发送，现在统一改为以数组形式传递。
* 豆包（火山引擎 ARK）行为不受影响，原本就使用 `json_image_array` 模式。

---

### v1.1.3

**📝 LLM 工具描述优化与衣柜预览精简**

* 优化 `aiimg_generate` 工具描述，增加 `wardrobe` 边界说明。
* `aiimg_wardrobe_preview` 改为前置步骤定位，未开启时动态卸载。
* 衣柜预览返回指引精简，移除冗余的 `ref_strength` 提示逻辑。

---

### v1.1.2

**🐛 图片上下文模式修复**

* 修复 `image_context_mode=image` 时重复发送图片的问题。

---

### v1.1.1

**🛠️ Bug 修复与代码清理**

* 修复 `utils.py` 缺少 `Path` 导入的问题。
* 修复 `selfie` 正则误触发问题。
* 补充数据目录兼容性处理。
* 添加 `__init__.py`，修复模块导入问题。
* 清理死代码，优化 registry 重置关闭逻辑。

---

### v1.1.0

**👗 衣柜参考图支持上线**

* 新增 `aiimg_wardrobe_preview` LLM 工具，支持从衣柜中检索最匹配的参考图。
* 新增 `aiimg_search_wardrobe_image` 内部方法，按关键词搜索衣柜参考图。
* 衣柜参考图异常保护：搜索失败时优雅降级。
* 配置读取统一化，`extra_refs` 计数修正。
* 自拍参考照查找调试日志完善。

---

### v1.0.0

**🚀 插件 Fork 首发版本**

* 基于 `astrbot_plugin_gitee_aiimg` v4.2.18 Fork，重命名为 `astrbot_plugin_aiimg`。
* 保留原有功能：文生图、图生图/改图、Bot 自拍（参考照）、视频生成。
* 支持多服务商：Gitee AI、Gemini、Grok、Vertex AI Anonymous、OpenAI 兼容接口等。
* 支持 LLM 工具调用、指令调用、预设提示词、多 Key 轮询、失败重试与超时配置。
