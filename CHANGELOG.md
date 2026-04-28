### v1.2.6

**🔧 优化**

* 统一 strength_hint 指引：补画与手动取图现在共用 `_build_strength_hint()` 函数，措辞完全一致
* 补画参考图编号：描述格式从 `{desc}\n指引：{guide}` 改为 `参考图{N}描述：{desc}\n\n{hint}\n\n这张参考图的序号为{N}`，与手动取图格式对齐
* main.py 复用公共函数，删除硬编码的 strength_hint 逻辑

---

### v1.2.5

**🐛 Bug 修复**

* 露脸规则提升为最高优先级：当参考图描述包含挡脸/遮脸姿势时，LLM 必须改为面部完整朝向镜头的替代姿势，此规则覆盖一切指引
* 搜图匹配不上时不再浪费额度：为缺失参考图的查询生成"无参考图自行发挥"描述，确保 N 个额度生成 N 张图
* 补画对话模型改为可配置：WebUI 新增"补画对话模型"下拉选择器，留空则回退到会话/框架默认模型

**🔧 优化**

* `_get_default_chat_provider_id` 重命名为 `_get_chat_provider_id`，优先读取用户配置的模型

---

### v1.2.4

**🐛 Bug 修复**

* 修复补画流程 `total_batch` 变量名拼写错误（应为 `total_batches`），导致 Round2 成功返回提示词后抛出 `NameError`，补画流程中断
* 修复 `daily_selfie.py` 使用标准库 `logging.getLogger()` 导致日志在 Docker 环境不可见，改用 `from astrbot.api import logger`（loguru）
* 修复补画流程第二轮 LLM 返回空文本时静默失败，增加降级重试（去掉可能触发内容安全过滤的 `_SKILL_RULES_SYSTEM_PROMPT` 后重新调用）
* 修复 `_generate_daily_selfie_image` 调用 `edit.edit()` 时未传 `default_output=""`，导致补画时 `features.edit.default_output`（默认4K）覆盖后端 `default_edit_size` 的问题
* 增加 tool_call 检测和 LLM 空响应诊断日志

**🔧 优化**

* 补画画图从串行改为并发：每隔5秒发一个请求，不等前一张画完，用 `asyncio.gather` 收集结果
* 取消补画发送图片给用户的功能，补画结果仅静默存入衣橱
* LLM 调用增加超时保护（120s）、画图增加超时保护（300s）
* `/补画` 命令支持只处理当前人格

---

### v1.2.3
