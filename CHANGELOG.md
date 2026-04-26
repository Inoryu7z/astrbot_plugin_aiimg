### v1.2.3

**✨ 新功能**

* OpenAI Images 模板新增 gpt-image-2 专用配置字段：`quality`、`output_format`、`output_compression`、`moderation`，留空不传
* 新增 `default_edit_size` 字段，支持图生图/自拍使用与文生图不同的默认尺寸（如 1152x2048 即 9:16）
* `/自拍` 命令支持比例参数，用法：`/自拍 提示词 9:16`，支持 1:1/4:3/3:4/3:2/2:3/16:9/9:16

**🔧 优化**

* `default_size` 默认值从 `4096x4096` 调整为 `1024x1024`，更适合 gpt-image-2 等新模型
* OpenAI Images 模板描述更新，提示 gpt-image-2 专用字段用法
* 比例默认尺寸调整：9:16 默认 `1152x2048`、16:9 默认 `2048x1152`、1:1 默认 `1024x1024`，确保满足 gpt-image-2 最低像素要求（655,360）

---

### v1.2.2

**🐛 Bug 修复**

* 修复 `terminate()` 中未取消 `DailySelfieService` 后台任务，导致插件重载后旧任务引用已销毁的 registry 而崩溃的问题
* 移除 `terminate()` 中 `self.edit.registry = None`，避免正在运行的生图任务因 registry 被置空而失败
* 补画生图前新增 `self.edit` 和 `self.edit.registry` 状态检查，如果为 None 则安全返回而非崩溃

---

### v1.2.1

**✨ 新功能**

* 使用 `/补画` 指令触发补画时，生成的图片会自动发送给发送指令的用户（定时补画不发送）

---

### v1.2.0

**🔧 日志优化**

* 移除补画 prompt 日志的 200 字符截断，输出完整提示词
* 补画启动时新增诊断日志：输出 debug 模式状态和 selfie 配置键列表，便于排查 debug 不生效问题

---

### v1.1.9

**🐛 补画提示词双重包装修复 + 补画衣橱保存 + Debug 模式**

* 修复补画提示词双重包装：`_generate_daily_selfie_image` 不再调用 `_build_selfie_prompt`，直接使用 LLM 第二轮生成的完整提示词
* 修复补画结果未保存到衣橱：新增 `_save_to_wardrobe` 方法，补画成功后自动保存
* 新增补画 Debug 模式：`features.selfie.daily_selfie_debug` 配置项，开启后日志输出 LLM 每轮完整请求与返回

---

### v1.1.8

**🐛 补画功能代码审查修复**

* 修复 LLM 输出编号前缀未清理：新增 `_clean_llm_line()` / `_parse_llm_lines()` 工具函数，自动清理 `1. `, `- `, `• ` 等前缀
* 修复 `_save()` 同步 I/O 阻塞事件循环：新增 `_save_async()` 使用 `asyncio.to_thread()` 包装
* 修复 `/补画` 命令 task 未追踪：改为 `await` 而非 `create_task`
* 修复 `_get_recent_styles()` 日期字符串比较：改用 `datetime` 对象比较
* 修复 `_get_recent_styles()` JSON 解析：处理 dict 类型 style_raw，确保 tags 始终为 list
* 修复 prompt-batch 对齐错位：新增 `valid_refs` 列表，避免 prompt 与参考图配对错误
* 修复 `_generate_daily_selfie_image` 未传 `resolution` 参数
* 清理 `on_provider_request` 未使用钩子（edit_router.py / draw_service.py）

---

### v1.1.7

**🧠 补画 LLM 交互重构：人格注入 + 内联 Skill 规则**

* 重构补画 LLM 交互：从简单提示词改为"人格 system prompt + 拍照任务模式 + 内联 skill 规则"三层架构
* 新增人格注入：通过 `persona_manager.get_persona_v3_by_id()` 读取人格完整 system prompt，拼入 LLM 调用的 system prompt
* 内联 selfie-reference-router skill 规则：固定开头模板、自拍母规则、强制要求直接写入 system prompt，无需独立 skill 文件
* 参考强度转化为自然语言指引：衣橱返回的 ref_strength（full/reimagine/style）转为具体指引文本，避免元概念
* 第2轮 LLM 输出完整提示词（含固定开头），第1轮风格描述直接用作向量检索查询
* 去掉强制结尾"完全保留她的面部特征。"
* 修复 `llm_generate()` 缺少必需参数 `chat_provider_id`，新增 `_get_default_chat_provider_id()` 获取全局默认 provider
* 修复第2轮 LLM 无对话上下文问题，将第1轮风格描述传入第2轮用户消息

---

### v1.1.6

**🐛 补画功能审查修复**

* 修复严重Bug：全局 `on_provider_request` 回调误将所有文生图/改图请求计入补画额度，导致正常用户请求消耗补画配额。改为补画流程内直接计数。
* 修复数据库耦合：`_get_style_pool()` 和 `_get_recent_styles()` 从直接 SQL 访问衣橱数据库改为调用衣橱插件公开 API（`get_tag_distribution` / `list_images_lightweight`），移除 `aiosqlite` 依赖。
* 修复 LLM 人格上下文：系统提示词中加入人格名称，让 LLM 以该人格视角选择风格和构建提示词。
* 修复参考图搜索：`current_persona` 从硬编码空字符串改为传入实际人格名，避免搜索到当前人格自己的图片。

---

### v1.1.5

**📸 每日自动补画（薅羊毛）功能**

* 新增 `daily_selfie` 模块，支持在每日指定时间自动检查并补画未用完的免费额度。
* 新增配置项：
  - `features.selfie.daily_selfie_schedule_time` — 定时触发时间（默认 23:30）
  - `selfie_persona_1/2.daily_selfie_enabled` — 是否启用补画
  - `selfie_persona_1/2.daily_selfie_limit` — 每日额度上限
  - `selfie_persona_1/2.daily_selfie_provider_id` — 计数提供商 ID
* 新增命令：`/补画`（手动触发）、`/补画状态`（查看额度）
* 补画流程：LLM 选择风格 → 衣橱向量检索参考图 → LLM 构建提示词 → 串行生图（5s间隔）→ 静默存入衣橱
* 计数机制：通过 `on_provider_request` 回调在请求前计数，宁可多算不少算

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
