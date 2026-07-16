### v1.8.3

**🔧 补拍额度计数策略重构**

*   修复手动自拍额度计数与服务商实际消耗对不上的问题：根因为 `_track_selfie_quota` 按链路顺序 reserve 第一个 provider，但手动自拍走 `edit_router` 链路兜底时实际成功的 provider 可能是后面的，导致计数错位
*   `edit_router` 新增 `last_success_provider` 属性记录实际成功的 provider_id
*   `_generate_selfie_image` 返回 `(Path, used_pid)`，逐层透传到 `_track_selfie_quota`，用实际 provider reserve 而非链路第一个
*   `DailyQuotaCounter` 计数 key 从 `provider_id` 改为 `persona_name::provider_id`，所有方法增加 `persona_name` 参数
*   保留失败 release 逻辑（网络超时频繁，避免额度被耗尽导致补拍不够数）

---

### v1.8.2

**🐛 修复 GrokVideo3 轮询状态识别失败导致请求爆炸**

*   修复中转站轮询返回大写 `SUCCESS` 状态时无法识别，导致任务成功但代码持续重试，3 模型 × 3 次 = 9 次请求
*   状态识别支持大写 `SUCCESS`/`ERROR`，`video_url` 提取支持 `data.video_url` 嵌套
*   降低 `retry_delay` 默认值：2 → 0（多模型级联已有 fallback）

---

### v1.8.1

**🐛 修复引用图片生成视频全部失败 + 后端重命名**

*   修复图生视频 FileNotFoundError：AstrBot PreProcessStage 在事件结束后清理临时图片文件，异步任务读图时已不存在。新增 `_prefetch_image_from_event` 在 `create_task` 前预提取 image_bytes
*   新增 `_extract_image_bytes_from_seg` fallback：按 url → file → path 顺序尝试下载/解码/读取
*   `grok_video_3` 模板重命名为 `grok_video_multipart`，保留旧名兼容
*   `grok_video_multipart` 的 `aspect_ratio` 从下拉框改为自由字符串输入，默认 16:9

---

### v1.8.0

**🚀 视频后端重构：移除云雾旧格式 + 新增 Grok Video 3 + 多模型级联**

*   移除云雾旧格式后端 `yunwu_grok_video` / `yunwu_grok_video_3`（不做兼容迁移，旧配置需手动改为新后端）
*   新增 `grok_video_3` 视频后端模板：基于 multipart/form-data 协议，适用于 PoloAI / s.apifox 等兼容接口，支持参考图远程 URL 和本地文件上传
*   `OfficialGrokVideoService` 适配 PoloAI 官方兼容格式：request_id 提取兼容 `id` / `task_id` 字段
*   新增 `MultiModelVideoCascade` 包装类：同一后端下按顺序尝试多个模型名，失败自动切换。所有视频后端模板新增 `models` 字段（留空则回退单模型模式）
*   `truegrok` / `official_grok_video` 模板 hint 更新：移除云雾引用

---

### v1.7.2

**✨ 提示词构建系统提示词人格级暴露**

* 补画第4轮「提示词构建」系统提示词现支持按人格单独配置，与 v1.7.0 的创意设计提示词保持一致
* 每个人格块（selfie_persona_1/2/3）新增 `prompt_engineer_system_prompt` 字段，预填完整默认提示词（3647字），用户可在 WebUI 中直接编辑
* 用户可根据角色特征调整提示词构建风格：例如强调某些视觉元素、调整提示词偏好等
* 配置留空时自动回退到插件内置默认提示词，不影响现有行为

---

### v1.7.1

**✨ 补拍风格池人格级定制**

* 补拍风格池现支持按人格单独定制，配合衣橱插件 v2.9.0 的人格级风格池功能
* `_get_style_pool` 方法新增 `persona_name` 参数，优先调用 `wardrobe.get_style_pool_for_persona()` 获取人格级风格池
* 人格级风格池留空时自动回退到衣橱全局风格池，不影响现有行为
* 风格池获取从循环前全局获取改为循环内按人格获取，确保每个人格使用自己的风格池

---

### v1.7.0

**✨ 创意设计系统提示词人格级暴露**

*   补画第3轮「创意设计」系统提示词现支持按人格单独配置，参考 dayflow 插件的配置界面风格
*   每个人格块（selfie_persona_1/2/3）新增 `costume_designer_system_prompt` 字段，预填完整默认提示词，用户可直接在 WebUI 中编辑
*   用户可根据角色特征自行调整：例如不戴眼镜的角色可删除眼镜约束，喜欢穿裙子的角色可添加裙装偏好
*   配置留空时自动回退到插件内置默认提示词，不影响现有行为
*   字段使用 markdown 编辑器（全屏模式），与 dayflow 插件配置界面风格一致

---

### v1.6.9

**🚀 官方 Grok 视频后端适配云雾官方兼容格式**

*   修复 `OfficialGrokVideoService` 的 `image` 字段格式 Bug：从字符串改为 `{"url": ...}` 对象格式，符合 xAI 官方 API 规范（否则图生视频参考图无法识别）
*   新增 `reference_images` 多参考图数组支持（与 `image` 互斥），格式为 `[{"url": "..."}]`
*   新增 `edit_video_url` 方法：`POST /v1/videos/edits`，接收 `video: {"url": "..."}` 进行视频编辑
*   提取 `_poll_video_task` 共享轮询逻辑，消除 generate 与 edit 的重复代码
*   配置默认 model 从 `grok-videos` 改为 `grok-imagine-video`，server_url hint 补充云雾地址

---

### v1.6.8

**🐛 修复：image 模式下 LLM 重复发送图片**

*   修复 image 上下文模式下，LLM 调用画图工具后重复调用 `send_message_to_user` 导致用户收到重复图片+多余文本的问题
*   根因：AstrBot 框架在工具返回 `ImageContent` 时硬编码添加"Use send_message_to_user to send it to the user"指令，而插件已直接发送无损原图
*   新增 `_patch_agent_runner_for_direct_send` 运行时补丁：将 aiimg 工具的框架指令替换为"图片已直接发送，不要再次发送，请生成文本回复"
*   `_build_llm_tool_image_result` 返回值从纯 `ImageContent` 改为 `TextContent` + `ImageContent`，双重保障防止 LLM 重复发送

---

### v1.6.7

**🔄 适配 QZone v4.0.0 Daemon API**

*   适配 QZone 插件 v4.0.0 Daemon 架构重构：`service.publish_post()` → `controller.publish_post()`

---

### v1.6.6

**🚀 补拍架构重构：两轮→四轮**

*   补拍提示词生成从两轮架构重构为四轮架构：Round1（风格选择）与 Round2（场景生成）并行执行 → 代码顺序配对 + 搜图 → Round3（创意设计，统一有图/无图）→ Round4（提示词构建）
*   风格和场景独立生成，打破刻板耦合（如"汉服→书法""JK→教室"），让画面更多元化
*   有图/无图不再分流，统一走 Round3→Round4
*   修复：有参考图但无描述时，参考图不再在生图环节丢失

---

### v1.6.5

**🐛 修复视频发送超时误判导致重复发送**

*   修复 auto 模式下，本地文件发送遇到 QQ NTEvent 超时（retcode=1200）时，消息实际已送达但被误判为失败，导致降级到 URL 发送，用户收到重复视频
*   新增 _is_timeout_likely_sent 方法，检测超时类错误（timeout / retcode=1200），视为消息可能已送达，不再尝试其他发送方式

---

### v1.6.4

**🐛 修复**

*   修复补拍发空间图片上传失败（顽固问题）：`_ensure_qzone_compatible_image` 移除格式短路逻辑，无条件重编码为 baseline RGB JPEG（`progressive=False`），确保剥离所有不兼容的色彩模式（CMYK/RGBA/YCCK 等）和渐进式编码；`_publish_to_qzone` 移除 URI 回退，转换失败则跳过该图片

---

### v1.6.3

**🐛 修复**

*   修复补拍发空间图片上传失败（"空间相册仅支持JPG、GIF、PNG、BMP等格式的图片"）：`_publish_to_qzone` 改为主动读取图片 bytes 并用 `_ensure_qzone_compatible_image` 确保格式兼容后传递给 qzone 插件，不再依赖 `file:///` URI + qzone `download_file` 链路；增加调试日志（图片路径、大小、magic bytes、PIL 检测格式）便于排查

---

### v1.6.2

**🚀 新功能**

* 自拍链路支持 per-provider output 覆盖：人格 selfie_persona 的 `provider_ids` 升级为 `chain`（template_list），每个服务商可单独设置输出分辨率，与改图/文生图的 chain 格式一致
* 改图命令支持末尾比例参数：`/改图 换背景 16:9` 自动解析比例并映射尺寸，与 `/生图`、`/自拍` 一致
* 比例参数支持中文冒号：`16：9` 等中文冒号输入自动规范化，不再被当作提示词
* 文生图默认输出尺寸可留空：`draw.default_output` 下拉新增空选项，留空时由服务商默认决定
* 提供商支持自定义 User-Agent 请求头：每个服务商可配置 `user_agent` 字段，填入浏览器 UA 可绕过中转站 UA 拦截

**🐛 修复**

* 自拍 `default_output` 优先级修正：`default_output` 改为传参而非设为 `size`，不再覆盖 per-provider output
* daily_sharing 插件同步修复自拍 `default_output` 传参问题

---

### v1.6.1

**🔄 提示词优化：适配4字段结构**

* 豆包创意设计师输出从3字段（clothing/pose/scene）升级为4字段（clothing/appearance/pose/scene）
* 新增 appearance（外观造型）字段：承载发型+指甲油，与服装/动作/场景逻辑分离
* 豆包系统提示词全面重写：创意总监角色定位、可视化全覆盖原则、概念一致性、姿态-场景互动、结构多样性、细节具体化示例等
* DeepSeek 提示词优化大师系统提示词全面重写：信息筛选与力度分配、位置权重、视觉维度覆盖、叙事流畅性、常见生成失败预防等
* JSON 解析逻辑和提示词工程调用同步适配4字段

---

### v1.6.0

**🚀 新功能：无参考图创意设计分流**

* 补拍搜图无参考图时，可调用创意设计提供商（如豆包）进行专业服装与场景设计，再由对话模型生成提示词
* 人格级新增 `costume_designer_provider_id` 配置：选择创意设计提供商，留空则由对话模型自行构建提示词（现有行为）
* 创意设计提供商返回结构化 JSON（clothing/pose/scene），对话模型据此产出最终提示词
* 创意设计提供商调用失败时自动重试一次，仍失败则优雅降级为现有无图自由发挥行为
* 补画批次大小从 5 降为 3，降低单次 LLM 压力
* 有图/无图路径分离处理：有图走现有 Round2 流程，无图走创意设计→提示词优化流程

**🐛 修复：provider_registry 预存变量名 Bug**

* `_resolve_template_key` 中 `normalized` 引用错误改为 `item`（此前因 WebUI 自动注入 `__template_key` 而未触发）

---

### v1.5.2

✨ 新增：补拍衰减过滤联动

* `_search_reference_images()` 调用 `get_reference_image()` 时传入 `daily_selfie_mode=True`，启用衣橱插件的补拍衰减过滤

---

### v1.5.1

**🐛 修复：生图/视频后端隔离失效**

* provider_registry 自动根据模板类型注入 kind 字段：视频模板（grok_video 等）kind=video，其余 kind=image
* resolve_backend 新增 kind 参数：generate 工具只能解析 image 后端，video 工具只能解析 video 后端
* 根因：_conf_schema.json 中只有 truegrok 模板定义了 kind 字段，其他视频模板和所有图片模板都缺少 kind，导致用户添加的生图后端不可见，而 generate 工具能错误调用到视频后端

﻿### v1.5.0

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
