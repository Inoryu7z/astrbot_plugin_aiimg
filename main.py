"""
Gitee AI 图像生成插件

功能:
- 文生图 (z-image-turbo)
- 图生图/改图 (Gemini / Gitee 千问，可切换)
- Bot 自拍（参考照）：上传参考人像后用改图模型生成自拍
- 视频生成 (Grok imagine, 参考图 + 提示词)
- 预设提示词
- 智能降级
"""

import asyncio
import base64
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    At,
    AtAll,
    File,
    Image,
    Plain,
    Reply,
    Video,
)
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .core.debouncer import Debouncer
from .core.draw_service import ImageDrawService
from .core.edit_router import EditRouter
from .core.emoji_feedback import mark_failed, mark_processing, mark_success
from .core.gitee_sizes import (
    GITEE_SUPPORTED_RATIOS,
    normalize_size_text,
    resolve_ratio_size,
)
from .core.image_format import guess_image_mime_and_ext
from .core.image_manager import ImageManager
from .core.nanobanana import NanoBananaService
from .core.provider_registry import ProviderRegistry
from .core.ref_store import ReferenceStore
from .core.utils import close_session, get_images_from_event
from .core.video_manager import VideoManager


@dataclass(slots=True)
class SendImageResult:
    ok: bool
    reason: str = ""
    cached_path: Path | None = None
    used_fallback: bool = False
    last_error: str = ""

    def __bool__(self) -> bool:
        return self.ok


class GiteeAIImagePlugin(Star):
    """Gitee AI 图像生成插件"""

    # Gitee AI 支持的图片比例
    SUPPORTED_RATIOS: dict[str, list[str]] = GITEE_SUPPORTED_RATIOS
    IMAGE_AS_FILE_THRESHOLD_BYTES: int = 20 * 1024 * 1024

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_aiimg")
        self._legacy_data_dir = StarTools.get_data_dir("astrbot_plugin_gitee_aiimg")
        # 单用户场景，无需清理；每用户仅保留最近一条记录。
        self._last_image_by_user: dict[str, dict] = {}
        # 缓存 wardrobe preview 结果，避免 _generate_selfie_image 重复调用 wardrobe。
        # 格式：{user_id: {"image_path": str, "description": str, "persona": str, "image_id": str}}
        self._wardrobe_preview_cache: dict[str, dict] = {}

    async def _call_native_poke(self, event: AstrMessageEvent, target_id: str) -> bool:
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False

        user_id: int | str = int(target_id) if target_id.isdigit() else target_id
        try:
            await bot.call_action("friend_poke", user_id=user_id)
            return True
        except Exception as exc:
            logger.warning(
                "[GiteeAIImagePlugin] friend_poke failed: target=%s err=%s",
                target_id,
                exc,
            )

        try:
            await bot.call_action("send_poke", user_id=user_id)
            return True
        except Exception as exc:
            logger.warning(
                "[GiteeAIImagePlugin] send_poke failed: target=%s err=%s",
                target_id,
                exc,
            )
            return False

    async def _signal_llm_tool_failure(self, event: AstrMessageEvent) -> None:
        if event.is_private_chat():
            target_id = str(event.get_sender_id() or "").strip()
            if target_id:
                if await self._call_native_poke(event, target_id):
                    return
        await mark_failed(event)

    async def initialize(self):
        self.debouncer = Debouncer(self.config)
        self.imgr = ImageManager(self.config, self.data_dir)
        self.registry = ProviderRegistry(
            self.config, imgr=self.imgr, data_dir=self.data_dir
        )
        for err in self.registry.validate():
            logger.warning("[GiteeAIImagePlugin][config] %s", err)

        self.draw = ImageDrawService(
            self.config, self.imgr, self.data_dir, registry=self.registry
        )
        self.edit = EditRouter(
            self.config, self.imgr, self.data_dir, registry=self.registry
        )
        self.nb = NanoBananaService(self.config, self.imgr)
        self.refs = ReferenceStore(self.data_dir)
        self._migrate_legacy_data()
        self.videomgr = VideoManager(self.config, self.data_dir)

        self._concurrency_lock = asyncio.Lock()
        self._image_inflight: dict[str, int] = {}
        self._video_inflight: dict[str, int] = {}
        self._video_tasks: set[asyncio.Task] = set()
        self._image_tasks: set[asyncio.Task] = set()

        self._patch_tool_image_cache_runtime()

        # 动态注册预设命令 (方案C: /手办化 直接触发)
        self._register_preset_commands()

        # 每日补画服务
        from .core.daily_selfie import DailySelfieService
        self.daily_selfie = DailySelfieService(self)
        await self.daily_selfie.start()

        logger.info(
            f"[GiteeAIImagePlugin] 插件初始化完成: "
            f"改图后端={self.edit.get_available_backends()}, "
            f"改图预设={len(self.edit.get_preset_names())}个, "
            f"视频启用={bool(self._get_feature('video').get('enabled', False))}, "
            f"视频预设={len(self._get_video_presets())}个"
        )

        self._inject_provider_list_to_tool_doc()

    def _inject_provider_list_to_tool_doc(self):
        labels = self.registry.provider_labels()
        if not labels:
            return
        entries = []
        for pid, lbl in labels.items():
            entries.append(f"- {lbl}")
        provider_block = (
            "\n可用后端列表（backend 参数可选值，填显示名称即可）：\n"
            + "\n".join(entries)
            + "\n注意：除非用户明确要求使用特定后端（如提到后端名称），否则永远填 auto。"
        )
        from astrbot.core.provider.register import llm_tools
        tool_names = ("aiimg_generate", "aiimg_draw", "aiimg_edit", "aiimg_video")
        for name in tool_names:
            func_tool = llm_tools.get_func(name)
            if func_tool and func_tool.description:
                func_tool.description += provider_block

    def _migrate_legacy_data(self):
        import shutil as _shutil
        legacy = self._legacy_data_dir
        current = self.data_dir
        if not legacy.exists():
            return
        if legacy.resolve() == current.resolve():
            return
        migrated = False
        legacy_refs = legacy / "refs"
        current_refs = current / "refs"
        if legacy_refs.exists() and not current_refs.exists():
            try:
                _shutil.copytree(str(legacy_refs), str(current_refs))
                logger.info("[aiimg] 已迁移旧参考照数据: %s -> %s", legacy_refs, current_refs)
                migrated = True
            except Exception as e:
                logger.warning("[aiimg] 迁移旧参考照数据失败: %s", e)
        legacy_images = legacy / "images"
        current_images = current / "images"
        if legacy_images.exists() and not current_images.exists():
            try:
                _shutil.copytree(str(legacy_images), str(current_images))
                logger.info("[aiimg] 已迁移旧图片数据: %s -> %s", legacy_images, current_images)
                migrated = True
            except Exception as e:
                logger.warning("[aiimg] 迁移旧图片数据失败: %s", e)
        if migrated:
            self.refs = ReferenceStore(self.data_dir)

    def _remember_last_image(self, event: AstrMessageEvent, image_path: Path, mode: str = "") -> None:
        try:
            user_id = str(event.get_sender_id() or "")
        except Exception:
            user_id = ""
        if not user_id:
            return
        self._last_image_by_user[user_id] = {"path": Path(image_path), "mode": mode}

    @staticmethod
    def _as_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
                return True
            if v in {"0", "false", "no", "n", "off", "disable", "disabled", ""}:
                return False
        return default

    def _patch_tool_image_cache_runtime(self) -> None:
        try:
            from astrbot.core.agent import tool_image_cache as cache_module
        except Exception as exc:
            logger.debug("[GiteeAIImagePlugin] skip tool image cache runtime patch: %s", exc)
            return

        cache_cls = getattr(cache_module, "ToolImageCache", None)
        cache_obj = getattr(cache_module, "tool_image_cache", None)
        cached_image_cls = getattr(cache_module, "CachedImage", None)
        if cache_cls is None or cache_obj is None or cached_image_cls is None:
            return
        if getattr(cache_cls, "_gitee_aiimg_runtime_patch", False):
            return

        def _patched_save_image(
                cache_self,
                base64_data: str,
                tool_call_id: str,
                tool_name: str,
                index: int = 0,
                mime_type: str = "image/png",
        ):
            ext = cache_self._get_file_extension(mime_type)
            cache_dir_value = str(getattr(cache_self, "_cache_dir", "") or "").strip()
            cache_dir = (
                Path(cache_dir_value)
                if cache_dir_value
                else Path(get_astrbot_temp_path())
                     / getattr(cache_self, "CACHE_DIR_NAME", "tool_images")
            )
            file_path = cache_dir / f"{tool_call_id}_{index}{ext}"

            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                image_bytes = base64.b64decode(base64_data)
                file_path.write_bytes(image_bytes)
            except Exception as exc:
                logger.error(f"Failed to save tool image: {exc}")
                raise

            cache_self._cache_dir = str(cache_dir)
            logger.debug(
                "[GiteeAIImagePlugin] tool image cache runtime patch wrote: %s", file_path
            )
            return cached_image_cls(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                file_path=str(file_path),
                mime_type=mime_type,
            )

        cache_cls.save_image = _patched_save_image
        cache_cls._gitee_aiimg_runtime_patch = True
        cache_obj._cache_dir = str(
            Path(get_astrbot_temp_path())
            / getattr(cache_cls, "CACHE_DIR_NAME", "tool_images")
        )
        Path(cache_obj._cache_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            "[GiteeAIImagePlugin] tool image cache runtime patch active: %s",
            cache_obj._cache_dir,
        )

    def _get_max_user_concurrency(self) -> int:
        v = self._as_int(self.config.get("max_user_concurrency", 2), default=2)
        return max(1, min(10, v))

    def _get_max_user_video_concurrency(self) -> int:
        v = self._as_int(self.config.get("max_user_video_concurrency", 1), default=1)
        return max(1, min(5, v))

    def _debounce_key(self, event: AstrMessageEvent, prefix: str, user_id: str) -> str:
        """尽量用消息维度去重，避免同用户短时间内无法并发提交多条任务。"""
        mid = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if mid and origin:
            return f"{prefix}:{origin}:{mid}"
        return f"{prefix}:{user_id}"

    async def _begin_user_job(self, user_id: str, *, kind: str) -> bool:
        user_id = str(user_id or "").strip()
        if not user_id:
            return True

        if kind == "video":
            limit = self._get_max_user_video_concurrency()
            store = self._video_inflight
        else:
            limit = self._get_max_user_concurrency()
            store = self._image_inflight

        async with self._concurrency_lock:
            cur = int(store.get(user_id, 0) or 0)
            if cur >= limit:
                return False
            store[user_id] = cur + 1
            return True

    async def _end_user_job(self, user_id: str, *, kind: str) -> None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return

        store = self._video_inflight if kind == "video" else self._image_inflight
        async with self._concurrency_lock:
            cur = int(store.get(user_id, 0) or 0)
            if cur <= 1:
                store.pop(user_id, None)
            else:
                store[user_id] = cur - 1

    @staticmethod
    def _is_rich_media_transfer_failed(exc: Exception | None) -> bool:
        if exc is None:
            return False
        msg = f"{exc!r} {exc}".lower()
        return "rich media transfer failed" in msg

    @staticmethod
    def _build_compact_image_bytes(
            image_path: Path, *, max_side: int = 2048, target_bytes: int = 3_500_000
    ) -> bytes | None:
        """Build a smaller JPEG variant for platforms that reject large rich-media upload."""
        try:
            from PIL import Image as PILImage
        except Exception:
            return None

        try:
            with PILImage.open(image_path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")

                w, h = im.size
                if max(w, h) > max_side:
                    ratio = float(max_side) / float(max(w, h))
                    nw = max(1, int(w * ratio))
                    nh = max(1, int(h * ratio))
                    resampling = getattr(
                        getattr(PILImage, "Resampling", PILImage), "LANCZOS"
                    )
                    im = im.resize((nw, nh), resampling)

                for q in (88, 82, 76, 70, 64):
                    buf = io.BytesIO()
                    im.save(
                        buf,
                        format="JPEG",
                        quality=q,
                        optimize=True,
                        progressive=True,
                    )
                    data = buf.getvalue()
                    if data and (len(data) <= target_bytes or q == 64):
                        return data
        except Exception:
            return None
        return None

    @staticmethod
    def _compress_for_llm_context(
            image_path: Path, max_side: int = 2048, quality: int = 85
    ) -> bytes | None:
        """
        为LLM上下文压缩图片（保持比例，限制最大边长）。

        Args:
            image_path: 原图路径
            max_side: 最大边长（默认2048，适配模型输入限制）
            quality: JPEG质量（默认85）

        Returns:
            压缩后的JPEG字节流，失败返回None
        """
        try:
            from PIL import Image as PILImage
        except Exception:
            return None

        try:
            with PILImage.open(image_path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")

                w, h = im.size
                if w <= 0 or h <= 0:
                    return None

                # 如果尺寸合规，直接返回原图
                if w <= max_side and h <= max_side:
                    # 如果是JPEG格式，直接读取原文件
                    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
                        try:
                            return image_path.read_bytes()
                        except Exception:
                            pass
                    # 否则转换为JPEG
                    out = io.BytesIO()
                    im.save(out, format="JPEG", quality=quality)
                    return out.getvalue()

                # 等比例缩放
                scale = min(max_side / w, max_side / h)
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))

                resampling = getattr(
                    getattr(PILImage, "Resampling", PILImage), "LANCZOS"
                )
                im_resized = im.resize((new_w, new_h), resampling)

                # 保存为JPEG
                out = io.BytesIO()
                im_resized.save(out, format="JPEG", quality=quality, optimize=True)
                return out.getvalue()
        except Exception as e:
            logger.warning(
                "[compress_for_llm] 图片压缩失败: path=%s, err=%s", image_path, e
            )
            return None

    def _register_preset_commands(self):
        """动态注册预设命令

        为每个预设创建对应的命令，如 /手办化, /Q版化 等
        """
        preset_names = self.edit.get_preset_names()
        if not preset_names:
            return

        for preset_name in preset_names:
            # 创建闭包捕获 preset_name
            self._create_and_register_preset_handler(preset_name)

        logger.info(f"[GiteeAIImagePlugin] 已注册 {len(preset_names)} 个预设命令")

    def _create_and_register_preset_handler(self, preset_name: str):
        """为单个预设创建并注册命令处理器

        支持: /手办化 [额外提示词]
        例如: /手办化 加点金色元素
        """

        # 默认后端命令: /手办化
        async def preset_handler(event: AstrMessageEvent):
            # 提取命令后的额外提示词
            extra_prompt = self._extract_extra_prompt(event, preset_name)
            await self._do_edit_direct(event, extra_prompt, preset=preset_name)

        preset_handler.__name__ = f"preset_{preset_name}"
        preset_handler.__doc__ = f"预设改图: {preset_name} [额外提示词]"

        self.context.register_commands(
            star_name="astrbot_plugin_aiimg",
            command_name=preset_name,
            desc=f"预设改图: {preset_name}",
            priority=5,
            awaitable=preset_handler,
        )

    def _extract_extra_prompt(self, event: AstrMessageEvent, command_name: str) -> str:
        """从消息中提取命令后的额外提示词

        支持格式:
        - /手办化 加点金色元素 -> "加点金色元素"
        - /手办化@张三 背景是星空 -> "背景是星空"
        - /手办化@张三@李四 背景是星空 -> "背景是星空"

        注意: message_str 中 @用户 会被替换为空格或移除
        """
        msg = event.message_str.strip()
        # 移除命令前缀 (/, !, ., 等)
        # 兼容唤醒前缀：.视频 / 。视频 / ．视频
        if msg and msg[0] in "/!！.。．":
            msg = msg[1:]
        # 移除命令名
        if msg.startswith(command_name):
            msg = msg[len(command_name):]
        # 清理多余空格
        return msg.strip()

    @staticmethod
    def _extract_command_arg_anywhere(message: str, command_name: str) -> str:
        """从任意位置提取“/命令 参数”，用于图片在前导致 @filter.command 不触发的场景。"""
        msg = (message or "").strip()
        if not msg:
            return ""
        for prefix in "/!！.。．":
            token = f"{prefix}{command_name}"
            idx = msg.find(token)
            if idx >= 0:
                return msg[idx + len(token):].strip()
        return ""

    def _extract_command_arg_from_chain(
            self, event: AstrMessageEvent, command_name: str
    ) -> tuple[bool, str]:
        """从消息链中提取命令后的提示词。

        用于修复“/命令 + 图片 + 文本”时，平台把文本段无空格拼接到 `message_str`
        导致 command filter 和字符串提取都失效的问题。
        """
        try:
            chain = event.get_messages()
        except Exception:
            return False, ""

        found = False
        parts: list[str] = []
        for seg in chain:
            if isinstance(seg, (At, AtAll, Reply)):
                continue

            if not found:
                if not isinstance(seg, Plain):
                    continue
                plain = str(getattr(seg, "text", "") or "").lstrip()
                if not plain:
                    continue
                if plain[0] in "/!！.。．":
                    plain = plain[1:]
                if not plain.startswith(command_name):
                    continue
                found = True
                tail = plain[len(command_name):].strip()
                if tail:
                    parts.append(tail)
                continue

            if isinstance(seg, Plain):
                text = str(getattr(seg, "text", "") or "").strip()
                if text:
                    parts.append(text)

        return found, " ".join(parts).strip()

    def _extract_chain_provider_id(self, item: object) -> str:
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            return ""
        return str(
            item.get("provider_id")
            or item.get("id")
            or item.get("provider")
            or item.get("backend")
            or ""
        ).strip()

    def _normalize_chain_item(self, item: object) -> dict | None:
        pid = self._extract_chain_provider_id(item)
        if not pid:
            return None
        out = ""
        if isinstance(item, dict):
            out = str(item.get("output") or item.get("default_output") or "").strip()
        return {"provider_id": pid, "output": out} if out else {"provider_id": pid}

    def _parse_provider_override_prefix(self, text: str) -> tuple[str | None, str]:
        """仅当 @token 命中已配置 provider_id 时，才作为 provider 覆盖。"""
        s = (text or "").strip()
        if not s.startswith("@"):
            return None, s
        first, _, rest = s.partition(" ")
        candidate = first.lstrip("@").strip()
        if not candidate:
            return None, s
        if candidate in set(self.registry.provider_ids()):
            return candidate, rest.strip()
        logger.debug(
            "[provider_override] 忽略未知 @token，继续走自动链路: token=%s",
            candidate,
        )
        return None, s

    @staticmethod
    def _plain_starts_with_command(text: str, command_name: str) -> bool:
        plain = (text or "").lstrip()
        if not plain:
            return False
        for prefix in "/!！.。．":
            if plain.startswith(f"{prefix}{command_name}"):
                return True
        return False

    def _is_direct_command_message(
            self, event: AstrMessageEvent, command_names: tuple[str, ...]
    ) -> bool:
        """仅当“首个有效文本段”直接是命令时返回 True。

        用于 regex 兜底去重：避免正常 /命令 被重复处理；
        同时允许“图片在前、命令在后”的消息继续走兜底逻辑。
        """
        try:
            chain = event.get_messages()
        except Exception:
            return False
        if not chain:
            return False

        first_plain = ""
        for seg in chain:
            if isinstance(seg, (At, AtAll, Reply)):
                continue
            if isinstance(seg, Plain):
                first_plain = str(getattr(seg, "text", "") or "")
            break

        if not first_plain:
            return False
        return any(
            self._plain_starts_with_command(first_plain, name) for name in command_names
        )

    @staticmethod
    def _is_framework_direct_command_text(
            message: str, command_names: tuple[str, ...], *, allow_bare: bool = True
    ) -> bool:
        """按 AstrBot CommandFilter 的文本规则判断是否可直接命中 command handler。"""
        plain = " ".join(str(message or "").strip().split())
        if not plain:
            return False
        if plain[0] in "/!！.。．":
            plain = plain[1:].lstrip()
        return any(
            (plain == name if allow_bare else False) or plain.startswith(f"{name} ")
            for name in command_names
        )

    async def terminate(self):
        self.debouncer.clear_all()
        try:
            tasks = list(getattr(self, "_video_tasks", []))
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        try:
            tasks = list(getattr(self, "_image_tasks", []))
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        if hasattr(self, "daily_selfie") and self.daily_selfie:
            await self.daily_selfie.stop()
        await self.imgr.close()
        await self.draw.close()
        await self.nb.close()
        await close_session()  # 关闭 utils.py 的 HTTP 会话

    # ==================== 文生图 ====================

    @filter.command("aiimg", alias={"文生图", "生图", "画图", "绘图", "出图"})
    async def generate_image_command(self, event: AstrMessageEvent, prompt: str):
        """生成图片指令

        用法: /aiimg [@provider_id] <提示词> [比例]
        示例: /aiimg 一个女孩 9:16
        支持比例: 1:1, 4:3, 3:4, 3:2, 2:3, 16:9, 9:16
        """
        event.should_call_llm(True)
        # 解析参数
        arg = event.message_str.partition(" ")[2]
        if not arg:
            await mark_failed(event)
            return
        provider_override: str | None = None
        provider_override, arg = self._parse_provider_override_prefix(arg)
        if not arg:
            await mark_failed(event)
            return

        prompt = arg.strip()
        size: str | None = None
        parts = arg.split()
        if parts and parts[-1] in self.SUPPORTED_RATIOS:
            ratio = parts[-1]
            prompt = " ".join(parts[:-1]).strip()
            size = self._resolve_ratio_size(ratio)

        if not prompt:
            await mark_failed(event)
            return

        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "generate", user_id)

        # 防抖检查
        if self.debouncer.hit(request_id):
            await mark_failed(event)
            return

        if not await self._begin_user_job(user_id, kind="image"):
            await mark_failed(event)
            return

        try:
            # 标记处理中
            await mark_processing(event)
            t_start = time.perf_counter()
            image_path = await self.draw.generate(
                prompt, size=size, provider_id=provider_override
            )
            t_end = time.perf_counter()

            self._remember_last_image(event, image_path)
            sent = await self._send_image_with_fallback(event, image_path)
            if not sent:
                await mark_failed(event)
                logger.warning(
                    "[文生图] 图片发送失败，已仅使用表情标注: reason=%s", sent.reason
                )
                return

            # 标记成功
            await mark_success(event)
            logger.info(
                f"[文生图] 完成: {prompt[:30] if prompt else '文生图'}..., 耗时={t_end - t_start:.2f}s"
            )

        except Exception as e:
            logger.error(f"[文生图] 失败: {e}")
            await mark_failed(event)
        finally:
            await self._end_user_job(user_id, kind="image")

    # ==================== 图生图/改图 ====================

    @filter.command("aiedit", alias={"图生图", "改图", "修图"})
    async def edit_image_default(self, event: AstrMessageEvent, prompt: str):
        """使用默认后端改图

        用法: /aiedit <提示词>
        需要同时发送或引用图片
        """
        event.should_call_llm(True)
        await self._do_edit(event, prompt, backend=None)

    @filter.command("重发图片")
    async def resend_last_image(self, event: AstrMessageEvent):
        """重发最近一次生成/改图的图片（不重新生成，不消耗次数）。"""
        user_id = str(event.get_sender_id() or "")
        entry = self._last_image_by_user.get(user_id)
        if not entry:
            await mark_failed(event)
            return
        p = entry.get("path") if isinstance(entry, dict) else entry
        if not p or not Path(p).exists():
            await mark_failed(event)
            return
        ok = await self._send_image_with_fallback(event, p)
        if ok:
            await mark_success(event)
        else:
            await mark_failed(event)

    @filter.regex(r"(?:[/!！.。．])?(改图|图生图|修图|aiedit)", priority=-10)
    async def edit_image_regex_fallback(self, event: AstrMessageEvent):
        """兼容“图片在前、文字在后”的消息：确保 /改图 能触发。"""
        msg = (event.message_str or "").strip()
        command_names = ("改图", "图生图", "修图", "aiedit")
        if self._is_framework_direct_command_text(msg, command_names, allow_bare=False):
            return
        try:
            if not await self._has_message_images(event):
                return
        except Exception:
            return

        prompt = ""
        matched = False
        for name in command_names:
            prompt = self._extract_command_arg_anywhere(msg, name)
            found_in_chain, chain_prompt = self._extract_command_arg_from_chain(
                event, name
            )
            if prompt or found_in_chain:
                matched = True
                if not prompt:
                    prompt = chain_prompt
                break
        if matched:
            event.should_call_llm(True)
            await self._do_edit(event, prompt, backend=None)
            event.stop_event()

    @filter.regex(r"[/!！.。．][^\s]+", priority=-10)
    async def preset_regex_fallback(self, event: AstrMessageEvent):
        """兼容“图片在前、预设命令在后”的消息：确保 /<预设名> 能触发。"""
        msg = (event.message_str or "").strip()
        preset_names = self.edit.get_preset_names()
        if not preset_names:
            return

        # 如果首段文本本来就是 /预设，则交给 command handler，避免重复处理
        try:
            if self._is_direct_command_message(event, tuple(preset_names)):
                return
        except Exception:
            pass

        # 仅当消息/引用里确实带图（不含头像兜底）时才兜底，避免误伤其它插件命令
        try:
            if not await self._has_message_images(event):
                return
        except Exception:
            return

        # 在任意位置找到第一个匹配的预设命令
        used_preset: str | None = None
        for name in preset_names:
            for prefix in "/!！.。．":
                if f"{prefix}{name}" in msg:
                    used_preset = name
                    break
            if used_preset:
                break

        if not used_preset:
            return

        extra_prompt = self._extract_command_arg_anywhere(msg, used_preset)
        await self._do_edit_direct(event, extra_prompt, preset=used_preset)
        event.stop_event()

    # ==================== Bot 自拍（参考照） ====================

    @filter.command("自拍")
    async def selfie_command(self, event: AstrMessageEvent):
        """使用"自拍参考照"生成 Bot 自拍。

        用法:
        - /自拍 <提示词> [比例]
        - 可附带多张参考图（衣服/姿势/场景）作为额外参考
        - 支持比例: 1:1, 4:3, 3:4, 3:2, 2:3, 16:9, 9:16
        示例: /自拍 可爱女孩 9:16
        """
        event.should_call_llm(True)
        prompt = self._extract_extra_prompt(event, "自拍")
        await self._do_selfie(event, prompt, backend=None)

    @filter.regex(r"[/!！.。．]自拍(\s|$)", priority=-10)
    async def selfie_regex_fallback(self, event: AstrMessageEvent):
        """兼容“图片在前、文字在后”的消息：确保 /自拍 能触发。"""
        msg = (event.message_str or "").strip()
        # 如果本来就是“首段文本命令”，交给 command handler，避免重复回复
        if self._is_direct_command_message(event, ("自拍",)):
            return
        prompt = self._extract_command_arg_anywhere(msg, "自拍")
        has_selfie_cmd = any(
            msg.startswith(f"{prefix}自拍") for prefix in "/!！.。．"
        )
        if prompt or has_selfie_cmd:
            if not self._is_selfie_enabled():
                await mark_failed(event)
                event.stop_event()
                return
            await self._do_selfie(event, prompt, backend=None)
            event.stop_event()

    @filter.command("自拍参考")
    async def selfie_reference_command(self, event: AstrMessageEvent):
        """管理自拍参考照（建议仅管理员使用）。

        用法:
        - 发送图片 + /自拍参考 设置
        - /自拍参考 查看
        - /自拍参考 删除
        - /自拍参考 设置 全局
        - /自拍参考 查看 全局
        - /自拍参考 删除 全局
        """
        event.should_call_llm(True)
        arg = self._extract_extra_prompt(event, "自拍参考")
        action, _, rest = (arg or "").strip().partition(" ")
        action = action.strip().lower()
        rest = rest.strip()

        use_global = rest.lower() in {"全局", "global", "default"}
        persona_name = None if use_global else await self._get_current_persona_name(event)

        if not action or action in {"帮助", "help", "h"}:
            persona_hint = f"\n当前人格：{persona_name}" if persona_name else "\n当前无人格绑定"
            msg = (
                "📸 自拍参考照\n"
                "━━━━━━━━━━━━━━\n"
                "设置：发送图片 + /自拍参考 设置\n"
                "查看：/自拍参考 查看\n"
                "删除：/自拍参考 删除\n"
                "━━━━━━━━━━━━━━\n"
                "加「全局」操作全局参考照：\n"
                "/自拍参考 设置 全局\n"
                "/自拍参考 查看 全局\n"
                "/自拍参考 删除 全局\n"
                "━━━━━━━━━━━━━━\n"
                "生成自拍：/自拍 <提示词>\n"
                "可附带额外参考图（衣服/姿势/场景）"
                f"{persona_hint}"
            )
            yield event.plain_result(msg)
            return

        if action in {"设置", "set"}:
            await self._set_selfie_reference(event, persona_name=persona_name)
            return

        if action in {"查看", "show", "看"}:
            async for result in self._show_selfie_reference(event, persona_name=persona_name):
                yield result
            return

        if action in {"删除", "del", "delete"}:
            await self._delete_selfie_reference(event, persona_name=persona_name)
            return

        await mark_failed(event)

    @filter.regex(r"[/!！.。．]自拍参考(\s|$)", priority=-10)
    async def selfie_reference_regex_fallback(self, event: AstrMessageEvent):
        """兼容“图片在前、文字在后”的消息：确保 /自拍参考 能触发。"""
        msg = (event.message_str or "").strip()
        if self._is_direct_command_message(event, ("自拍参考",)):
            return
        arg = self._extract_command_arg_anywhere(msg, "自拍参考")
        action, _, rest = (arg or "").strip().partition(" ")
        action = action.strip().lower()
        rest = rest.strip()

        use_global = rest.lower() in {"全局", "global", "default"}
        persona_name = None if use_global else await self._get_current_persona_name(event)

        if not action or action in {"帮助", "help", "h"}:
            persona_hint = f"\n当前人格：{persona_name}" if persona_name else "\n当前无人格绑定"
            yield event.plain_result(
                "📸 自拍参考照\n"
                "━━━━━━━━━━━━━━\n"
                "设置：发送图片 + /自拍参考 设置\n"
                "查看：/自拍参考 查看\n"
                "删除：/自拍参考 删除\n"
                "━━━━━━━━━━━━━━\n"
                "加「全局」操作全局参考照：\n"
                "/自拍参考 设置 全局\n"
                "/自拍参考 查看 全局\n"
                "/自拍参考 删除 全局\n"
                "━━━━━━━━━━━━━━\n"
                "生成自拍：/自拍 <提示词>\n"
                "可附带额外参考图（衣服/姿势/场景）"
                f"{persona_hint}"
            )
            event.stop_event()
            return

        if action in {"设置", "set"}:
            await self._set_selfie_reference(event, persona_name=persona_name)
            event.stop_event()
            return

        if action in {"查看", "show", "看"}:
            async for r in self._show_selfie_reference(event, persona_name=persona_name):
                yield r
            event.stop_event()
            return

        if action in {"删除", "del", "delete"}:
            await self._delete_selfie_reference(event, persona_name=persona_name)
            event.stop_event()
            return

        await mark_failed(event)
        event.stop_event()

    # ==================== 每日补画 ====================

    @filter.command("补画")
    async def daily_selfie_command(self, event: AstrMessageEvent):
        """手动触发每日补画，自动计算缺口并补画。

        用法:
        - /补画
        """
        event.should_call_llm(True)
        if not hasattr(self, "daily_selfie") or not self.daily_selfie:
            yield event.plain_result("补画功能未启用。请先在配置中开启人格的每日补画。")
            return
        yield event.plain_result("⏳ 补画任务已启动...")
        persona_name = await self._get_current_persona_name(event)
        await self.daily_selfie.run_daily_selfie(persona_name=persona_name or "")

    @filter.command("补画状态")
    async def daily_selfie_status_command(self, event: AstrMessageEvent):
        """查看每日补画状态，包括各提供商的已用/剩余额度。

        用法:
        - /补画状态
        """
        if not hasattr(self, "daily_selfie") or not self.daily_selfie:
            yield event.plain_result("补画功能未启用。")
            return
        status = await self.daily_selfie.get_status()
        lines = [f"📅 日期：{status['date']}"]
        if not status["personas"]:
            lines.append("暂无启用补画的人格。")
        for p in status["personas"]:
            lines.append(
                f"👤 {p['persona_name']} | 提供商: {p['provider_id']} | "
                f"已用: {p['used']}/{p['limit']} | 剩余: {p['remaining']}"
            )
        yield event.plain_result("\n".join(lines))

    # ==================== 视频生成 ====================

    @filter.command("视频")
    async def generate_video_command(self, event: AstrMessageEvent):
        """生成视频

        用法:
        - /视频 [@provider_id] <提示词>
        - /视频 [@provider_id] <预设名> [额外提示词]
        """
        event.should_call_llm(True)
        if not bool(self._get_feature("video").get("enabled", False)):
            await mark_failed(event)
            return
        arg = self._extract_extra_prompt(event, "视频")
        if not arg:
            await mark_failed(event)
            return

        provider_override, arg = self._parse_provider_override_prefix(arg)
        if not arg:
            await mark_failed(event)
            return

        preset, prompt = self._parse_video_args(arg)
        presets = self._get_video_presets()
        if preset and preset in presets:
            preset_prompt = presets[preset]
            prompt = f"{preset_prompt}, {prompt}" if prompt else preset_prompt

        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "video", user_id)

        if self.debouncer.hit(request_id):
            await mark_failed(event)
            return

        if not await self._video_begin(user_id):
            await mark_failed(event)
            return

        try:
            await mark_processing(event)
        except Exception:
            await self._video_end(user_id)
            await mark_failed(event)
            return

        try:
            task = asyncio.create_task(
                self._async_generate_video(
                    event, prompt, user_id, provider_id=provider_override
                )
            )
        except Exception:
            await self._video_end(user_id)
            await mark_failed(event)
            return

        self._video_tasks.add(task)
        task.add_done_callback(lambda t: self._video_tasks.discard(t))
        return

    @filter.regex(r"[/!！.。．]视频(\s|$)", priority=-10)
    async def generate_video_regex_fallback(self, event: AstrMessageEvent):
        """兼容“图片在前、文字在后”的消息：确保 /视频 能触发。"""
        msg = (event.message_str or "").strip()
        if self._is_direct_command_message(event, ("视频",)):
            return

        arg = self._extract_command_arg_anywhere(msg, "视频")
        if not arg and "/视频" not in msg:
            return

        event.should_call_llm(True)
        if not bool(self._get_feature("video").get("enabled", False)):
            await mark_failed(event)
            event.stop_event()
            return
        if not arg:
            await mark_failed(event)
            event.stop_event()
            return

        provider_override, arg = self._parse_provider_override_prefix(arg)
        if not arg:
            await mark_failed(event)
            event.stop_event()
            return

        preset, prompt = self._parse_video_args(arg)
        presets = self._get_video_presets()
        if preset and preset in presets:
            preset_prompt = presets[preset]
            prompt = f"{preset_prompt}, {prompt}" if prompt else preset_prompt

        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "video", user_id)

        if self.debouncer.hit(request_id):
            await mark_failed(event)
            event.stop_event()
            return

        if not await self._video_begin(user_id):
            await mark_failed(event)
            event.stop_event()
            return

        try:
            await mark_processing(event)
        except Exception:
            await self._video_end(user_id)
            await mark_failed(event)
            event.stop_event()
            return

        try:
            task = asyncio.create_task(
                self._async_generate_video(
                    event, prompt, user_id, provider_id=provider_override
                )
            )
        except Exception:
            await self._video_end(user_id)
            await mark_failed(event)
            event.stop_event()
            return

        self._video_tasks.add(task)
        task.add_done_callback(lambda t: self._video_tasks.discard(t))
        event.stop_event()
        return

    @filter.command("视频预设列表")
    async def list_video_presets(self, event: AstrMessageEvent):
        """列出所有可用视频预设"""
        event.should_call_llm(True)
        presets = self._get_video_presets()
        names = list(presets.keys())
        if not names:
            yield event.plain_result(
                "📋 视频预设列表\n暂无预设（请在配置 features.video.presets 中添加）"
            )
            return

        msg = "📋 视频预设列表\n"
        for name in names:
            msg += f"- {name}\n"
        msg += "\n用法: /视频 [@provider_id] <预设名> [额外提示词]"
        yield event.plain_result(msg)

    # ==================== 管理命令 ====================

    @filter.command("预设列表")
    async def list_presets(self, event: AstrMessageEvent):
        """列出所有可用预设"""
        event.should_call_llm(True)
        presets = self.edit.get_preset_names()
        backends = self.edit.get_available_backends()
        edit_conf = self._get_feature("edit")
        chain = []
        for it in (
                edit_conf.get("chain", [])
                if isinstance(edit_conf.get("chain", []), list)
                else []
        ):
            pid = self._extract_chain_provider_id(it)
            if pid and pid not in chain:
                chain.append(pid)

        if not presets:
            msg = "📋 改图预设列表\n"
            msg += "━━━━━━━━━━━━━━\n"
            msg += f"🔧 可用后端: {', '.join(backends)}\n"
            if chain:
                msg += f"⭐ 当前链路: {', '.join(chain)}\n"
            msg += "━━━━━━━━━━━━━━\n"
            msg += "📌 暂无预设\n"
            msg += "━━━━━━━━━━━━━━\n"
            msg += "💡 在配置 features.edit.presets 中添加:\n"
            msg += '  格式: "触发词:英文提示词"'
        else:
            msg = "📋 改图预设列表\n"
            msg += "━━━━━━━━━━━━━━\n"
            msg += f"🔧 可用后端: {', '.join(backends)}\n"
            if chain:
                msg += f"⭐ 当前链路: {', '.join(chain)}\n"
            msg += "━━━━━━━━━━━━━━\n"
            msg += "📌 预设:\n"
            for name in presets:
                msg += f"  • {name}\n"
        msg += "━━━━━━━━━━━━━━\n"
        msg += "💡 用法: /aiedit [@provider_id] <提示词> [图片]"

        yield event.plain_result(msg)

    @filter.command("改图帮助")
    async def edit_help(self, event: AstrMessageEvent):
        """显示改图帮助"""
        event.should_call_llm(True)
        msg = """🎨 改图功能帮助

━━ 基础命令 ━━
/aiedit [@provider_id] <提示词>

━━ 使用方式 ━━
1. 发送图片 + 命令
2. 引用图片消息 + 命令

━━ 服务商链路 ━━
在 WebUI 配置：
- providers：添加服务商（id/url/key/model/超时/重试等）
- features.edit.chain：按顺序填写 provider_id（第一个=主用，其余=兜底）

━━ 自定义预设 ━━
查看预设：/预设列表
在 WebUI 配置 features.edit.presets 添加：
格式: 预设名:英文提示词
示例: 手办化:Transform into figurine style
"""

        yield event.plain_result(msg)

    # ==================== LLM 工具 ====================

    @filter.llm_tool(name="aiimg_draw")
    async def aiimg_draw(self, event: AstrMessageEvent, prompt: str):
        """根据提示词生成图片。

        Args:
            prompt(string): 图片提示词，需要包含主体、场景、风格等描述
        """
        return await self.aiimg_generate(
            event, prompt=prompt, mode="text", backend="auto"
        )

    @filter.llm_tool(name="aiimg_edit")
    async def aiimg_edit(
            self,
            event: AstrMessageEvent,
            prompt: str,
            use_message_images: bool = True,
            backend: str = "auto",
    ):
        """编辑用户发送的图片或引用的图片。

        Args:
            prompt(string): 图片编辑提示词
            use_message_images(boolean): 是否自动获取用户消息中的图片（目前仅支持 true）
            backend(string): auto=自动选择。可选值见下方列表，填显示名称或服务商ID均可。除非用户明确要求使用特定后端，否则永远填auto。
        """
        if not use_message_images:
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("没有可用的图片进行编辑")
        return await self.aiimg_generate(
            event, prompt=prompt, mode="edit", backend=backend
        )

    @filter.llm_tool(name="aiimg_generate")
    async def aiimg_generate(
            self,
            event: AstrMessageEvent,
            prompt: str,
            mode: str = "auto",
            backend: str = "auto",
            output: str = "",
    ):
        """统一图片生成/改图/自拍工具。

        生成的图片会自动发送给用户，你绝对禁止手动调用 send_message_to_user 发送图片。
        调用此工具前，你必须先阅读对应的自拍 skill（如 selfie-reference-router 或 sakuragawa-momoha-selfie-router），了解完整的自拍流程和规范后再调用。

        使用建议：
        - 用户发送/引用了图片，并要求"改图/换背景/换风格/修图/换衣服"等：用 mode=edit（或 mode=auto）
        - 最高频：用户要求"bot 自拍/来一张你自己的自拍"，且已设置自拍参考照：用 mode=selfie_ref（或 mode=auto）
        - 纯文生图（用户没有给图片）：用 mode=text（或 mode=auto）

        Args:
            prompt(string): 提示词，必须参考skill构建
            mode(string): auto=自动判断, text=文生图, edit=改图, selfie_ref=自拍
            backend(string): auto=自动选择。可选值见下方列表，填显示名称或服务商ID均可。除非用户明确要求使用特定后端，否则永远填auto。
            output(string): 输出尺寸/分辨率。例: 2048x2048 或 4K（留空用默认）
        """
        prompt = (prompt or "").strip()
        m = (mode or "auto").strip().lower()

        # === TTL 去重检查（防止 ToolLoop 重复调用）===
        message_id = (
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        )
        origin = getattr(event, "unified_msg_origin", "") or ""
        if message_id and origin:
            if self.debouncer.llm_tool_is_duplicate(message_id, origin):
                logger.debug(f"[aiimg_generate] 重复调用已拦截: msg_id={message_id}")
                await mark_success(event)
                return None

        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "aiimg", user_id)
        if self.debouncer.hit(request_id):
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("请求过于频繁，请稍后再试")

        if not await self._begin_user_job(user_id, kind="image"):
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("当前有图片正在生成中，请稍后再试")

        b_raw = (backend or "auto").strip()
        target_backend = self.registry.resolve_backend(b_raw)
        if b_raw and b_raw.lower() != "auto" and target_backend is None:
            logger.warning(
                "[aiimg_generate] 忽略未知 backend 覆盖，回退自动链路: backend=%s",
                b_raw,
            )

        output = (output or "").strip()
        size = output if output and "x" in output else None
        resolution = output if output and size is None else None

        if self._is_background_generate():
            try:
                await mark_processing(event)
            except Exception:
                await self._end_user_job(user_id, kind="image")
                await self._signal_llm_tool_failure(event)
                return self._build_llm_tool_failure_result("标记处理中失败")

            task = asyncio.create_task(
                self._async_llm_tool_generate(
                    event, prompt, m, target_backend, size, resolution, user_id
                )
            )
            self._image_tasks.add(task)
            task.add_done_callback(lambda t: self._image_tasks.discard(t))
            return self._build_llm_tool_background_result(prompt, m)

        try:
            await mark_processing(event)
            image_path, result_mode = await self._execute_llm_tool_generate_core(
                event, prompt, m, target_backend, size, resolution
            )
            return await self._finalize_llm_tool_image(event, image_path, prompt=prompt, mode=result_mode)
        except Exception as e:
            logger.error(f"[aiimg_generate] 失败: {e}", exc_info=True)
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result(str(e))
        finally:
            await self._end_user_job(user_id, kind="image")

    async def _execute_llm_tool_generate_core(
        self,
        event: AstrMessageEvent,
        prompt: str,
        m: str,
        target_backend: str | None,
        size: str | None,
        resolution: str | None,
    ) -> tuple[Path, str]:
        if m in {"selfie_ref", "selfie", "ref"}:
            logger.info("[aiimg_generate] route=selfie_ref (explicit)")
            if not self._is_selfie_enabled():
                raise RuntimeError("自拍功能未启用")
            if not self._is_selfie_llm_enabled():
                raise RuntimeError("自拍功能未启用")
            image_path = await self._generate_selfie_image(
                event, prompt, target_backend, size=size, resolution=resolution,
            )
            return image_path, "selfie"

        if m == "auto" and await self._should_auto_selfie_ref(event, prompt):
            if not self._is_selfie_enabled():
                logger.info("[aiimg_generate] auto-selfie skipped: features.selfie.enabled=false")
            elif not self._is_selfie_llm_enabled():
                logger.info("[aiimg_generate] auto-selfie skipped: features.selfie.llm_tool_enabled=false")
            else:
                try:
                    logger.info("[aiimg_generate] route=auto->selfie_ref")
                    image_path = await self._generate_selfie_image(
                        event, prompt, target_backend, size=size, resolution=resolution,
                    )
                    return image_path, "selfie"
                except Exception as e:
                    logger.warning("[aiimg_generate] auto-selfie failed, fallback to draw/edit: %s", e)

        has_msg_images = await self._has_message_images(event)
        prefetched_edit_image_segs = None
        has_at_avatar_refs = False
        if m == "auto" and not has_msg_images:
            prefetched_edit_image_segs = await get_images_from_event(
                event, include_avatar=True, include_sender_avatar_fallback=False,
            )
            has_at_avatar_refs = bool(prefetched_edit_image_segs)

        if m in {"edit", "img2img", "aiedit"} or (
            m == "auto" and (has_msg_images or has_at_avatar_refs)
        ):
            logger.info("[aiimg_generate] route=edit")
            edit_conf = self._get_feature("edit")
            if not bool(edit_conf.get("enabled", True)):
                raise RuntimeError("改图功能未启用")
            if not bool(edit_conf.get("llm_tool_enabled", True)):
                raise RuntimeError("改图功能未启用")
            image_segs = prefetched_edit_image_segs
            if image_segs is None:
                image_segs = await get_images_from_event(
                    event, include_avatar=True, include_sender_avatar_fallback=False,
                )
            bytes_images = await self._image_segs_to_bytes(image_segs)
            if not bytes_images:
                raise RuntimeError("没有找到可编辑的图片")
            image_path = await self.edit.edit(
                prompt=prompt, images=bytes_images, backend=target_backend,
                size=size, resolution=resolution,
            )
            return image_path, "edit"

        draw_conf = self._get_feature("draw")
        if not bool(draw_conf.get("enabled", True)):
            raise RuntimeError("文生图功能未启用")
        if not bool(draw_conf.get("llm_tool_enabled", True)):
            raise RuntimeError("文生图功能未启用")
        if not prompt:
            prompt = "a selfie photo"

        logger.info("[aiimg_generate] route=draw")
        image_path = await self.draw.generate(
            prompt, provider_id=target_backend, size=size, resolution=resolution,
        )
        return image_path, "draw"

    async def _async_llm_tool_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        mode: str,
        target_backend: str | None,
        size: str | None,
        resolution: str | None,
        user_id: str,
    ) -> None:
        try:
            image_path, result_mode = await self._execute_llm_tool_generate_core(
                event, prompt, mode, target_backend, size, resolution
            )
            self._remember_last_image(event, image_path, mode=result_mode)
            sent = await self._send_image_with_fallback(event, image_path)
            if sent:
                await mark_success(event)
            else:
                await self._signal_llm_tool_failure(event)
                logger.warning("[aiimg_generate][bg] 图片发送失败: reason=%s", sent.reason)
        except Exception as e:
            logger.error(f"[aiimg_generate][bg] 失败: {e}", exc_info=True)
            await self._signal_llm_tool_failure(event)
        finally:
            await self._end_user_job(user_id, kind="image")

    @filter.llm_tool(name="aiimg_video")
    async def aiimg_video(self, event: AstrMessageEvent, prompt: str, image_url: str = "", backend: str = "auto"):
        """根据用户发送/引用的图片生成视频。

        Args:
            prompt(string): 视频提示词。支持 "预设名 额外提示词"（与 `/视频 预设名 额外提示词` 一致）
            image_url(string): 可选。如果通过 aiimg_generate 等工具生成了图片，将其返回的图片地址传入此处，即可基于该图片生成视频。留空则使用当前消息中的图片。
            backend(string): auto=自动选择。可选值见下方列表，填显示名称或服务商ID均可。除非用户明确要求使用特定后端，否则永远填auto。
        """
        vconf = self._get_feature("video")
        if not bool(vconf.get("enabled", False)):
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("视频生成功能未启用")
        if not bool(vconf.get("llm_tool_enabled", True)):
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("视频生成功能未启用")

        arg = (prompt or "").strip()
        if not arg:
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("请提供视频提示词")

        b_raw = (backend or "auto").strip()
        backend_resolved = self.registry.resolve_backend(b_raw)
        if backend_resolved:
            provider_override = backend_resolved
        else:
            provider_override, arg = self._parse_provider_override_prefix(arg)
        if not arg:
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("请提供视频提示词")

        preset, extra_prompt = self._parse_video_args(arg)
        presets = self._get_video_presets()
        if preset and preset in presets:
            preset_prompt = presets[preset]
            extra_prompt = (
                f"{preset_prompt}, {extra_prompt}" if extra_prompt else preset_prompt
            )

        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "video", user_id)

        if self.debouncer.hit(request_id):
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("请求过于频繁，请稍后再试")

        if not await self._video_begin(user_id):
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("当前有视频正在生成中，请稍后再试")

        try:
            await mark_processing(event)
            task = asyncio.create_task(
                self._async_generate_video(
                    event,
                    extra_prompt,
                    user_id,
                    provider_id=provider_override,
                    llm_tool_failure=True,
                    image_url=image_url,
                )
            )
        except Exception:
            await self._video_end(user_id)
            await self._signal_llm_tool_failure(event)
            return self._build_llm_tool_failure_result("视频生成任务创建失败")

        self._video_tasks.add(task)
        task.add_done_callback(lambda t: self._video_tasks.discard(t))

        return None

    @filter.llm_tool(name="aiimg_wardrobe_preview")
    async def aiimg_wardrobe_preview(self, event: AstrMessageEvent, query: str):
        """【自拍专用】从衣橱中检索一张参考图并返回其文字描述，用于指导自拍提示词的构建。
        本工具不会发送图片给用户，只返回文字描述供你参考。
        不要与 search_wardrobe_image 混淆：search_wardrobe_image 是直接发送图片给用户查看，而本工具是自拍流程的预处理步骤。
        使用流程：先调用本工具获取描述 → 根据描述构建提示词 → 再调用 aiimg_generate(mode=selfie_ref) 生图。
        仅当 features.selfie.wardrobe_ref_enabled 开启时可用。

        Args:
            query(string): 自然语言描述，如"穿着洛丽塔连衣裙在户外拍照""泳装海边自拍"，用于从衣橱中检索最匹配的参考图
        """
        selfie_conf = self._get_feature("selfie")
        if not selfie_conf.get("wardrobe_ref_enabled", False):
            return self._build_llm_tool_text_desc_result(
                "衣橱参考图功能未开启（features.selfie.wardrobe_ref_enabled=false）"
            )

        if not self._is_selfie_enabled() or not self._is_selfie_llm_enabled():
            return self._build_llm_tool_text_desc_result(
                "自拍功能已关闭"
            )

        wardrobe = self._get_wardrobe_instance()
        if not wardrobe:
            return self._build_llm_tool_text_desc_result(
                "衣橱插件未安装或未启用，无法获取参考图"
            )

        persona_name = await self._get_current_persona_name(event)
        if not persona_name:
            return self._build_llm_tool_text_desc_result(
                "当前对话未绑定人格，无法使用衣橱参考图"
            )

        search_query = (query or "").strip() or "日常自拍照"
        try:
            ref = await wardrobe.get_reference_image(
                query=search_query,
                current_persona=persona_name,
            )
        except Exception as e:
            logger.warning("[wardrobe_preview] 衣橱参考图获取失败: %s", e)
            return self._build_llm_tool_text_desc_result(
                f"衣橱参考图获取失败: {e}"
            )

        if not ref:
            return self._build_llm_tool_text_desc_result(
                "衣橱中未找到匹配的参考图。你可以直接调用 aiimg_generate(mode=selfie_ref) 使用人设参考图自拍。"
            )

        user_id = str(event.get_sender_id() or "")
        if user_id:
            self._wardrobe_preview_cache[user_id] = ref

        description = ref.get("description", "") or ""
        ref_persona = ref.get("persona", "") or "未知"
        ref_strength = ref.get("ref_strength", "style") or "style"

        from .core.daily_selfie import _build_strength_hint
        hint = _build_strength_hint(ref_strength)

        persona_ref_count = len(self._get_persona_config_selfie_reference_paths(persona_name))
        wardrobe_ref_index = persona_ref_count + 1

        result_text = (
            f"衣橱参考图已找到（来自人格「{ref_persona}」）：\n"
            f"{description}\n\n{hint}\n\n"
            f"请根据以上描述构建自拍提示词，然后调用 aiimg_generate(mode=selfie_ref)。"
            f"这张参考图的序号为{wardrobe_ref_index}，会自动作为额外参考图传入。"
            f"前{persona_ref_count}张参考图是你的人设图，要使用这张新的参考图，请在提示词中使用序号{wardrobe_ref_index}来引用该参考图。"
        )
        return self._build_llm_tool_text_desc_result(result_text)

    # ==================== 内部方法 ====================

    def _get_feature(self, name: str) -> dict:
        feats = self.config.get("features", {}) if isinstance(self.config, dict) else {}
        feats = feats if isinstance(feats, dict) else {}
        conf = feats.get(name, {})
        return conf if isinstance(conf, dict) else {}

    def _is_selfie_enabled(self) -> bool:
        conf = self._get_feature("selfie")
        return self._as_bool(conf.get("enabled", True), default=True)

    def _is_selfie_llm_enabled(self) -> bool:
        conf = self._get_feature("selfie")
        return self._as_bool(conf.get("llm_tool_enabled", True), default=True)

    @staticmethod
    def _selfie_disabled_message() -> str:
        return "自拍参考图模式已关闭（features.selfie.enabled=false）"

    async def _send_image_with_fallback(
        self, event: AstrMessageEvent, image_path: Path, *, max_attempts: int = 5
    ) -> SendImageResult:
        p = Path(image_path)

        if not p.exists():
            logger.warning("[send_image] file not found: %s", p)
            return SendImageResult(ok=False, reason="file_not_found", cached_path=p)

        try:
            size_bytes = int(p.stat().st_size)
        except Exception:
            size_bytes = 0

        file_send_tries = 0

        async def try_send_as_file(trigger: str) -> bool:
            nonlocal file_send_tries
            if file_send_tries >= 2:
                return False
            file_send_tries += 1
            try:
                await event.send(event.chain_result([File(name=p.name, file=str(p))]))
                logger.info(
                    "[send_image][file-fallback-v2] file send success: %s (%s bytes), trigger=%s, try=%s",
                    p.name, size_bytes, trigger, file_send_tries,
                )
                return True
            except Exception as e:
                logger.warning(
                    "[send_image][file-fallback-v2] file send failed: trigger=%s, try=%s, err=%s",
                    trigger, file_send_tries, e,
                )
                return False

        if size_bytes > self.IMAGE_AS_FILE_THRESHOLD_BYTES:
            if await try_send_as_file("size_threshold"):
                return SendImageResult(ok=True, cached_path=p, used_fallback=True)

        delay = 1.5
        last_exc: Exception | None = None
        attempts = max(1, int(max_attempts))
        rich_media_failures = 0
        compact_bytes: bytes | None = None
        compact_prepared = False
        for attempt in range(1, attempts + 1):
            fs_exc: Exception | None = None
            bytes_exc: Exception | None = None
            compact_exc: Exception | None = None
            fs_failed_by_rich_media = False

            try:
                await event.send(event.chain_result([Image.fromFileSystem(str(p))]))
                return SendImageResult(ok=True, cached_path=p, used_fallback=False)
            except Exception as e:
                fs_exc = e
                last_exc = e
                if self._is_rich_media_transfer_failed(e):
                    fs_failed_by_rich_media = True
                logger.debug(
                    "[send_image] fromFileSystem failed (attempt=%s/%s): %s",
                    attempt, attempts, e,
                )

            try:
                data = await asyncio.to_thread(p.read_bytes)
                await event.send(event.chain_result([Image.fromBytes(data)]))
                if fs_exc is not None:
                    logger.info(
                        "[send_image] fromBytes fallback succeeded (attempt=%s/%s).",
                        attempt, attempts,
                    )
                return SendImageResult(ok=True, cached_path=p, used_fallback=True)
            except Exception as e:
                bytes_exc = e
                last_exc = e
                logger.debug(
                    "[send_image] fromBytes failed (attempt=%s/%s): %s",
                    attempt, attempts, e,
                )

            if self._is_rich_media_transfer_failed(
                fs_exc
            ) or self._is_rich_media_transfer_failed(bytes_exc):
                if await try_send_as_file("rich_media_transfer_failed"):
                    return SendImageResult(ok=True, cached_path=p, used_fallback=True)

            if self._is_rich_media_transfer_failed(
                fs_exc
            ) or self._is_rich_media_transfer_failed(bytes_exc):
                if not compact_prepared:
                    compact_prepared = True
                    compact_bytes = await asyncio.to_thread(
                        self._build_compact_image_bytes, p
                    )
                    if compact_bytes:
                        logger.info(
                            "[send_image] prepared compact fallback image: %s -> %s bytes",
                            p, len(compact_bytes),
                        )
                if compact_bytes:
                    try:
                        await event.send(
                            event.chain_result([Image.fromBytes(compact_bytes)])
                        )
                        logger.info(
                            "[send_image] compact fromBytes fallback succeeded (attempt=%s/%s).",
                            attempt, attempts,
                        )
                        return SendImageResult(
                            ok=True, cached_path=p, used_fallback=True
                        )
                    except Exception as e:
                        compact_exc = e
                        last_exc = e
                        logger.debug(
                            "[send_image] compact fromBytes failed (attempt=%s/%s): %s",
                            attempt, attempts, e,
                        )

            attempt_has_rich_media = (
                self._is_rich_media_transfer_failed(fs_exc)
                or self._is_rich_media_transfer_failed(bytes_exc)
                or self._is_rich_media_transfer_failed(compact_exc)
            )
            if attempt_has_rich_media:
                rich_media_failures += 1

            if rich_media_failures >= 2:
                logger.info(
                    "[send_image] detected repeated rich media transfer failures, stop retrying early."
                )
                break

            if attempt < attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 1.8, 8.0)

        reason = (
            "rich_media_transfer_failed"
            if self._is_rich_media_transfer_failed(last_exc)
            else "send_failed"
        )
        logger.error(
            "[send_image] failed after retries: reason=%s, err=%s", reason, last_exc
        )
        return SendImageResult(
            ok=False,
            reason=reason,
            cached_path=p,
            last_error=str(last_exc or ""),
        )

    def _get_draw_ratio_default_sizes(self) -> dict[str, str]:
        conf = self._get_feature("draw")
        raw = conf.get("ratio_default_sizes", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for ratio, size in raw.items():
            r = str(ratio or "").strip()
            s = normalize_size_text(size)
            if not r or not s:
                continue
            out[r] = s
        return out

    def _resolve_ratio_size(self, ratio: str) -> str:
        ratio = str(ratio or "").strip()
        overrides = self._get_draw_ratio_default_sizes()
        size, warning = resolve_ratio_size(
            ratio,
            overrides=overrides,
            supported_ratios=self.SUPPORTED_RATIOS,
        )
        if warning:
            logger.warning("[aiimg] %s", warning)
        return size

    def _get_video_presets(self) -> dict[str, str]:
        presets: dict[str, str] = {}
        conf = self._get_feature("video")
        items = conf.get("presets", [])
        if not isinstance(items, list):
            return presets
        for item in items:
            if isinstance(item, str) and ":" in item:
                key, val = item.split(":", 1)
                key = key.strip()
                val = val.strip()
                if key and val:
                    presets[key] = val
        return presets

    def _get_video_chain(self) -> list[str]:
        conf = self._get_feature("video")
        chain = conf.get("chain", [])
        if not isinstance(chain, list):
            return []
        out: list[str] = []
        for item in chain:
            pid = self._extract_chain_provider_id(item)
            if pid and pid not in out:
                out.append(pid)
        return out

    def _parse_video_args(self, text: str) -> tuple[str | None, str]:
        """解析 /视频 参数，返回 (preset, prompt)

        - 当第一个 token 命中预设名时：preset=该 token, prompt=剩余内容
        - 否则：preset=None, prompt=text
        """
        text = (text or "").strip()
        if not text:
            return None, ""

        first, _, rest = text.partition(" ")
        if first and first in self._get_video_presets():
            return first, rest.strip()
        return None, text

    async def _video_begin(self, user_id: str) -> bool:
        """单用户并发保护：成功占用返回 True，否则 False（上限可配置）"""
        return await self._begin_user_job(str(user_id or ""), kind="video")

    async def _video_end(self, user_id: str) -> None:
        await self._end_user_job(str(user_id or ""), kind="video")

    async def _send_video_result(self, event: AstrMessageEvent, video_url: str) -> None:
        vconf = self._get_feature("video")
        mode = str(vconf.get("send_mode", "auto")).strip().lower()
        if mode not in {"auto", "url", "file"}:
            mode = "auto"

        send_timeout = int(vconf.get("send_timeout_seconds", 90) or 90)
        send_timeout = max(10, min(send_timeout, 300))

        download_timeout = int(vconf.get("download_timeout_seconds", 300) or 300)
        download_timeout = max(1, min(download_timeout, 3600))

        async def _send_file(url: str) -> bool:
            try:
                video_path = await self.videomgr.download_video(
                    url, timeout_seconds=download_timeout
                )
                await asyncio.wait_for(
                    event.send(
                        event.chain_result([Video(file=f"file://{str(video_path)}", path=str(video_path))])
                    ),
                    timeout=float(send_timeout),
                )
                return True
            except Exception as e:
                logger.warning(f"[视频] 本地文件发送失败: {e}")
                return False

        async def _send_url(url: str) -> bool:
            try:
                await asyncio.wait_for(
                    event.send(event.chain_result([Video.fromURL(url)])),
                    timeout=float(send_timeout),
                )
                return True
            except Exception as e:
                logger.warning(f"[视频] URL 发送失败: {e}")
                return False

        # file/url forced
        if mode == "file":
            if await _send_file(video_url):
                return
            await event.send(event.plain_result(video_url))
            return

        if mode == "url":
            if await _send_url(video_url):
                return
            await event.send(event.plain_result(video_url))
            return

        # auto: prefer file first (most platforms won't render URL as playable video)
        if await _send_file(video_url):
            return
        if await _send_url(video_url):
            return
        await event.send(event.plain_result(video_url))

    async def _async_generate_video(
            self,
            event: AstrMessageEvent,
            prompt: str,
            user_id: str,
            *,
            provider_id: str | None = None,
            llm_tool_failure: bool = False,
            image_url: str = "",
    ) -> None:
        try:
            image_bytes: bytes | None = None
            if image_url:
                image_url = image_url.strip()
                if image_url:
                    try:
                        if image_url.startswith(("http://", "https://")):
                            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                                resp = await client.get(image_url)
                                resp.raise_for_status()
                                image_bytes = resp.content
                        elif image_url.startswith("file://"):
                            path = image_url[7:]
                            image_bytes = await asyncio.to_thread(lambda: open(path, "rb").read())
                        else:
                            image_bytes = await asyncio.to_thread(lambda: open(image_url, "rb").read())
                    except Exception as e:
                        logger.warning(f"[视频] 读取 image_url 失败: {e}")

            if not image_bytes:
                image_segs = await get_images_from_event(
                    event,
                    include_avatar=True,
                    include_sender_avatar_fallback=False,
                )
                had_image = bool(image_segs)
                for i, seg in enumerate(image_segs):
                    try:
                        b64 = await seg.convert_to_base64()
                        image_bytes = base64.b64decode(b64)
                        if not image_url:
                            seg_url = str(getattr(seg, "url", "") or "").strip()
                            if seg_url:
                                image_url = seg_url
                        break
                    except Exception as e:
                        logger.warning(f"[视频] 图片 {i + 1} 转换失败，跳过: {e}")

                if image_bytes and not image_url:
                    from .core.grok_video_service import _build_data_url
                    image_url = _build_data_url(image_bytes)
                    logger.info(
                        "[视频] image_url 为空，已从 image_bytes 构建 data URL: "
                        "size=%s bytes, data URL 长度=%s",
                        len(image_bytes),
                        len(image_url),
                    )

                if had_image and not image_bytes:
                    if llm_tool_failure:
                        await self._signal_llm_tool_failure(event)
                    else:
                        await mark_failed(event)
                    return

            t_start = time.perf_counter()
            candidates = (
                [str(provider_id).strip()] if provider_id else self._get_video_chain()
            )
            candidates = [c for c in candidates if c]
            if not candidates:
                raise RuntimeError(
                    "No video providers configured. Please set features.video.chain."
                )

            last_error: Exception | None = None
            video_url: str | None = None
            used_pid: str | None = None
            for pid in candidates:
                try:
                    backend = self.registry.get_video_backend(pid)
                    candidate_url = await backend.generate_video_url(
                        prompt=prompt, image_bytes=image_bytes, image_url=image_url
                    )
                    candidate_url = str(candidate_url or "").strip()
                    if not candidate_url:
                        raise RuntimeError("Provider returned empty video url")
                    video_url = candidate_url
                    used_pid = pid
                    break
                except Exception as e:
                    last_error = e
                    logger.warning("[视频] Provider=%s 失败: %s", pid, e)

            if not video_url:
                raise RuntimeError(f"视频生成失败: {last_error}") from last_error

            await self._send_video_result(event, video_url)
            await mark_success(event)

            t_end = time.perf_counter()
            name = used_pid or "video"
            logger.info(f"[视频] 完成: provider={name}, 耗时={t_end - t_start:.2f}s")

        except Exception as e:
            logger.error(f"[视频] 失败: {e}", exc_info=True)
            if llm_tool_failure:
                await self._signal_llm_tool_failure(event)
            else:
                await mark_failed(event)
        finally:
            await self._video_end(user_id)

    async def _do_edit_direct(
            self,
            event: AstrMessageEvent,
            prompt: str,
            backend: str | None = None,
            preset: str | None = None,
    ):
        """改图执行入口 (非 generator 版本，用于动态注册的命令)

        使用 event.send() 直接发送消息，不使用 yield
        """
        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "edit", user_id)

        # 防抖
        if self.debouncer.hit(request_id):
            await mark_failed(event)
            return

        p = (prompt or "").strip()
        override, rest = self._parse_provider_override_prefix(p)
        if override:
            backend = override
            prompt = rest

        # 获取图片
        image_segs = await get_images_from_event(
            event,
            include_avatar=True,
            include_sender_avatar_fallback=False,
        )
        logger.debug(f"[改图] 获取到 {len(image_segs)} 个图片段")
        if not image_segs:
            await mark_failed(event)
            return

        bytes_images: list[bytes] = []
        for i, seg in enumerate(image_segs):
            try:
                logger.debug(f"[改图] 转换图片 {i + 1}/{len(image_segs)}...")
                b64 = await seg.convert_to_base64()
                bytes_images.append(base64.b64decode(b64))
                logger.debug(
                    f"[改图] 图片 {i + 1} 转换成功, 大小={len(bytes_images[-1])} bytes"
                )
            except Exception as e:
                logger.warning(f"[改图] 图片 {i + 1} 转换失败，跳过: {e}")

        if not bytes_images:
            await mark_failed(event)
            return

        if not await self._begin_user_job(user_id, kind="image"):
            await mark_failed(event)
            return

        try:
            # 标记处理中
            await mark_processing(event)
            t_start = time.perf_counter()
            image_path = await self.edit.edit(
                prompt=prompt,
                images=bytes_images,
                backend=backend,
                preset=preset,
            )
            t_end = time.perf_counter()

            self._remember_last_image(event, image_path)
            sent = await self._send_image_with_fallback(event, image_path)
            if not sent:
                await mark_failed(event)
                logger.warning(
                    "[改图] 结果发送失败，已仅使用表情标注: reason=%s",
                    sent.reason,
                )
                return

            # 标记成功
            await mark_success(event)
            display_name = preset or (prompt[:20] if prompt else "改图")
            logger.info(f"[改图] 完成: {display_name}..., 耗时={t_end - t_start:.2f}s")

        except Exception as e:
            logger.error(f"[改图] 失败: {e}", exc_info=True)
            await mark_failed(event)
        finally:
            await self._end_user_job(user_id, kind="image")

    async def _do_edit(
            self,
            event: AstrMessageEvent,
            prompt: str,
            backend: str | None = None,
            preset: str | None = None,
    ):
        """统一改图执行入口

        预设触发逻辑:
        1. 如果 preset 参数已指定，直接使用
        2. 否则检查 prompt 是否匹配预设名，若匹配则自动转为预设
        3. 都不匹配则作为普通提示词处理
        """
        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "edit", user_id)

        # 防抖
        if self.debouncer.hit(request_id):
            await mark_failed(event)
            return

        # Optional provider override: "/aiedit @provider_id <prompt>"
        p = (prompt or "").strip()
        override, rest = self._parse_provider_override_prefix(p)
        if override:
            backend = override
            prompt = rest

        # 预设自动检测: prompt 完全匹配预设名时，自动转为预设
        if not preset and prompt:
            prompt_stripped = prompt.strip()
            preset_names = self.edit.get_preset_names()
            if prompt_stripped in preset_names:
                preset = prompt_stripped
                prompt = ""  # 清空 prompt，使用预设的提示词
                logger.debug(f"[改图] 自动匹配预设: {preset}")

        # 获取图片
        image_segs = await get_images_from_event(
            event,
            include_avatar=True,
            include_sender_avatar_fallback=False,
        )
        if not image_segs:
            await mark_failed(event)
            return

        bytes_images: list[bytes] = []
        for seg in image_segs:
            try:
                b64 = await seg.convert_to_base64()
                bytes_images.append(base64.b64decode(b64))
            except Exception as e:
                logger.warning(f"[改图] 图片转换失败，跳过: {e}")

        if not bytes_images:
            await mark_failed(event)
            return

        if not await self._begin_user_job(user_id, kind="image"):
            await mark_failed(event)
            return

        try:
            # 标记处理中
            await mark_processing(event)
            t_start = time.perf_counter()
            image_path = await self.edit.edit(
                prompt=prompt,
                images=bytes_images,
                backend=backend,
                preset=preset,
            )
            t_end = time.perf_counter()

            self._remember_last_image(event, image_path)
            sent = await self._send_image_with_fallback(event, image_path)
            if not sent:
                await mark_failed(event)
                logger.warning(
                    "[改图] 结果发送失败，已仅使用表情标注: reason=%s",
                    sent.reason,
                )
                return

            # 标记成功
            await mark_success(event)
            display_name = preset or (prompt[:20] if prompt else "改图")
            logger.info(f"[改图] 完成: {display_name}..., 耗时={t_end - t_start:.2f}s")

        except Exception as e:
            logger.error(f"[改图] 失败: {e}")
            await mark_failed(event)
        finally:
            await self._end_user_job(user_id, kind="image")

    # ==================== 自拍参考照：内部实现 ====================

    def _get_selfie_persona_config(self, index: int) -> dict:
        """获取指定索引的人格自拍配置（1 或 2）"""
        conf = self._get_feature(f"selfie_persona_{index}")
        return conf if isinstance(conf, dict) else {}

    async def _get_current_persona_name(self, event: AstrMessageEvent) -> str | None:
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not umo:
                return None

            persona_id = None

            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                try:
                    curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                    if curr_cid:
                        conversation = await conv_mgr.get_conversation(umo, curr_cid)
                        if conversation:
                            persona_id = getattr(conversation, "persona_id", None)
                except Exception as e:
                    logger.debug("[aiimg] 从 conversation_manager 获取 persona_id 失败: %s", e)

            if persona_id:
                return str(persona_id).strip() or None

            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr:
                try:
                    persona_obj = None
                    if hasattr(persona_mgr, "get_default_persona_v3"):
                        persona_obj = await persona_mgr.get_default_persona_v3(umo)
                    if persona_obj:
                        name = self._extract_persona_name(persona_obj)
                        if name:
                            return name
                except Exception as e:
                    logger.debug("[aiimg] 从 persona_manager 获取默认人格失败: %s", e)
        except Exception as e:
            logger.debug("[aiimg] 获取人格名失败: %s", e)
        return None

    def _get_wardrobe_instance(self):
        """获取衣橱插件实例，用于参考图功能。"""
        try:
            star = self.context.get_registered_star("astrbot_plugin_wardrobe")
            if star and star.activated and star.star_cls:
                return star.star_cls
        except Exception as e:
            logger.debug("[aiimg] 获取衣橱插件实例失败: %s", e)
        return None

    async def _trigger_wardrobe_auto_save(self, event: AstrMessageEvent) -> None:
        # 命令路径使用 event.send() 发送图片，不会触发 Pipeline 的 RespondStage，
        # 因此 wardrobe 的 on_after_message_sent 钩子不会被调用。
        # 这里主动调用 wardrobe 的自动存图方法来弥补。
        wardrobe = self._get_wardrobe_instance()
        if wardrobe and hasattr(wardrobe, "_auto_save_aiimg_image"):
            try:
                await wardrobe._auto_save_aiimg_image(event, tool=None)
            except Exception as e:
                logger.debug("[aiimg] 触发衣橱自动存图失败: %s", e)

    @staticmethod
    def _extract_persona_name(persona_obj) -> str | None:
        if not persona_obj:
            return None
        if isinstance(persona_obj, dict):
            for key in ("name", "persona_id", "id"):
                val = persona_obj.get(key)
                if val and str(val).strip():
                    return str(val).strip()
            return None
        for attr in ("name", "persona_id", "id"):
            if hasattr(persona_obj, attr):
                val = getattr(persona_obj, attr, None)
                if val and str(val).strip():
                    return str(val).strip()
        return None

    def _get_llm_tool_conf(self) -> dict:
        conf = self.config.get("llm_tool", {}) if isinstance(self.config, dict) else {}
        return conf if isinstance(conf, dict) else {}

    def _get_image_context_mode(self) -> str:
        conf = self._get_llm_tool_conf()
        mode = str(conf.get("image_context_mode", "image")).strip().lower()
        if mode not in ("image", "text", "none"):
            mode = "image"
        return mode

    def _is_background_generate(self) -> bool:
        conf = self._get_llm_tool_conf()
        return self._as_bool(conf.get("background_generate", True), default=True)

    async def _ensure_tool_image_cache_dir(self) -> None:
        tool_image_dir = Path(get_astrbot_temp_path()) / "tool_images"
        await asyncio.to_thread(tool_image_dir.mkdir, parents=True, exist_ok=True)

    async def _build_llm_tool_image_result(
            self, image_path: Path
    ) -> mcp.types.CallToolResult | None:
        try:
            compressed_bytes = await asyncio.to_thread(
                self._compress_for_llm_context, image_path, max_side=2048, quality=85
            )
            if not compressed_bytes:
                return None
            b64_data = base64.b64encode(compressed_bytes).decode("utf-8")
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.ImageContent(
                        type="image",
                        data=b64_data,
                        mimeType="image/jpeg",
                    )
                ]
            )
        except Exception as e:
            logger.warning("[aiimg_generate] 构建LLM图片结果失败: %s", e)
            return None

    @staticmethod
    def _build_llm_tool_failure_result(reason: str = "") -> mcp.types.CallToolResult:
        text = "图片生成失败" + (f"：{reason}" if reason else "") + "。请以符合你人设的口吻告知用户此结果，不要直接复述原始错误信息。"
        return mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text=text)]
        )

    @staticmethod
    def _build_llm_tool_text_desc_result(prompt: str) -> mcp.types.CallToolResult:
        desc = str(prompt or "").strip()
        text = f"发送了一张图片" + (f"：{desc}" if desc else "")
        return mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text=text)]
        )

    @staticmethod
    def _build_llm_tool_background_result(prompt: str, mode: str) -> mcp.types.CallToolResult:
        mode_desc = {"text": "文生图", "edit": "改图", "selfie_ref": "自拍", "auto": "图片"}.get(mode, "图片")
        text = f"正在生成{mode_desc}，完成后会自动发送。请以符合你人设的口吻告知用户图片正在生成中。"
        return mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text=text)]
        )

    async def _finalize_llm_tool_image(
            self,
            event: AstrMessageEvent,
            image_path: Path,
            *,
            prompt: str = "",
            mode: str = "",
    ) -> mcp.types.CallToolResult | None:
        self._remember_last_image(event, image_path, mode=mode)

        sent = await self._send_image_with_fallback(event, image_path)
        if not sent:
            await self._signal_llm_tool_failure(event)
            logger.warning(
                "[aiimg_generate] 无损原图发送失败，已使用表情标注: reason=%s",
                sent.reason,
            )
            return self._build_llm_tool_failure_result("图片发送失败")

        await mark_success(event)

        mode = self._get_image_context_mode()

        if mode == "none":
            return None

        if mode == "text":
            return self._build_llm_tool_text_desc_result(prompt)

        await self._ensure_tool_image_cache_dir()
        result = await self._build_llm_tool_image_result(image_path)
        if result is not None:
            return result
        logger.warning(
            "[aiimg_generate] LLM上下文图片构建失败，降级为文字描述"
        )
        return self._build_llm_tool_text_desc_result(prompt)

    def _get_selfie_ref_store_key(
            self, event: AstrMessageEvent, persona_name: str | None = None
    ) -> str:
        self_id = ""
        try:
            if hasattr(event, "get_self_id"):
                self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        base = f"bot_selfie_{self_id}" if self_id else "bot_selfie"
        if persona_name:
            return f"{base}__persona_{persona_name}"
        return base

    def _resolve_data_rel_path(self, rel_path: str) -> Path | None:
        if not isinstance(rel_path, str) or not rel_path.strip():
            return None
        rel = rel_path.replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p]
        if any(p in {".", ".."} for p in parts):
            return None
        base = Path(self.data_dir).resolve(strict=False)
        target = (base / "/".join(parts)).resolve(strict=False)
        try:
            target.relative_to(base)
        except ValueError:
            return None
        if target.is_file():
            return target
        legacy_base = Path(self._legacy_data_dir).resolve(strict=False)
        legacy_target = (legacy_base / "/".join(parts)).resolve(strict=False)
        try:
            legacy_target.relative_to(legacy_base)
        except ValueError:
            return target
        if legacy_target.is_file():
            return legacy_target
        return target

    def _get_persona_config_selfie_reference_paths(
            self, persona_name: str
    ) -> list[Path]:
        """从 selfie_persona_1 或 selfie_persona_2 查找匹配人格的参考照"""
        logger.debug("[selfie_ref] 查找人格配置: persona_name=%r", persona_name)
        for idx in [1, 2]:
            conf = self._get_selfie_persona_config(idx)
            if not conf:
                logger.debug("[selfie_ref] selfie_persona_%s 无配置", idx)
                continue
            conf_persona = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            logger.debug("[selfie_ref] selfie_persona_%s: select_persona=%r vs persona_name=%r match=%s", idx, conf_persona, persona_name, conf_persona == persona_name)
            if conf_persona != persona_name:
                continue
            ref_list = conf.get("reference_images", [])
            logger.debug("[selfie_ref] selfie_persona_%s: reference_images=%s", idx, ref_list)
            if not isinstance(ref_list, list):
                continue
            paths: list[Path] = []
            for rel_path in ref_list:
                p = self._resolve_data_rel_path(str(rel_path))
                logger.debug("[selfie_ref] 解析路径: rel=%r -> p=%s exists=%s", rel_path, p, p.is_file() if p else None)
                if not p:
                    continue
                if p.is_file():
                    paths.append(p)
            logger.debug("[selfie_ref] 找到 %d 张参考照", len(paths))
            return paths
        logger.debug("[selfie_ref] 未找到任何匹配的人格配置")
        return []

    async def _get_selfie_reference_paths(
            self, event: AstrMessageEvent, persona_name: str | None = None
    ) -> tuple[list[Path], str]:
        if not persona_name:
            return [], "none"
        persona_webui = self._get_persona_config_selfie_reference_paths(persona_name)
        if persona_webui:
            return persona_webui, "webui_persona"
        persona_key = self._get_selfie_ref_store_key(event, persona_name=persona_name)
        persona_store_paths = await self.refs.get_paths(persona_key)
        if persona_store_paths:
            return persona_store_paths, "store_persona"
        return [], "none"

    async def _read_paths_bytes(self, paths: list[Path]) -> list[bytes]:
        out: list[bytes] = []
        for p in paths:
            try:
                data = await asyncio.to_thread(p.read_bytes)
            except Exception:
                continue
            if data:
                out.append(data)
        return out

    async def _image_segs_to_bytes(self, image_segs: list) -> list[bytes]:
        """将 Image 组件列表转换为 bytes。"""
        out: list[bytes] = []
        for seg in image_segs:
            try:
                b64 = await seg.convert_to_base64()
                out.append(base64.b64decode(b64))
            except Exception as e:
                logger.warning(f"[图片] 转换失败，跳过: {e}")
        return out

    async def _has_message_images(self, event: AstrMessageEvent) -> bool:
        """仅检测用户消息/引用里的图片（不含头像兜底）。"""
        image_segs = await get_images_from_event(event, include_avatar=False)
        return bool(image_segs)

    def _is_auto_selfie_prompt(self, prompt: str) -> bool:
        text = (prompt or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if "自拍" in text or "selfie" in lowered:
            return True
        if any(
                k in text
                for k in (
                        "来一张你",
                        "来张你",
                        "你来一张",
                        "你来张",
                        "看看你",
                        "你自己",
                        "你本人",
                        "你的照片",
                        "你的自拍",
                        "你自己的照片",
                        "你自己的自拍",
                        "你长什么样",
                        "看看你本人",
                        "看看你自己",
                        "bot自拍",
                        "机器人自拍",
                )
        ):
            return True
        if any(
                k in lowered
                for k in ("your selfie", "your photo", "your picture", "your face")
        ):
            return True
        return False

    async def _should_auto_selfie_ref(
            self, event: AstrMessageEvent, prompt: str
    ) -> bool:
        if not self._is_auto_selfie_prompt(prompt):
            logger.debug("[aiimg_generate] auto-selfie skipped: prompt not selfie")
            return False
        persona_name = await self._get_current_persona_name(event)
        paths, source = await self._get_selfie_reference_paths(
            event, persona_name=persona_name
        )
        if not paths:
            logger.info("[aiimg_generate] auto-selfie skipped: no reference images")
            return False
        logger.debug(
            "[aiimg_generate] auto-selfie candidate: persona=%s refs=%s source=%s",
            persona_name,
            len(paths),
            source,
        )
        return True

    def _build_selfie_prompt(self, prompt: str, extra_refs: int, prompt_prefix: str = "") -> str:
        # 使用配置的提示词前缀，如果未配置则使用默认前缀
        if prompt_prefix:
            prefix = prompt_prefix
        else:
            prefix = (
                "请根据参考图生成一张新的自拍照：\n"
                "1) 以第1张参考图的人脸身份为准（仅人脸身份特征），保持五官/气质一致。\n"
                "2) 如果还有其它参考图，请将它们仅作为服装/姿势/构图/场景的参考。\n"
                "3) 输出一张高质量照片风格自拍，不要拼图，不要水印。"
            )
        user_prompt = (prompt or "").strip() or "日常自拍照"
        if extra_refs > 0:
            return (
                f"{prefix}\n\n{user_prompt}\n（额外参考图数量：{extra_refs}）"
            )
        return f"{prefix}\n\n{user_prompt}"

    def _get_persona_selfie_chain(self, persona_name: str) -> list[dict] | None:
        """从 selfie_persona_1 或 selfie_persona_2 查找匹配人格的链路"""
        for idx in [1, 2]:
            conf = self._get_selfie_persona_config(idx)
            if not conf:
                continue
            conf_persona = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            if conf_persona != persona_name:
                continue
            provider_ids = conf.get("provider_ids", [])
            if not isinstance(provider_ids, list):
                continue
            # 获取覆盖输出设置（用于链路中每个 provider 的 output）
            output_override = str(conf.get("output_override", "") or "").strip()
            chain_items = [
                {"provider_id": str(pid).strip(), "output": output_override}
                for pid in provider_ids
                if str(pid).strip()
            ]
            return chain_items if chain_items else None
        return None

    def _get_persona_video_chain(self, persona_name: str) -> list[str] | None:
        """从 selfie_persona_1 或 selfie_persona_2 查找匹配人格的视频链路"""
        for idx in [1, 2]:
            conf = self._get_selfie_persona_config(idx)
            if not conf:
                continue
            conf_persona = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            if conf_persona != persona_name:
                continue
            provider_ids = conf.get("video_provider_ids", [])
            if not isinstance(provider_ids, list):
                continue
            result = [str(pid).strip() for pid in provider_ids if str(pid).strip()]
            return result if result else None
        return None

    def _get_persona_selfie_config(self, persona_name: str) -> dict | None:
        """从 selfie_persona_1 或 selfie_persona_2 查找匹配人格的完整配置"""
        for idx in [1, 2]:
            conf = self._get_selfie_persona_config(idx)
            if not conf:
                continue
            conf_persona = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            if conf_persona != persona_name:
                continue
            return conf
        return None

    async def _generate_selfie_image(
            self,
            event: AstrMessageEvent,
            prompt: str,
            backend: str | None,
            *,
            size: str | None = None,
            resolution: str | None = None,
    ) -> Path:
        persona_name = await self._get_current_persona_name(event)
        if not persona_name:
            raise RuntimeError("当前对话未绑定人格，无法使用自拍功能。")

        ref_paths, source = await self._get_selfie_reference_paths(
            event, persona_name=persona_name
        )

        selfie_conf = self._get_feature("selfie")
        wardrobe_ref_added = False
        if selfie_conf.get("wardrobe_ref_enabled", False):
            wardrobe = self._get_wardrobe_instance()
            if wardrobe:
                # 优先使用 aiimg_wardrobe_preview 缓存的结果，避免重复调用 wardrobe
                user_id = str(event.get_sender_id() or "")
                cached = self._wardrobe_preview_cache.pop(user_id, None)
                if cached:
                    ref = cached
                    logger.info(
                        "[selfie] 使用 wardrobe_preview 缓存: image_id=%s",
                        ref.get("image_id", "未知"),
                    )
                else:
                    query = (prompt or "").strip() or "日常自拍照"
                    try:
                        ref = await wardrobe.get_reference_image(
                            query=query,
                            current_persona=persona_name,
                        )
                    except Exception as e:
                        logger.warning("[selfie] 衣橱参考图获取失败，跳过: %s", e)
                        ref = None
                if ref:
                    ref_paths.append(Path(ref["image_path"]))
                    wardrobe_ref_added = True
                    logger.info(
                        "[selfie] 已追加衣橱参考图: persona=%s image_id=%s",
                        ref.get("persona", "未知"),
                        ref.get("image_id", "未知"),
                    )

        ref_images = await self._read_paths_bytes(ref_paths)
        if not ref_images:
            raise RuntimeError(
                f"人格「{persona_name}」未设置自拍参考照。请先：发送图片 + /自拍参考 设置，或在 WebUI 的 features.selfie_personas 中配置该人格。"
            )

        chain_override = self._get_persona_selfie_chain(persona_name)
        if not chain_override:
            raise RuntimeError(
                f"人格「{persona_name}」未配置自拍服务商链路。请在 WebUI 的 features.selfie_personas 中为该人格添加 chain。"
            )

        # 获取人格自拍配置
        persona_conf = self._get_persona_selfie_config(persona_name)
        
        # 获取默认输出尺寸（如果调用方未指定）
        if size is None and resolution is None:
            default_output = str(persona_conf.get("default_output", "") or "").strip() if persona_conf else ""
            if default_output:
                size = default_output
        
        # 获取并应用提示词前缀
        prompt_prefix = str(persona_conf.get("prompt_prefix", "") or "").strip() if persona_conf else ""
        
        extra_segs = await get_images_from_event(event, include_avatar=False)
        extra_bytes = await self._image_segs_to_bytes(extra_segs)
        images = [*ref_images, *extra_bytes]

        final_prompt = self._build_selfie_prompt(prompt, extra_refs=len(extra_bytes) + (1 if wardrobe_ref_added else 0), prompt_prefix=prompt_prefix)

        logger.debug(
            "[selfie] persona=%s source=%s providers=%s size=%s",
            persona_name,
            source,
            [str(x.get("provider_id") or "").strip() for x in chain_override if isinstance(x, dict)],
            size or resolution or "default",
        )

        return await self.edit.edit(
            prompt=final_prompt,
            images=images,
            backend=backend,
            size=size,
            resolution=resolution,
            default_output="",
            chain_override=chain_override,
        )

    async def _do_selfie(
            self,
            event: AstrMessageEvent,
            prompt: str,
            backend: str | None = None,
    ):
        """指令 /自拍 执行入口。"""

        user_id = str(event.get_sender_id() or "")
        request_id = self._debounce_key(event, "selfie", user_id)

        if self.debouncer.hit(request_id):
            await mark_failed(event)
            return

        if not await self._begin_user_job(user_id, kind="image"):
            await mark_failed(event)
            return

        p = (prompt or "").strip()
        override, rest = self._parse_provider_override_prefix(p)
        if override:
            backend = override
            prompt = rest

        size: str | None = None
        parts = prompt.split()
        if parts and parts[-1] in self.SUPPORTED_RATIOS:
            ratio = parts[-1]
            prompt = " ".join(parts[:-1]).strip()
            size = self._resolve_ratio_size(ratio)

        try:
            await mark_processing(event)
            image_path = await self._generate_selfie_image(event, prompt, backend, size=size)
            self._remember_last_image(event, image_path, mode="selfie")
            await self._trigger_wardrobe_auto_save(event)
            sent = await self._send_image_with_fallback(event, image_path)
            if not sent:
                await mark_failed(event)
                logger.warning(
                    "[自拍] 结果发送失败，已仅使用表情标注: reason=%s",
                    sent.reason,
                )
                return
            await mark_success(event)
        except Exception as e:
            logger.error(f"[自拍] 失败: {e}", exc_info=True)
            await mark_failed(event)
        finally:
            await self._end_user_job(user_id, kind="image")

    async def _generate_daily_selfie_image(
            self,
            persona_name: str,
            prompt: str,
            ref_image_path: str,
            ref_strength: str = "style",
            persona_conf: dict | None = None,
    ) -> Path | None:
        ref_paths = self._get_persona_config_selfie_reference_paths(persona_name)
        if not ref_paths:
            logger.warning("[daily_selfie] 人格 %s 无参考照，跳过", persona_name)
            return None

        if ref_image_path:
            p = Path(ref_image_path)
            if p.exists():
                ref_paths.append(p)

        ref_images = await self._read_paths_bytes(ref_paths)
        if not ref_images:
            logger.warning("[daily_selfie] 人格 %s 参考照读取失败", persona_name)
            return None

        chain_override = self._get_persona_selfie_chain(persona_name)
        if not chain_override:
            logger.warning("[daily_selfie] 人格 %s 未配置自拍链路", persona_name)
            return None

        size = None
        if persona_conf:
            default_output = str(persona_conf.get("default_output", "") or "").strip()
            if default_output:
                size = default_output

        final_prompt = prompt

        logger.info(
            "[daily_selfie] persona=%s prompt=%s providers=%s",
            persona_name,
            final_prompt,
            [str(x.get("provider_id") or "").strip() for x in chain_override if isinstance(x, dict)],
        )

        if self.edit is None:
            logger.error("[daily_selfie] self.edit is None! 插件可能已被重载")
            return None
        if self.edit.registry is None:
            logger.error("[daily_selfie] self.edit.registry is None! 插件可能已被 terminate")
            return None
        available = self.edit.get_available_backends()
        logger.info("[daily_selfie] edit可用后端: %s", available)

        return await self.edit.edit(
            prompt=final_prompt,
            images=ref_images,
            size=size,
            resolution=None,
            default_output="",
            chain_override=chain_override,
        )

    async def _set_selfie_reference(
            self, event: AstrMessageEvent, persona_name: str | None = None
    ):

        image_segs = await get_images_from_event(event, include_avatar=False)
        if not image_segs:
            await mark_failed(event)
            return

        bytes_images = await self._image_segs_to_bytes(image_segs)
        if not bytes_images:
            await mark_failed(event)
            return

        max_images = 8
        bytes_images = bytes_images[:max_images]

        store_key = self._get_selfie_ref_store_key(event, persona_name=persona_name)
        try:
            await self.refs.set(store_key, bytes_images)
        except Exception:
            await mark_failed(event)
            return

        persona_hint = f"（人格：{persona_name}）" if persona_name else "（全局）"
        logger.info("[自拍参考] 已设置参考照 %s，共 %d 张", persona_hint, len(bytes_images))
        await mark_success(event)

    async def _show_selfie_reference(
            self, event: AstrMessageEvent, persona_name: str | None = None
    ):

        paths, source = await self._get_selfie_reference_paths(
            event, persona_name=persona_name
        )
        if not paths:
            await mark_failed(event)
            return

        max_show = 5
        show_paths = paths[:max_show]
        yield event.chain_result([Image.fromFileSystem(str(p)) for p in show_paths])
        persona_hint = f"（人格：{persona_name}）" if persona_name else ""
        yield event.plain_result(
            f"📌 当前自拍参考照{persona_hint}来源：{source}，共 {len(paths)} 张（已展示 {len(show_paths)} 张）"
        )

    async def _delete_selfie_reference(
            self, event: AstrMessageEvent, persona_name: str | None = None
    ):
        # 注意：此方法仅删除通过 /自拍参考 设置 命令保存的参考照（ReferenceStore）。
        # WebUI 中 selfie_persona_1/2 配置的参考照不受影响，仍会继续生效。
        # 这是设计意图：WebUI 配置属于持久化配置，不应通过命令删除。

        store_key = self._get_selfie_ref_store_key(event, persona_name=persona_name)
        deleted = await self.refs.delete(store_key)

        if persona_name:
            persona_webui = self._get_persona_config_selfie_reference_paths(persona_name)
            if persona_webui:
                logger.info(
                    "[自拍参考] 人格 %s 的命令参考照已删除，但 WebUI selfie_personas 仍生效",
                    persona_name,
                )

        if deleted:
            await mark_success(event)
        else:
            await mark_failed(event)