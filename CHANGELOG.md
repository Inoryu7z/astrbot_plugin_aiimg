# Changelog

## v1.1.2

**✨ 新功能**

* 新增 `aiimg_wardrobe_preview` LLM 工具：自拍前先获取衣橱参考图的文字描述，LLM 可据此构建更精确的提示词（两步调用流程）
* 新增 `_wardrobe_preview_cache` 缓存机制：preview 结果自动传递给后续 `aiimg_generate`，避免重复调用衣橱搜索
* selfie 配置新增 `enabled` 和 `llm_tool_enabled` 字段，与其他功能（draw/edit/video）保持一致

**🐛 Bug 修复**

* 修复自动存图不区分生成模式的问题：自动存图现在仅保存自拍模式生成的图片，文生图/改图不再自动存入
* 修复命令路径自动存图不触发的问题：`after_message_sent` 钩子仅在 Pipeline RespondStage 触发，`event.send()` 不会触发；新增 `_trigger_wardrobe_auto_save` 主动调用衣橱存图方法
* 修复 `_last_image_by_user` 不携带模式信息的问题：类型从 `dict[str, Path]` 改为 `dict[str, dict]`，增加 `mode` 字段

**📝 文档**

* 更新 AIIMG_DEV_GUIDE.md 与当前代码同步
* 新增 HANDOFF.md 交接文档（本地维护，不上传 GitHub）

---

## v1.1.1

**🐛 Bug 修复**

* 修复衣橱 `get_reference_image` 调用无异常保护的问题：衣橱插件异常时不再导致自拍整体失败，改为跳过并继续正常生图。
* 修复配置读取不一致：衣橱参考图配置改用 `_get_feature("selfie")`，与类内其他配置读取方式统一，增加类型安全检查。
* 修复 `extra_refs` 计数未包含衣橱参考图的问题：prompt 中"额外参考图数量"现在正确反映衣橱参考图。
* 修复 `core/utils.py` 缺少 `Path` 导入：引用消息中的本地文件路径图片会触发 `NameError`，现已补全导入。
* 修复 `selfie_regex_fallback` 条件过于宽泛：`"自拍" in msg` 会匹配普通聊天消息（如"教我自拍"），改为只匹配带命令前缀的情况。

**🧹 代码清理**

* 移除 `aiimg_generate` 中 `if self is None` 不可达的死代码。
* 移除衣橱调用处 `persona_name or ""` 的冗余写法（此处 `persona_name` 必定非空）。

---

## v1.1.0

**✨ 衣橱参考图接入**

* 新增 `features.selfie.wardrobe_ref_enabled` 配置项，开启后自拍时会自动从衣橱插件获取参考图。
* 衣橱参考图会追加到人设参考图后面，供改图模型作为服装/姿势/场景参考。
* 自动排除当前人格的图库，只从其他人格的衣橱中搜索，避免人格混淆。
* 需要衣橱插件已安装且已配置取图模型（search_provider_id）。

---

## v1.0.0

**🎉 初始版本发布**

* 支持多服务商文生图、图生图/改图、视频生成。
* Bot 自拍功能：支持上传参考人像，通过改图模型生成自拍。
* 支持多人格自拍配置，每个人格可独立设置参考照、服务商链路和提示词前缀。
* LLM 工具调用支持，模型可主动调用 aiimg_generate 生成图片。
* 预设提示词、多 Key 轮询、失败重试与超时配置。
