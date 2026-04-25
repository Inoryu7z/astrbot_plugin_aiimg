from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("astrbot_plugin_aiimg.daily_selfie")

_DATE_FMT = "%Y-%m-%d"

_NUMBER_PREFIX_RE = re.compile(r'^[\d]+[.、)\]】]\s*')
_BULLET_PREFIX_RE = re.compile(r'^[-•*]\s+')


def _clean_llm_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    line = _NUMBER_PREFIX_RE.sub('', line)
    line = _BULLET_PREFIX_RE.sub('', line)
    return line.strip()


def _parse_llm_lines(text: str, limit: int) -> list[str]:
    lines = []
    for raw in text.split("\n"):
        cleaned = _clean_llm_line(raw)
        if cleaned:
            lines.append(cleaned)
        if len(lines) >= limit:
            break
    return lines


class DailyQuotaCounter:
    def __init__(self, data_dir: Path):
        self._path = data_dir / "daily_selfie_counter.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                self._data = json.loads(raw) if raw else {}
        except Exception as e:
            logger.warning("[DailySelfie] 计数器文件读取失败，重置: %s", e)
            self._data = {}
        self._ensure_date()

    def _ensure_date(self):
        today = datetime.now().strftime(_DATE_FMT)
        stored = self._data.get("date", "")
        if stored != today:
            self._data = {"date": today, "counts": {}}
            self._save()

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[DailySelfie] 计数器文件保存失败: %s", e)

    async def _save_async(self):
        await asyncio.to_thread(self._save)

    async def increment(self, provider_id: str, amount: int = 1) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            cur = int(counts.get(provider_id, 0))
            new_val = cur + amount
            counts[provider_id] = new_val
            await self._save_async()
            return new_val

    async def get_count(self, provider_id: str) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.get("counts", {})
            return int(counts.get(provider_id, 0))

    async def get_all_counts(self) -> dict[str, int]:
        async with self._lock:
            self._ensure_date()
            return dict(self._data.get("counts", {}))

    async def get_remaining(self, provider_id: str, limit: int) -> int:
        count = await self.get_count(provider_id)
        return max(0, limit - count)

    def get_date(self) -> str:
        return self._data.get("date", "")


_TASK_MODE_SYSTEM_PROMPT = (
    "【自动拍照任务模式】\n\n"
    "当收到自动拍照任务时，你需要完成以下流程：\n"
    "1. 选择拍摄方案（自然语言描述服装风格、场景、姿势）\n"
    "2. 等待系统返回参考图描述\n"
    "3. 按照提示词构建规则构建提示词\n\n"
    "约束：\n"
    "- 每次任务选择多种不同风格、姿势、构图，大胆与保守兼顾\n"
    "- 优先选择近期未尝试过的风格\n"
    "- 输出格式：每条一行，不编号，不解释\n"
    "- 禁止调用aiimg_generate工具"
)

_SKILL_RULES_SYSTEM_PROMPT = (
    "## 提示词构建规则\n\n"
    "### 固定开头\n"
    "每条提示词必须以以下固定开头开始：\n"
    "\"以前3张参考图中的同一少女为基准，完整保留她的五官、身材等全部人体身份特征，"
    "绝对禁止任何拼图，为她生成一张单人的自然生活照："
    "她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，\"\n\n"
    "### 自拍母规则\n"
    "在固定开头之后，按以下逻辑构建画面，最终串联成一段连贯的自然语言视觉描述：\n"
    "1. 最终输出只能是一整段连贯、通顺、符合语法逻辑的自然长句，不要输出分析、分点、规则解释\n"
    "2. 核心结构始终是：主体人物 + 具体动作 + 所处环境\n"
    "3. 只描述可直接视觉化的内容，不要写声音、气味、触感等不可见信息\n"
    "4. 一般地，大部分构图采用中近景\n"
    "5. 穿搭描述必须遵守可见性原则：只写画面里能看见的服装结构与层次，不写完全被遮挡的内容\n"
    "6. 如果要调整动作姿势，则必须写完整，并且必须明确头部朝向与眼神朝向；笑容只用\"微笑\"\n"
    "7. 整体目标是单人、自然、高清、写实的生活照，不是海报、插画、拼图或宣传图\n"
    "8. 每条参考图描述后会附带具体指引，请严格按照指引处理该参考图\n\n"
    "### 强制要求\n"
    "- 最终提示词必须使用中文\n"
    "- 不得使用或生成任何文字、标识或象征性元素\n"
    "- 人物的视觉年龄应符合设定\n"
    "- 面部必须完整露出\n"
    "- 姿势必须物理可行。人物只有两只手和两条腿，不能同时处于矛盾状态，"
    "尤其需要注意图片的描述与你所构建的提示词之间是否冲突\n"
    "- 优先使用服装状态变化或动作间接营造性感效果，而非直接描述敏感身体部位"
)

_ROUND1_USER_PROMPT = (
    "你在整理衣橱时发现，今天还有 {remaining} 次拍照额度没用完。\n\n"
    "衣橱中可选的风格：\n{style_pool}\n\n"
    "近3天已拍过的风格：\n{recent_styles}\n\n"
    "请选择 {remaining} 种不同的拍摄方案，每种方案用一句话描述（包含服装风格、场景、姿势）。\n"
    "要求：风格多样化，优先选择近期未尝试的。\n\n"
    "直接返回 {remaining} 条描述，每条一行。"
)

_ROUND2_USER_PROMPT = (
    "本次选择的拍摄方案：\n{style_summary}\n\n"
    "参考图描述（第{batch_num}批，共{total_batch}批）：\n"
    "{descriptions}\n\n"
    "请为以上 {count} 张参考图构建提示词。\n\n"
    "约束：\n"
    "- 直接返回 {count} 条提示词，每条一行\n"
    "- 禁止调用aiimg_generate工具"
)


class DailySelfieService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.counter = DailyQuotaCounter(plugin.data_dir)
        self._running = False
        self._cron_task: Optional[asyncio.Task] = None
        self._selfie_task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._cron_task = asyncio.create_task(self._cron_loop())
        logger.info("[DailySelfie] 服务已启动")

    async def stop(self):
        self._running = False
        if self._cron_task:
            self._cron_task.cancel()
            self._cron_task = None
        if self._selfie_task:
            self._selfie_task.cancel()
            self._selfie_task = None
        logger.info("[DailySelfie] 服务已停止")

    def _get_schedule_time(self) -> str:
        selfie_conf = self.plugin._get_feature("selfie")
        return str(selfie_conf.get("daily_selfie_schedule_time", "23:30") or "23:30").strip()

    def _parse_schedule_time(self) -> tuple[int, int]:
        time_str = self._get_schedule_time()
        try:
            parts = time_str.split(":")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return 23, 30

    def _seconds_until_next_run(self) -> float:
        now = datetime.now()
        hour, minute = self._parse_schedule_time()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _cron_loop(self):
        while self._running:
            try:
                wait_seconds = self._seconds_until_next_run()
                logger.debug("[DailySelfie] 距离下次执行: %.0f秒", wait_seconds)
                await asyncio.sleep(wait_seconds)
                if not self._running:
                    break
                await self.run_daily_selfie()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[DailySelfie] 定时任务异常: %s", e)
                await asyncio.sleep(60)

    def _get_enabled_personas(self) -> list[dict[str, Any]]:
        personas = []
        for idx in [1, 2]:
            conf = self.plugin._get_selfie_persona_config(idx)
            if not conf:
                continue
            if not self.plugin._as_bool(conf.get("daily_selfie_enabled", False), default=False):
                continue
            provider_id = str(conf.get("daily_selfie_provider_id", "") or "").strip()
            if not provider_id:
                continue
            daily_limit = self.plugin._as_int(conf.get("daily_selfie_limit", 20), default=20)
            persona_name = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            if not persona_name or persona_name == "default":
                continue
            personas.append({
                "index": idx,
                "persona_name": persona_name,
                "provider_id": provider_id,
                "daily_limit": daily_limit,
                "config": conf,
            })
        return personas

    async def run_daily_selfie(self):
        if self._selfie_task and not self._selfie_task.done():
            logger.warning("[DailySelfie] 补画任务正在运行中，跳过本次触发")
            return

        personas = self._get_enabled_personas()
        if not personas:
            logger.info("[DailySelfie] 没有启用补画的人格，跳过")
            return

        wardrobe = self.plugin._get_wardrobe_instance()
        if not wardrobe:
            logger.warning("[DailySelfie] 衣橱插件不可用，跳过补画")
            return

        self._selfie_task = asyncio.create_task(self._execute_daily_selfie(personas, wardrobe))

    async def _execute_daily_selfie(self, personas: list[dict], wardrobe: Any):
        total_success = 0
        total_fail = 0
        request_interval = 5

        debug_mode = self._is_debug()
        selfie_conf = self.plugin._get_feature("selfie")
        logger.info(
            "[DailySelfie] 补画开始: 人格数=%d debug=%s selfie_conf_keys=%s",
            len(personas), debug_mode, list(selfie_conf.keys()),
        )

        try:
            style_pool = await self._get_style_pool(wardrobe)
            recent_styles = await self._get_recent_styles(wardrobe)

            for p in personas:
                remaining = await self.counter.get_remaining(p["provider_id"], p["daily_limit"])
                if remaining <= 0:
                    logger.info("[DailySelfie] 人格 %s 额度已用完，跳过", p["persona_name"])
                    continue

                s, f = await self._process_persona_selfie(
                    p, wardrobe, style_pool, recent_styles, remaining, request_interval
                )
                total_success += s
                total_fail += f

        except asyncio.CancelledError:
            logger.info("[DailySelfie] 补画任务被取消")
        except Exception as e:
            logger.error("[DailySelfie] 补画任务异常: %s", e)
        finally:
            logger.info(
                "[DailySelfie] 补画完成: 成功=%d 失败=%d",
                total_success, total_fail,
            )

    def _get_persona_system_prompt(self, persona_name: str) -> str:
        try:
            persona_mgr = getattr(self.plugin.context, "persona_manager", None)
            if not persona_mgr:
                return ""
            if hasattr(persona_mgr, "get_persona_v3_by_id"):
                persona = persona_mgr.get_persona_v3_by_id(persona_name)
                if persona and isinstance(persona, dict):
                    return persona.get("prompt", "") or ""
            return ""
        except Exception as e:
            logger.warning("[DailySelfie] 获取人格 system prompt 失败: %s", e)
            return ""

    def _get_default_chat_provider_id(self) -> str | None:
        try:
            provider = self.plugin.context.get_using_provider()
            if provider:
                meta = provider.meta()
                if meta and getattr(meta, "id", None):
                    return str(meta.id).strip() or None
        except Exception:
            pass
        try:
            pm = getattr(self.plugin.context, "provider_manager", None)
            if pm and hasattr(pm, "provider_insts"):
                for p in pm.provider_insts:
                    try:
                        m = p.meta()
                        if m and getattr(m, "id", None):
                            return str(m.id).strip()
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    async def _process_persona_selfie(
        self,
        persona: dict,
        wardrobe: Any,
        style_pool: list[str],
        recent_styles: list[str],
        remaining: int,
        request_interval: int,
    ) -> tuple[int, int]:
        persona_name = persona["persona_name"]
        provider_id = persona["provider_id"]
        success = 0
        fail = 0

        logger.info("[DailySelfie] 开始处理人格 %s，剩余额度 %d", persona_name, remaining)

        chat_provider_id = self._get_default_chat_provider_id()
        if not chat_provider_id:
            logger.error("[DailySelfie] 无法获取默认 LLM Provider，跳过人格 %s", persona_name)
            return 0, 0

        persona_system_prompt = self._get_persona_system_prompt(persona_name)
        if not persona_system_prompt:
            logger.warning("[DailySelfie] 人格 %s 未找到 system prompt，使用空人格上下文", persona_name)

        queries = await self._llm_round1(chat_provider_id, persona_system_prompt, remaining, style_pool, recent_styles)
        if not queries:
            logger.warning("[DailySelfie] 人格 %s LLM第1轮未返回查询", persona_name)
            return 0, 0

        ref_results = await self._search_reference_images(queries, wardrobe, persona_name)
        if not ref_results:
            logger.warning("[DailySelfie] 人格 %s 未找到参考图", persona_name)
            return 0, 0

        batch_size = 5
        all_prompts: list[tuple[str, dict]] = []
        total_batches = (len(ref_results) + batch_size - 1) // batch_size
        style_summary = "\n".join(f"- {q}" for q in queries)

        for batch_num, batch_start in enumerate(range(0, len(ref_results), batch_size), 1):
            batch = ref_results[batch_start:batch_start + batch_size]
            descriptions = []
            valid_refs = []
            for r in batch:
                desc = r.get("description", "")
                if not desc:
                    continue
                strength = r.get("ref_strength", "style") or "style"
                if strength == "full":
                    guide = "请完整保留这张参考图的全部视觉细节，包括姿势动作、构图与服装"
                elif strength == "reimagine":
                    guide = "请仅提取这张参考图的服装款式信息，完全重新设计姿势与构图"
                else:
                    guide = "请保留这张参考图的服装与整体氛围，对姿势或构图做出明确的小变动"
                descriptions.append(f"{desc}\n指引：{guide}")
                valid_refs.append(r)
            if not descriptions:
                continue

            prompts = await self._llm_round2(
                chat_provider_id, persona_system_prompt, descriptions, len(descriptions),
                batch_num=batch_num, total_batch=total_batches,
                style_summary=style_summary,
            )
            for i, prompt in enumerate(prompts):
                if i < len(valid_refs):
                    all_prompts.append((prompt.strip(), valid_refs[i]))

        if not all_prompts:
            logger.warning("[DailySelfie] 人格 %s 未生成任何提示词", persona_name)
            return 0, 0

        for prompt, ref in all_prompts:
            cur_remaining = await self.counter.get_remaining(provider_id, persona["daily_limit"])
            if cur_remaining <= 0:
                logger.info("[DailySelfie] 人格 %s 额度用完，停止", persona_name)
                break

            if not prompt:
                fail += 1
                continue

            ref_image_path = ref.get("image_path", "")
            ref_strength = ref.get("ref_strength", "style")

            if not ref_image_path:
                fail += 1
                continue

            try:
                image_path = await self.plugin._generate_daily_selfie_image(
                    persona_name=persona_name,
                    prompt=prompt,
                    ref_image_path=ref_image_path,
                    ref_strength=ref_strength,
                    persona_conf=persona["config"],
                )
                if image_path:
                    success += 1
                    await self.counter.increment(provider_id)
                    logger.info("[DailySelfie] 人格 %s 补画成功 (%d/%d)", persona_name, success, remaining)
                    await self._save_to_wardrobe(image_path, persona_name)
                else:
                    fail += 1
                    logger.warning("[DailySelfie] 人格 %s 补画返回空路径", persona_name)
            except Exception as e:
                fail += 1
                logger.error("[DailySelfie] 人格 %s 生图失败: %s", persona_name, e)

            await asyncio.sleep(request_interval)

        return success, fail

    def _is_debug(self) -> bool:
        selfie_conf = self.plugin._get_feature("selfie")
        return bool(selfie_conf.get("daily_selfie_debug", False))

    async def _save_to_wardrobe(self, image_path: Path, persona_name: str) -> None:
        wardrobe = self.plugin._get_wardrobe_instance()
        if not wardrobe or not hasattr(wardrobe, "_save_image_from_bytes"):
            return
        try:
            import aiofiles
            async with aiofiles.open(image_path, "rb") as f:
                image_bytes = await f.read()
            if not image_bytes:
                return
            image_id, attrs, duplicate = await wardrobe._save_image_from_bytes(
                image_bytes, persona=persona_name, created_by="daily_selfie",
            )
            if duplicate:
                logger.debug("[DailySelfie] 补画图片已存在于衣橱，跳过: %s", image_id)
            elif image_id:
                logger.info("[DailySelfie] 补画图片已保存到衣橱: %s", image_id)
        except Exception as e:
            logger.debug("[DailySelfie] 补画图片保存到衣橱失败: %s", e)

    async def _llm_round1(
        self,
        chat_provider_id: str,
        persona_system_prompt: str,
        remaining: int,
        style_pool: list[str],
        recent_styles: list[str],
    ) -> list[str]:
        style_pool_text = "、".join(style_pool) if style_pool else "无可用风格"
        recent_text = "、".join(recent_styles) if recent_styles else "无"

        system_prompt = f"{persona_system_prompt}\n\n{_TASK_MODE_SYSTEM_PROMPT}" if persona_system_prompt else _TASK_MODE_SYSTEM_PROMPT

        user_prompt = _ROUND1_USER_PROMPT.format(
            remaining=remaining,
            style_pool=style_pool_text,
            recent_styles=recent_text,
        )

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round1] chat_provider_id=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                chat_provider_id, system_prompt, user_prompt,
            )

        try:
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return []

            if self._is_debug():
                logger.info(
                    "[DailySelfie][DEBUG][Round1] === LLM response ===\n%s",
                    text,
                )

            return _parse_llm_lines(text, remaining)
        except Exception as e:
            logger.error("[DailySelfie] LLM第1轮调用失败: %s", e)
            return []

    async def _llm_round2(
        self,
        chat_provider_id: str,
        persona_system_prompt: str,
        descriptions: list[str],
        count: int,
        *,
        batch_num: int = 1,
        total_batch: int = 1,
        style_summary: str = "",
    ) -> list[str]:
        desc_text = "\n".join(f"- {d}" for d in descriptions)

        system_prompt = (
            f"{persona_system_prompt}\n\n{_TASK_MODE_SYSTEM_PROMPT}\n\n{_SKILL_RULES_SYSTEM_PROMPT}"
            if persona_system_prompt
            else f"{_TASK_MODE_SYSTEM_PROMPT}\n\n{_SKILL_RULES_SYSTEM_PROMPT}"
        )

        user_prompt = _ROUND2_USER_PROMPT.format(
            batch_num=batch_num,
            total_batch=total_batch,
            descriptions=desc_text,
            count=count,
            style_summary=style_summary,
        )

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round2] batch=%d/%d chat_provider_id=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                batch_num, total_batch, chat_provider_id, system_prompt, user_prompt,
            )

        try:
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return []

            if self._is_debug():
                logger.info(
                    "[DailySelfie][DEBUG][Round2] batch=%d/%d === LLM response ===\n%s",
                    batch_num, total_batch, text,
                )

            return _parse_llm_lines(text, count)
        except Exception as e:
            logger.error("[DailySelfie] LLM第2轮调用失败: %s", e)
            return []

    async def _search_reference_images(
        self,
        queries: list[str],
        wardrobe: Any,
        persona_name: str = "",
    ) -> list[dict]:
        results = []
        used_ids: set[str] = set()

        for query in queries:
            try:
                if hasattr(wardrobe, "get_reference_image"):
                    ref = await wardrobe.get_reference_image(
                        query=query,
                        current_persona=persona_name,
                    )
                    if ref:
                        img_id = str(ref.get("image_id", ""))
                        if img_id and img_id not in used_ids:
                            used_ids.add(img_id)
                            results.append(ref)
            except Exception as e:
                logger.warning("[DailySelfie] 参考图搜索失败: query=%s error=%s", query[:50], e)

        return results

    async def _get_style_pool(self, wardrobe: Any) -> list[str]:
        try:
            db = getattr(wardrobe, "db", None)
            if not db:
                return []
            if not hasattr(db, "get_tag_distribution"):
                return []
            dist = await db.get_tag_distribution(persona="")
            styles = dist.get("style", {})
            return [s for s, c in styles.items() if c > 0]
        except Exception as e:
            logger.warning("[DailySelfie] 获取风格池失败: %s", e)
            return []

    async def _get_recent_styles(self, wardrobe: Any) -> list[str]:
        try:
            db = getattr(wardrobe, "db", None)
            if not db:
                return []
            if not hasattr(db, "list_images_lightweight"):
                return []
            three_days_ago = datetime.now() - timedelta(days=3)
            images = await db.list_images_lightweight(
                persona="", exclude_persona="",
                sort_by="created_at", limit=50,
            )
            styles: set[str] = set()
            for img in images:
                created_raw = str(img.get("created_at", "") or "")[:10]
                if created_raw:
                    try:
                        created_dt = datetime.strptime(created_raw, _DATE_FMT)
                        if created_dt < three_days_ago:
                            continue
                    except ValueError:
                        pass
                style_raw = img.get("style", "")
                if not style_raw:
                    continue
                try:
                    tags = json.loads(style_raw) if isinstance(style_raw, str) else style_raw
                except (json.JSONDecodeError, TypeError):
                    tags = [style_raw] if style_raw else []
                if isinstance(tags, str):
                    tags = [tags]
                elif isinstance(tags, dict):
                    tags = list(tags.values()) if tags.values() else list(tags.keys())
                if not isinstance(tags, list):
                    tags = [tags] if tags else []
                for t in tags:
                    t = str(t).strip()
                    if t:
                        styles.add(t)
            return list(styles)
        except Exception as e:
            logger.warning("[DailySelfie] 获取近期风格失败: %s", e)
            return []

    async def get_status(self) -> dict[str, Any]:
        personas = self._get_enabled_personas()
        counts = await self.counter.get_all_counts()
        status = {
            "date": self.counter.get_date(),
            "personas": [],
        }
        for p in personas:
            pid = p["provider_id"]
            used = counts.get(pid, 0)
            limit = p["daily_limit"]
            status["personas"].append({
                "persona_name": p["persona_name"],
                "provider_id": pid,
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
            })
        return status
