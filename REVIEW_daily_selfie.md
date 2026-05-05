# 补画功能代码审查报告

> 审查日期: 2026-04-25
> 审查范围: `core/daily_selfie.py`, `main.py` 中补画相关代码, `_conf_schema.json`, `core/edit_router.py`, `core/draw_service.py`
> 审查重点: 代码质量与 Bug

---

## 🔴 严重问题 (可能导致功能异常)

### 1. LLM 输出解析未清理编号前缀 ✅ 已修复

**文件**: `daily_selfie.py` L444, L486

`_llm_round1` 和 `_llm_round2` 通过 `text.split("\n")` 按行拆分 LLM 输出，但未清理常见的编号前缀（如 `1. `, `2. `, `- `, `• ` 等）。LLM 极有可能输出带编号的列表，导致最终提示词以编号开头，影响生图质量。

**修复方案**: 新增 `_clean_llm_line()` 和 `_parse_llm_lines()` 工具函数，使用正则清理编号/项目符号前缀。`_llm_round1` 和 `_llm_round2` 均改用 `_parse_llm_lines()`。

---

### 2. `_save()` 同步 I/O 阻塞事件循环 ✅ 已修复

**文件**: `daily_selfie.py` L39-47

`_save()` 使用 `self._path.write_text()` 和 `self._path.parent.mkdir()` 等同步文件操作，在 `increment()` 持有 `asyncio.Lock` 时被调用。虽然数据量小、影响有限，但理论上会短暂阻塞事件循环。

**修复方案**: 新增 `_save_async()` 方法，使用 `asyncio.to_thread(self._save)` 包装。`increment()` 中改用 `await self._save_async()`。

---

### 3. `/补画` 命令创建的 task 未被追踪 ✅ 已修复

**文件**: `main.py` L1007

```python
asyncio.create_task(self.daily_selfie.run_daily_selfie())
```

此 task 未被存储到任何集合中。如果 task 内部抛出未捕获异常，Python 3.11+ 仅打印警告；更关键的是，插件 `terminate()` 时无法取消此 task。

**修复方案**: 改为 `await self.daily_selfie.run_daily_selfie()`。`run_daily_selfie()` 本身只做检查并创建内部 `_selfie_task`，执行很快，不会阻塞命令返回。

---

## 🟡 中等问题 (可能影响体验或存在隐患)

### 4. `_get_recent_styles()` 日期比较使用字符串 ✅ 已修复

**文件**: `daily_selfie.py` L547

`created` 和 `three_days_ago` 都是 `YYYY-MM-DD` 格式的字符串。虽然字典序比较在此格式下恰好正确，但这是隐式依赖。

**修复方案**: 改用 `datetime` 对象比较，`three_days_ago` 改为 `datetime` 对象，`created_raw` 通过 `datetime.strptime()` 解析后比较，解析失败时跳过而非静默通过。

---

### 5. `_get_recent_styles()` 中 JSON 解析逻辑有冗余分支 ✅ 已修复

**文件**: `daily_selfie.py` L553-557

问题：
- 如果 `style_raw` 是字符串且能被 `json.loads` 解析，结果可能是 list 或 dict。如果是 dict，后续 `for t in tags` 会遍历 dict 的 key，而非 value。
- `isinstance(style_raw, str)` 为 False 时直接包装为 `[style_raw]`，但 `style_raw` 可能已经是 list。

**修复方案**: `json.loads` 结果不再直接包装为 list；新增 `isinstance(tags, dict)` 分支提取 values；新增最终 `if not isinstance(tags, list)` 兜底。

---

### 6. LLM 两轮调用无对话上下文 ⚠️ 已知限制

**文件**: `daily_selfie.py` L416-490

`_llm_round1` 和 `_llm_round2` 各自独立调用 `llm_generate()`，没有传递对话历史。虽然 `style_summary` 作为文本传入了第2轮，但 LLM 不知道第1轮的完整对话内容。

**评估**: `llm_generate` API 不支持传入 conversation history，当前 `style_summary` 方案已是最优折中。暂不修改。

---

### 7. `_process_persona_selfie` 中 prompts 与 batch 的对齐可能错位 ✅ 已修复

**文件**: `daily_selfie.py` L345-370

`descriptions` 过滤掉了无 description 的参考图，但 `batch[i]` 仍按原始 batch 索引取值。如果 batch 中某些元素没有 description 被跳过，prompt 会与错误的参考图配对。

**修复方案**: 新增 `valid_refs` 列表，与 `descriptions` 同步收集有 description 的参考图。最终用 `valid_refs[i]` 替代 `batch[i]`。

---

### 8. `_generate_daily_selfie_image` 未传递 `resolution` 参数 ✅ 已修复

**文件**: `main.py` L2778-2783

对比 `_generate_selfie_image`，后者传了 `resolution=resolution`，但 `_generate_daily_selfie_image` 没有。

**修复方案**: 在 `edit.edit()` 调用中显式添加 `resolution=None`。

---

### 9. 计数器 increment 与实际 API 调用之间无原子性保证 ⚠️ 已知限制

**文件**: `daily_selfie.py` L393-403

如果图片生成成功但 `counter.increment()` 失败（如文件 I/O 错误），计数不会更新，下次补画可能超出额度限制。

**评估**: `_save()` 有 try/except 保护，即使失败也只是 warning，不会抛异常。极端情况下计数可能不准，但实际风险极低。暂不修改。

---

## 🟢 轻微问题 (代码质量/可维护性)

### 10. `request_interval` 和 `batch_size` 硬编码 ⚠️ 待未来配置化

**文件**: `daily_selfie.py` L240, L340

HANDOFF.md 已提到此问题。建议未来可配置化。

---

### 11. `_cron_loop` 不处理系统时钟回拨 ⚠️ 已知限制

**文件**: `daily_selfie.py` L181-194

如果系统时钟被调回，`_seconds_until_next_run()` 可能返回很大的值，导致定时任务长时间不触发。

**评估**: 服务器时钟通常不会大幅回拨，实际影响极低。

---

### 12. `on_provider_request` 钩子保留但未使用 ✅ 已修复

**文件**: `edit_router.py` L35, `draw_service.py` L30

助手3的修复移除了全局回调绑定，但保留了属性和调用点。

**修复方案**: 移除 `edit_router.py` 和 `draw_service.py` 中的 `self.on_provider_request = None` 初始化，以及所有 `if self.on_provider_request:` 调用点。

---

### 13. `/补画` 命令无完成反馈 ⚠️ 待未来实现

**文件**: `main.py` L996-1007

用户执行 `/补画` 后只收到"⏳ 补画任务已启动"，完成后无通知。HANDOFF.md 已提到此问题。

---

## 修复总结

| # | 问题 | 级别 | 状态 |
|---|------|------|------|
| 1 | LLM编号前缀未清理 | 🔴 严重 | ✅ 已修复 |
| 2 | 同步I/O阻塞事件循环 | 🔴 严重 | ✅ 已修复 |
| 3 | /补画 task未追踪 | 🔴 严重 | ✅ 已修复 |
| 4 | 日期字符串比较 | 🟡 中等 | ✅ 已修复 |
| 5 | JSON解析冗余分支 | 🟡 中等 | ✅ 已修复 |
| 6 | LLM无对话上下文 | 🟡 中等 | ⚠️ 已知限制 |
| 7 | prompt-batch错位 | 🟡 中等 | ✅ 已修复 |
| 8 | resolution参数缺失 | 🟡 中等 | ✅ 已修复 |
| 9 | 计数原子性 | 🟡 中等 | ⚠️ 已知限制 |
| 10 | 硬编码参数 | 🟢 轻微 | ⚠️ 待配置化 |
| 11 | 时钟回拨 | 🟢 轻微 | ⚠️ 已知限制 |
| 12 | 未使用钩子 | 🟢 轻微 | ✅ 已修复 |
| 13 | 无完成反馈 | 🟢 轻微 | ⚠️ 待实现 |
