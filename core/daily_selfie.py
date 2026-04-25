from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("astrbot_plugin_aiimg.daily_selfie")

_DATE_FMT = "%Y-%m-%d"


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

    async def increment(self, provider_id: str, amount: int = 1) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            cur = int(counts.get(provider_id, 0))
            new_val = cur + amount
            counts[provider_id] = new_val
            self._save()
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


_SYSTEM_PROMPT_ROUND1 = (
    "你是一位正在准备今日写真拍摄的虚拟角色。根据提供的信息，选择你今天想要拍摄的服装风格和场景。\n\n"
    "规则：\n"
    "- 你需要选择 {remaining} 种不同的拍摄方案\n"
    "- 每种方案用一句自然语言描述，包含服装风格、场景和姿势（例如：在校园湖边穿着甜系洛丽塔裙优雅地散步）\n"
    "- 描述要具体，便于图库检索\n"
    "- 尽量选择不同风格，避免重复\n"
    "- 如果近期已选择过某些风格，请优先尝试其他风格，但如果你真的很想穿也可以\n\n"
    "可用风格池（仅列出衣橱中实际有图的风格）：\n{style_pool}\n\n"
    "近期已选择的风格（近3天）：\n{recent_styles}\n\n"
    "请直接返回 {remaining} 条自然语言描述，每条一行，不要编号，不要额外解释。"
)

_SYSTEM_PROMPT_ROUND2 = (
    "你是一位虚拟角色，正在为今日写真构建提示词。根据提供的参考图描述，为每张参考图构建一个用于AI绘画的提示词。\n\n"
    "规则：\n"
    "- 提示词应描述一张高质量的自拍照\n"
    "- 保持你的人格特征和气质\n"
    "- 参考图描述仅作为服装/姿势/场景的灵感来源\n"
    "- 每个提示词应该不同，体现不同的风格和场景\n"
    "- 提示词使用中文\n\n"
    "参考图描述：\n{descriptions}\n\n"
    "请直接返回 {count} 条提示词，每条一行，不要编号，不要额外解释。"
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

        queries = await self._llm_round1(persona_name, remaining, style_pool, recent_styles)
        if not queries:
            logger.warning("[DailySelfie] 人格 %s LLM第1轮未返回查询", persona_name)
            return 0, 0

        ref_results = await self._search_reference_images(queries, wardrobe)
        if not ref_results:
            logger.warning("[DailySelfie] 人格 %s 未找到参考图", persona_name)
            return 0, 0

        batch_size = 5
        prompt_idx = 0
        all_prompts: list[tuple[str, dict]] = []

        for batch_start in range(0, len(ref_results), batch_size):
            batch = ref_results[batch_start:batch_start + batch_size]
            descriptions = [r.get("description", "") for r in batch if r.get("description")]
            if not descriptions:
                continue

            prompts = await self._llm_round2(persona_name, descriptions, len(descriptions))
            for i, prompt in enumerate(prompts):
                if i < len(batch):
                    all_prompts.append((prompt.strip(), batch[i]))

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
                    logger.info("[DailySelfie] 人格 %s 补画成功 (%d/%d)", persona_name, success, remaining)
                else:
                    fail += 1
                    logger.warning("[DailySelfie] 人格 %s 补画返回空路径", persona_name)
            except Exception as e:
                fail += 1
                logger.error("[DailySelfie] 人格 %s 生图失败: %s", persona_name, e)

            await asyncio.sleep(request_interval)

        return success, fail

    async def _llm_round1(
        self,
        persona_name: str,
        remaining: int,
        style_pool: list[str],
        recent_styles: list[str],
    ) -> list[str]:
        style_pool_text = "、".join(style_pool) if style_pool else "无可用风格"
        recent_text = "、".join(recent_styles) if recent_styles else "无"

        system_prompt = _SYSTEM_PROMPT_ROUND1.format(
            remaining=remaining,
            style_pool=style_pool_text,
            recent_styles=recent_text,
        )

        user_prompt = f"你好，{persona_name}！今天还有 {remaining} 张写真额度，请选择你今天想拍的风格吧。"

        try:
            resp = await self.plugin.context.llm_generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return []
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            return lines[:remaining]
        except Exception as e:
            logger.error("[DailySelfie] LLM第1轮调用失败: %s", e)
            return []

    async def _llm_round2(
        self,
        persona_name: str,
        descriptions: list[str],
        count: int,
    ) -> list[str]:
        desc_text = "\n".join(f"- {d}" for d in descriptions)

        system_prompt = _SYSTEM_PROMPT_ROUND2.format(
            descriptions=desc_text,
            count=count,
        )

        user_prompt = f"{persona_name}，请根据以上参考图描述，为每张图构建一个自拍提示词。"

        try:
            resp = await self.plugin.context.llm_generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return []
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            return lines[:count]
        except Exception as e:
            logger.error("[DailySelfie] LLM第2轮调用失败: %s", e)
            return []

    async def _search_reference_images(
        self,
        queries: list[str],
        wardrobe: Any,
    ) -> list[dict]:
        results = []
        used_ids: set[str] = set()

        for query in queries:
            try:
                if hasattr(wardrobe, "get_reference_image"):
                    ref = await wardrobe.get_reference_image(
                        query=query,
                        current_persona="",
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
            import aiosqlite
            async with aiosqlite.connect(db.db_path) as conn:
                sql = "SELECT style FROM images WHERE persona = ''"
                cursor = await conn.execute(sql)
                rows = await cursor.fetchall()
            style_counts: dict[str, int] = {}
            for (style_raw,) in rows:
                if not style_raw:
                    continue
                try:
                    tags = json.loads(style_raw) if isinstance(style_raw, str) else [style_raw]
                except (json.JSONDecodeError, TypeError):
                    tags = [style_raw] if style_raw else []
                if isinstance(tags, str):
                    tags = [tags]
                for t in tags:
                    t = str(t).strip()
                    if t:
                        style_counts[t] = style_counts.get(t, 0) + 1
            return [s for s, c in style_counts.items() if c > 0]
        except Exception as e:
            logger.warning("[DailySelfie] 获取风格池失败: %s", e)
            return []

    async def _get_recent_styles(self, wardrobe: Any) -> list[str]:
        try:
            db = getattr(wardrobe, "db", None)
            if not db:
                return []
            three_days_ago = (datetime.now() - timedelta(days=3)).strftime(_DATE_FMT)
            import aiosqlite
            async with aiosqlite.connect(db.db_path) as conn:
                sql = (
                    "SELECT style FROM images "
                    "WHERE persona = '' AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 50"
                )
                cursor = await conn.execute(sql, [three_days_ago])
                rows = await cursor.fetchall()
            styles: set[str] = set()
            for (style_raw,) in rows:
                if not style_raw:
                    continue
                try:
                    tags = json.loads(style_raw) if isinstance(style_raw, str) else [style_raw]
                except (json.JSONDecodeError, TypeError):
                    tags = [style_raw] if style_raw else []
                if isinstance(tags, str):
                    tags = [tags]
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
