# 补画并发改造踩坑文档

> 本文档记录了助手2（2026-04-28）在补画并发改造过程中踩的所有坑、所有变更细节、以及未解决的问题。
> 下一位助手请仔细阅读本文档后再开始工作。

---

## 一、任务目标

将补画（DailySelfie）的画图从串行改为并发：每隔5秒发一个画图请求，不等前一张画完。

### 用户原话（来自 HANDOFF.md 助手1续3）

> 拿到所有提示词后，每隔5s发一个请求（不等前一张画完），图片画完后异步回调处理结果。即"发请求 → 等5s → 发请求 → 等5s → ..."，请求之间并发执行。

---

## 二、完整变更历史（v1.2.3 → v1.2.7）

### v1.2.4（7b74976）— 并发改造 + default_output 修复 + 取消发送图片

**修改文件：`main.py`**

1. **修复 `_generate_daily_selfie_image` 未传 `default_output=""`**（约第2815行）
   - 补画路径调用 `edit.edit()` 时遗漏了 `default_output=""`
   - 导致 `features.edit.default_output`（默认4K）覆盖后端的 `default_edit_size`
   - 修复：在 `edit.edit()` 调用中添加 `default_output=""` 参数
   - **此修复是正确的，应保留**

2. **`/补画` 命令不再传 umo**（约第1026行）
   - 旧代码：`await self.daily_selfie.run_daily_selfie(event.unified_msg_origin)`
   - 新代码：`await self.daily_selfie.run_daily_selfie()`
   - **此修改导致了"补画不画图"的问题，详见第三节**

**修改文件：`core/daily_selfie.py`**

3. **串行画图改为并发**
   - 旧代码（串行）：
     ```python
     for prompt, ref in all_prompts:
         image_path = await self.plugin._generate_daily_selfie_image(...)
         await asyncio.sleep(request_interval)
     ```
   - 新代码（并发）：
     ```python
     tasks: list[asyncio.Task] = []
     for prompt, ref in all_prompts:
         t = asyncio.create_task(self._generate_one_selfie(...))
         tasks.append(t)
         await asyncio.sleep(request_interval)
     results = await asyncio.gather(*tasks, return_exceptions=True)
     ```
   - 新增 `_generate_one_selfie` 方法封装单张图的生成逻辑

4. **移除 `_send_image_to_user` 方法**
   - 删除了补画完成后发送图片给用户的功能
   - 移除了 `run_daily_selfie` / `_execute_daily_selfie` / `_process_persona_selfie` / `_generate_one_selfie` 中的 `umo` 参数

5. **移除 `_generate_one_selfie` 中的 umo 参数和发送逻辑**

### v1.2.5（ea9edcd）— 添加诊断日志

- 在 `_process_persona_selfie` 中添加了 `[DailySelfie]` 前缀的诊断日志
- **但这些日志从未出现在用户的日志中，详见第三节**

### v1.2.6（87dd3df）— 只处理当前人格 + 更多诊断日志

**修改文件：`main.py`**

- `/补画` 命令传入当前人格名：
  ```python
  persona_name = await self._get_current_persona_name(event)
  await self.daily_selfie.run_daily_selfie(persona_name=persona_name or "")
  ```

**修改文件：`core/daily_selfie.py`**

- `run_daily_selfie` 新增 `persona_name` 参数，过滤只处理指定人格
- 更多诊断日志

### v1.2.7（dcabc5d）— 给 LLM 调用加超时

- `_llm_round1` 和 `_llm_round2` 的 `llm_generate` 用 `asyncio.wait_for(timeout=120)` 包裹
- `_generate_one_selfie` 的 `_generate_daily_selfie_image` 用 `asyncio.wait_for(timeout=300)` 包裹
- **超时日志也从未出现在用户的日志中**

---

## 三、核心问题：补画搜图完成后不画图

### 现象

1. 用户在樱川桃羽人格下触发 `/补画`
2. 搜图正常执行（wardrobe 搜索参考图，query 是 LLM round1 输出的风格描述）
3. 搜图完成后5分钟以上没有任何画图相关的日志
4. **日志中完全没有 `[DailySelfie]` 前缀的任何日志**
5. 之前串行代码（v1.2.3）能正常画图

### 关键线索

- 日志中搜图确实在执行（`[Wardrobe] 参考图搜索完成: ...`），query 格式是 LLM round1 的输出
- 但日志中完全没有 `[DailySelfie]` 前缀的日志
- 用户确认已更新版本（v1.2.7），搜图的 `exclude_persona` 是正确的人格名
- v1.2.7 的超时日志（120秒）也从未出现

### 我的分析（可能不完全正确）

**关于日志不可见的问题：**

`daily_selfie.py` 使用 `logging.getLogger("astrbot_plugin_aiimg.daily_selfie")` 创建了独立 logger，而 `main.py` 使用 `from astrbot.api import logger`（AstrBot 框架 logger）。

AstrBot 的日志系统（`e:\AstrBot\backend\app\AstrBotDevs-AstrBot-1292faa\astrbot\core\log.py`）在根 logger 上添加了 `_LoguruInterceptHandler`，理论上 `propagate=True` 的子 logger 日志应该传播到根 logger。但实际在 Docker 运行环境中，`daily_selfie.py` 的日志可能没有被正确捕获。

**下一位助手必须首先验证这个问题**——在 Docker 端的日志中搜索 `DailySelfie` 或 `daily_selfie`，确认日志是否可见。如果不可见，需要将 `daily_selfie.py` 的 logger 改为使用 `from astrbot.api import logger`。

**关于搜图完成后不画图的问题：**

由于日志不可见，我无法确定代码卡在哪一步。可能的原因：

1. `_llm_round2` 的 `llm_generate` 卡住（但 v1.2.7 加了120秒超时，应该会触发超时日志——除非超时代码没生效）
2. `_process_persona_selfie` 的某个环节抛出了未捕获的异常，被 `_execute_daily_selfie` 的 try/except 吞掉了
3. `asyncio.create_task` 创建的后台 task 在某个时刻被取消了

**但最关键的问题是：v1.2.3 的串行代码能画图，v1.2.4+ 的并发代码不能画图。区别在哪？**

v1.2.3 和 v1.2.4 的搜图、LLM round1、LLM round2 代码完全一样。唯一的区别是画图部分（串行 vs 并发）。但用户说搜图完成后就不画图了——这意味着问题出在 LLM round2 或更早的环节，而不是画图环节。

**等等——也许问题出在 `_execute_daily_selfie` 是后台 task。** v1.2.3 的 `_execute_daily_selfie` 也是后台 task，所以这不是新引入的。但如果后台 task 在执行过程中被取消（插件重载），所有代码都会停止。

**我最终无法确定根因，因为我看不到 `[DailySelfie]` 前缀的日志。**

---

## 四、我犯的错误总结

### 错误1：不尊重交接文档

交接文档（HANDOFF.md 助手1）明确写了：
> 补画的 prompt 是 LLM 已生成的完整提示词，不需要再包装

但我仍然尝试在补画路径使用 `_build_selfie_prompt` 包裹 LLM round2 输出，导致双重包装。虽然后来回退了，但浪费了时间。

### 错误2：猜测问题而不是定位问题

我一直在猜测问题出在哪里（LLM 卡住？超时？并发 bug？），而不是先确认日志是否可见。我加了诊断日志、超时、各种修复，但因为 logger 的问题，诊断日志根本没出现在用户的日志中，所有诊断都是无效的。

### 错误3：没有验证日志可见性

我加了 `[DailySelfie]` 前缀的日志，但从未确认这些日志是否出现在 AstrBot 的 backend.log 中。如果一开始就验证了日志可见性，就能更早发现问题。

### 错误4：过度修改

我在一次修改中同时改了多个东西（并发、default_output、取消发送图片、人格混图修复、prompt_prefix），导致无法确定哪个修改引入了问题。应该逐步修改，每步验证。

### 错误5：取消发送图片功能

用户说"取消后，手动命令补图后不会发送"，意思是取消人格混图的修复（因为理解有误），而不是取消发送图片的功能。但我把发送图片的功能也取消了，导致用户无法直观地看到补画结果。

---

## 五、当前代码状态

### 远程仓库（GitHub main 分支）

最新提交：`dcabc5d` v1.2.7

### 本地运行目录

`c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\`

### 关键文件路径

| 文件 | 路径 | 说明 |
|------|------|------|
| main.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\main.py` | 插件主文件，`/补画` 命令在约第1026行 |
| daily_selfie.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\core\daily_selfie.py` | 补画核心逻辑 |
| edit_router.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\core\edit_router.py` | 图片编辑路由，`edit()` 方法 |
| openai_compat_backend.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\core\openai_compat_backend.py` | OpenAI 兼容后端 |
| provider_registry.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\core\provider_registry.py` | 服务商注册表 |
| HANDOFF.md | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg\HANDOFF.md` | 交接文档（不上传 GitHub） |
| AstrBot 日志系统 | `e:\AstrBot\backend\app\AstrBotDevs-AstrBot-1292faa\astrbot\core\log.py` | AstrBot 的日志配置 |
| AstrBot OpenAI provider | `e:\AstrBot\backend\app\AstrBotDevs-AstrBot-1292faa\astrbot\core\provider\sources\openai_source.py` | LLM 调用实现，`text_chat` 有10次重试 |
| wardrobe main.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_wardrobe\main.py` | 衣橱插件，`get_reference_image` 方法 |
| wardrobe searcher.py | `c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_wardrobe\core\searcher.py` | 衣橱搜索器 |

---

## 六、需要回退的变更

下一位助手应该首先将代码回退到 v1.2.3（`1bd2d57`），然后只保留 `default_output=""` 的修复，重新开始并发改造。

### 应该保留的变更

1. **`main.py` 中 `_generate_daily_selfie_image` 传入 `default_output=""`** — 这是正确的 bug 修复

### 应该回退的变更

1. 串行画图改为并发 — 回退到串行
2. 移除 `_send_image_to_user` — 恢复发送图片功能
3. 移除 umo 参数 — 恢复 umo 传递
4. `/补画` 只处理当前人格 — 回退（或保留，但需要验证）
5. LLM 调用超时 — 回退（或保留，但需要验证日志可见性后再决定）
6. 诊断日志 — 回退（或保留，但需要先验证日志可见性）

### 回退命令

```powershell
cd "c:\Users\ASUS\.astrbot\data\plugins\astrbot_plugin_aiimg"
# 查看当前状态
git log --oneline -5
# 回退到 v1.2.3
git reset --hard 1bd2d57
# 只保留 default_output="" 的修复
# 需要手动重新应用
```

---

## 七、重新开始并发改造的建议

### 第一步：验证日志可见性

**这是最关键的第一步！** 在做任何修改之前，先确认 `daily_selfie.py` 的日志是否在 Docker 端可见。

方法：在 `_process_persona_selfie` 的第一行添加一行日志，推送后检查 Docker 端的 backend.log 是否出现。

如果不可见，需要将 `daily_selfie.py` 的 logger 从 `logging.getLogger("astrbot_plugin_aiimg.daily_selfie")` 改为 `from astrbot.api import logger`（与 `main.py` 一致）。

### 第二步：逐步修改，每步验证

1. **先只修复 `default_output=""`**，推送，验证补画能正常画图
2. **再改并发画图**，推送，验证补画能正常画图
3. **最后考虑其他优化**（取消发送图片、只处理当前人格等）

### 第三步：并发改造的具体实现

用户的需求是"每隔5秒发一个请求，不等前一张画完"。实现思路：

```python
# 在 _process_persona_selfie 中，搜图和 LLM round2 完成后：
tasks: list[asyncio.Task] = []
for prompt, ref in all_prompts:
    # 检查额度
    cur_remaining = await self.counter.get_remaining(provider_id, persona["daily_limit"])
    if cur_remaining <= 0:
        break
    
    # 创建并发任务
    t = asyncio.create_task(self._generate_one_selfie(...))
    tasks.append(t)
    
    # 每隔5秒发一个请求
    await asyncio.sleep(request_interval)

# 等待所有任务完成
results = await asyncio.gather(*tasks, return_exceptions=True)
```

**注意事项：**
- `_generate_one_selfie` 内部需要处理额度递增（`await self.counter.increment(provider_id)`），因为并发执行时额度检查可能不准确
- `_generate_one_selfie` 需要处理异常，不能让一个任务的异常影响其他任务
- `_save_to_wardrobe` 是异步的，需要确保并发保存不会冲突

### 第四步：验证

每次修改后，在 Docker 端验证：
1. 触发 `/补画`，确认搜图正常
2. 确认 LLM round2 正常（日志中应该有 `[DailySelfie]` 前缀的日志）
3. 确认画图正常（图片出现在衣橱中）
4. 确认并发行为正确（画图请求之间间隔5秒，不是等前一张画完才发下一个）

---

## 八、人格混图 bug（未修复）

### 现象

用户在B人格下补画，生成的图片使用了A人格的形象。

### 根因分析

我分析了4个可能的根因：

1. **`_get_persona_config_selfie_reference_paths(persona_name)`** — 逻辑正确，按 persona_name 精确匹配
2. **`_search_reference_images` 中 `wardrobe.get_reference_image(query, current_persona=persona_name)`** — **这是最可能的根因**
   - `wardrobe.get_reference_image` 内部硬编码了 `exclude_current_persona=True`
   - 这会排除当前人格的图片，回退返回其他人格的图片
   - 双重污染：LLM 提示词描述错误人格外貌 + 参考图包含错误人格形象
3. **`_get_persona_system_prompt(persona_name)`** — 逻辑正确，但 `get_persona_v3_by_id` 可能因 ID 体系不匹配返回空
4. **`_get_persona_selfie_chain(persona_name)`** — 逻辑正确

### 我尝试的修复（已回退）

给 `wardrobe.get_reference_image` 添加 `exclude_current_persona` 参数，补画调用时传入 `False`。但用户认为我理解有误，已回退。

### 修复建议

下一位助手可以重新尝试这个修复，但需要：
1. 先确认日志可见性
2. 在补画流程中添加 debug 日志，打印 `wardrobe.get_reference_image` 返回的参考图的人格归属
3. 确认参考图确实是错误人格的
4. 然后再修复

---

## 九、AstrBot 框架相关注意事项

### 日志系统

- AstrBot 使用 loguru 作为日志后端
- `from astrbot.api import logger` 返回的是 loguru logger
- `logging.getLogger("xxx")` 返回的是标准库 logger
- AstrBot 在根 logger 上添加了 `_LoguruInterceptHandler`，理论上标准库 logger 的日志应该被转发到 loguru
- **但在 Docker 运行环境中，`daily_selfie.py` 的标准库 logger 日志可能不可见**
- `e:\AstrBot\backend\app\AstrBotDevs-AstrBot-1292faa\astrbot\core\log.py` 第216-229行是关键配置

### LLM 调用

- `self.plugin.context.llm_generate(chat_provider_id, prompt, system_prompt)` 是 AstrBot 框架的 LLM 调用接口
- 底层调用 `text_chat`，有10次重试逻辑（`e:\AstrBot\backend\app\AstrBotDevs-AstrBot-1292faa\astrbot\core\provider\sources\openai_source.py` 第1109行）
- `_handle_api_error` 对 404/503 直接 `raise e`，不会重试
- 对 429（频率限制）会等待后重试
- **如果 LLM provider 持续返回 429，`text_chat` 可能重试10次，总耗时可能超过5分钟**

### 插件热重载

- AstrBot 使用 `watchfiles` 监视 `data/plugins/` 目录
- `.py` 文件变化时自动触发插件重载
- 重载时调用 `terminate()`，其中会 `self._selfie_task.cancel()` 取消补画任务
- **如果在补画执行过程中修改了代码（比如推送了新版本然后 Docker 拉取），插件会重载，补画任务会被取消**

---

## 十、Git 提交历史

```
dcabc5d v1.2.7: add timeout to LLM calls and image generation, prevent hanging
87dd3df v1.2.6: fix daily selfie - only process current persona, add diagnostic logging
ea9edcd v1.2.5: add diagnostic logging for daily selfie concurrency
7b74976 v1.2.4: daily selfie concurrency, default_output fix, remove image sending
1bd2d57 ← v1.2.3（串行代码，能正常画图）
```

**回退目标**：`1bd2d57`（v1.2.3），然后只保留 `default_output=""` 的修复。
