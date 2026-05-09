### v1.5.0

**🐛 修复补拍多提供商不切换后端 + 移除人格级补拍时间 + 优化配置提示**

* 修复补拍多提供商不实际切换生图后端的 bug：`_generate_daily_selfie_image` 新增 `provider_id` 参数，填则作为 `backend` 覆盖
* 额度是提供商级共享资源，计数 key 为 `provider_id`。多个人格配置同一提供商时额度共享（如提供商总额度 15，人格 A 配 5、人格 B 配 10，则共享 15 的池）
* 移除人格级 `daily_selfie_schedule_time` 配置字段：补拍时间已改为提供商级，人格级字段无意义且造成混淆
* `_get_provider_schedule_time` 回退逻辑改为直接回退到全局时间（不再经过人格级）
* 优化 `daily_selfie_providers` 配置 hint：明确说明每个字段的含义和额度共享规则

### v1.4.9

**🐛 修复：补拍多提供商不实际切换生图后端**

* 修复 `_generate_one_selfie` 接收预留的 `provider_id` 但从未传递给 `_generate_daily_selfie_image` 的 bug
* 根因：多提供商额度计数正确，但实际生图始终走人格 `provider_ids` 链路，`daily_selfie_providers` 的各提供商仅做额度计数而未用于实际后端
* `_generate_daily_selfie_image` 新增 `provider_id` 参数：填则作为 `backend` 覆盖（仅用该提供商），不填则走原人格链路
* 重试路径同步修复，确保重试时也使用对应提供商后端

### v1.4.8

**🐛 修复：后台生成模式下 LLM 自拍不计入补画额度**

* 修复 `background_generate=True`（默认）时，LLM 自拍不消耗每日补画额度的 bug
* 根因：额度追踪代码仅在 `_finalize_llm_tool_image`（前台模式）中执行，`_async_llm_tool_generate`（后台模式）路径完全遗漏
* 提取 `_track_selfie_quota` 方法，在两条路径统一调用
* 移除 `_generate_one_selfie` 中从未使用的 `daily_limit` 参数

### v1.4.7

🔧 优化：补拍调度时间改为提供商级 + 按提供商分别发布空间

* `daily_selfie_providers` 每项新增 `schedule_time` 字段：每个提供商可独立设置补拍触发时间
* 调度优先级：提供商级 > 人格级 > 全局设置，留空则回退到下一级
* cron 循环改为按提供商粒度匹配时间，同一人格的不同提供商可在不同时间分别触发
* 发空间改为每个提供商完成后分别发布：不再是所有提供商图片合并一次性发布，而是每个提供商完成后独立生成配文并发布
* `/补画状态` 输出增加每个提供商的调度时间显示

### v1.4.6

 修复：补拍发空间配文图片临时文件名冲突

* 修复 `_generate_qzone_caption` 临时文件名仅用 `persona_name+stem`，同名图片或连续补拍时文件被覆盖导致 LLM 配文图片错乱，现加入 uuid 确保唯一性

### v1.4.5

 修复：补拍发空间多项稳定性问题

* 修复 LLM 配文图片数量超限（[:9][:8]，API 限制8张/请求）
* 修复图片传 bytes 导致 qzone Post 模型 Pydantic 校验失败，改用 file:/// URL 字符串传入
* 修复配文生成失败时图片完全不发，现用日期作为回退配文继续发布
* 修复 Qzone 图片数量无上限可能被 API 拒绝，限制为9张
* 修复临时压缩图片文件未清理导致堆积
* 修复重试成功的图片未加入 success_paths 导致不发空间
* 修复 task-to-prompt 映射错误导致重试时使用错误的提示词
* 修复 _publish_to_qzone 在重试前调用导致重试成功的图不发布

### v1.4.4

 修复：补拍指引引用内部技能名称 + 空间配文超限/超时

* 补拍参考图指引改用专用常量 _DAILY_SELFIE_REF_HINT，不再引用「无图流程C」等 LLM 无法理解的内部技能名称
* 空间配文生成前压缩图片（最长边1024px，JPEG Q80），避免多图 payload 超 25MB API 限制
* 空间配文生成超时从 120s 增至 600s（10分钟），首次失败后自动重试一次

### v1.4.3

🔧 修复：视频后端 WriteTimeout

* 所有视频后端 write timeout 统一增大至 120s（原 30s/10s），解决大图 Base64 data URL 上传超时导致提供商后台收不到请求的问题
* 覆盖 DoubaoSeedance(豆包)、RealGrok(真Grok)、FakeGrok(假Grok)、Grok2Api 四种后端，确保 TrueGrok 级联 fallback 全路径覆盖

### v1.4.2

**🐛 重要修复：LLM自拍不计入补画额度**

* 修复 aiimg_generate(mode=selfie_ref) 生成的自拍不计入 DailyQuotaCounter 的恶性 bug
* 根因：早期 on_provider_request 回调被删除后，LLM自拍路径遗漏了 counter.increment()
* 修复 _finalize_llm_tool_image 中 mode 变量被覆盖的问题（生成模式 vs 上下文模式）

**🐛 修复 wardrobe_preview 返回文本双句号**

* result_text 末尾句号与 _build_llm_tool_text_desc_result 追加的句号拼接成双句号
* 优化指引文字：加 skill 调用提醒，序号改为参考图

### v1.4.1

**🐛 修复：生图工具能看到视频后端名称**

* provider_labels() 新增 kind 参数，支持按 image/video 过滤
* _inject_provider_list_to_tool_doc 分别注入：生图工具只看 image 后端，视频工具只看 video 后端
* 修复 LLM 用 video 后端名调用 aiimg_generate 的错误

### v1.4.0

**🐛 重要修复：LLM引用图片生成视频全部失败**

* 修复 _async_generate_video 中 image_url 为非远程链接时未转为 data URL 的 bug
* 当 LLM 传入本地路径作为 image_url 时，能读取 bytes 但路径传给 API 服务器导致失败
* RealGrok: 「图片上传失败」/ FakeGrok: 「illegal base64 data」
* 修复方案：非 http/https 的 URL 自动从 image_bytes 构建 data URL
* 双层防御：入口层(_async_generate_video) + 后端层(各 service)
* 优化压缩函数：压缩后比原始大时返回原始，避免无谓的体积膨胀

### v1.3.9

**🚀 新功能：自拍可禁用衣橱参考图**

* aiimg_generate 新增 use_wardrobe 参数：LLM 可控制是否使用衣橱参考图
* 默认 true（保持现有行为），用户说「不用衣橱」时 LLM 可设为 false
* 工具描述明确：除非用户明确要求不用，否则永远填 true

**📝 优化：LLM 返回文本提醒**

* 图片生成返回文本加「无需调用 send_message_to_user」提醒
* 防止 LLM 在图片已自动发送后重复调用 send 工具

### v1.3.8

**🚀 新功能：TrueGrok 级联模板**

* 新增 TrueGrok 组合模板：配置 fallback_chain 列表（最多3个），按顺序尝试，失败自动切换
* 典型用法：真Grok(便宜/不稳定) → 假Grok(贵/稳定)，省钱同时保成功
* 防循环引用：自动跳过链中的自身 provider_id
* 详细日志记录每个子后端的成败

### v1.3.7

**🚀 新功能：provider_id 快捷命令**

* 支持 /provider_id prompt 格式：直接用服务商ID作为命令，自动路由到对应功能
* 图片类 provider → 自动转为 /aiedit @provider_id（图生图/改图）
* 视频类 provider → 自动转为 /视频 @provider_id（视频生成）
* 原有命令（/aiedit、/视频 等）不受影响，完全共存
* 基于高优先级消息拦截器（priority=100）透明翻译，零侵入现有逻辑

### v1.3.6

**🚀 新功能：LLM 可指定视频后端**

* aiimg_video 工具新增 backend 参数：LLM 调用时可指定用哪个视频服务商生成视频（填显示名称或 provider_id 均可）
* 解析逻辑复用 resolve_backend，行为与 aiimg_generate 的 backend 参数完全一致
* backend=auto（默认）时走全局 video.chain，不影响现有行为

### v1.3.5

**🚀 新功能：假Grok 视频提供商（grok-video-3）**

* 新增 FakeGrokVideoService：基于 Yunwu API grok-video-3 模型，JSON 接口，支持 aspect_ratio/size/images 参数
* _conf_schema.json 新增 yunwu_grok_video_3 模板

**🔧 优化：精简提供商模板**

* 删除 9 个不用的提供商模板：Gemini 原生 / Flow2API（出图）/ Vertex 匿名 / Grok Images / Grok Chat / Grok2API Images / Gemini Chat 图 / Flow2API（视频）/ 魔搭 Images
* provider_registry.py 清理 _TEMPLATE_KEY_ALIASES 无用别名

### v1.3.4

**🚀 新功能：真Grok 视频提供商（Yunwu API）**

* 新增 RealGrokVideoService：基于 Yunwu API 的真 Grok 视频生成（图生视频/文生视频），异步轮询模式
* _conf_schema.json 新增 yunwu_grok_video 模板：支持时长(2-12s)、宽高比(16:9/9:16/1:1/4:3/3:4/21:9)、超时、轮询间隔等配置
* provider_registry.py 注册 yunwu_grok_video 模板，绑定到 RealGrokVideoService
* _async_generate_video 透传 image_url 给后端，支持 LLM 传图片 URL 做图生视频

**🐛 Bug 修复**

* 修复 input_reference 格式：Yunwu API 只接受 URL 字符串，不支持文件上传

### v1.3.3

**🔧 优化：补拍搜图收紧 + 无参考图自由发挥**

* 新增 daily_selfie_ref_min_similarity 配置项：补拍时从衣橱搜参考图的向量相似度阈值，设为0则走衣橱全局阈值，设为0.6~0.7可增加「无参考图自由发挥」的比例
* 补拍有参考图时一律使用 reimagine 强度：仅借用服装款式信息，姿势构图完全重新设计
* 补拍无参考图时指引增强：要求用自然连贯长句详细描述画面，包括场景、衣服款式/材质/颜色/层次/穿着状态
* wardrobe.get_reference_image 全链路支持 min_similarity 参数透传（需衣橱 >= 2.4.1）

---

### v1.3.2

**🚀 新功能：补画完成后自动发空间说说**

* 补画完成后自动将生成的图片发布到 QQ 空间说说，角色以第一人称写日常分享配文
* 人格级配置：`daily_selfie_qzone_publish_enabled`（开关）和 `daily_selfie_qzone_chat_provider_id`（多模态 LLM 提供商，独立配置，与补画对话模型分开）
* 新增 `_publish_to_qzone` 方法：检查配置 → 生成配文 → 读取图片 bytes → 调用 QZone 插件发布说说
* 新增 `_generate_qzone_caption` 方法：调用多模态 LLM 看图生成自然随意的空间说说配文
* `_generate_one_selfie` 返回值从 `bool` 改为 `Path | None`，支持收集成功图片路径

---


**🔧 优化：间隔拉长 + 失败自动重试**

* 补画请求间隔从 5s 拉长到 30s，避免短时间内大量并发冲击 API 导致排队超时
* 新增失败自动重试（daily_selfie_retry_on_fail开关，默认开启）：补画失败或超时的图片会自动重新生成，直到当天额度用完
* 全局补画默认时间从 23:30 提前到 23:00，给重试留出更多时间窗口

---

### v1.3.0

**🚀 新功能：人格级视频生成 + 人格级补拍时间**

* 视频生成支持人格级：在 selfie_persona_N 配置中新增 video_provider_ids 字段，每个人格可独立指定视频服务商链路，实现 A人格优先调用M提供商、B人格优先调用N提供商
* 补画触发时间支持人格级：在 selfie_persona_N 配置中新增 daily_selfie_schedule_time 字段，每个人格可设置独立的补画触发时间，留空则使用全局设置
* 新增 _get_persona_video_chain(persona_name) 方法：从人格配置获取专属视频链路
* cron 循环改为多时间段感知：自动计算所有启用人格中的最早触发时间，到点只运行匹配当前时间的人格

---

### v1.2.9

**🔧 优化**

* `aiimg_wardrobe_preview` 提示词增强：返回值中新增"前N张参考图是你的人设图"说明，LLM 现在能明确区分人设图与衣橱参考图，避免将 wardrobe 参考图误当人设图描述

---

### v1.2.8

**🐛 Bug 修复**

* 修复补画任务全局锁：A人格补画时B人格命令被拒绝，改为按人格隔离（`_selfie_tasks` dict）
* 修复参考图序号错乱：补画和手动取图的参考图序号现在根据人设参考照数量动态计算，与实际 images 列表索引对齐

**🔧 优化**

* 搜图并行化：`_search_reference_images` 改用 `asyncio.gather` 并发搜索，不再逐条串行等待

---

### v1.2.7

**✨ 新功能**

* 后台生成模式：LLM 调用画图工具时不再阻塞对话，图片在后台生成完成后自动发送，期间可继续聊天（配置项 `llm_tool.background_generate`，默认开启）
* 关闭后台模式后恢复原有行为：等待图片生成完毕，LLM 可在上下文中看到图片

---

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
