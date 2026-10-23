"""MiniMax-H3 视频后端（magic666 网关）。

协议要点（与 grok / seedance 系的差异）：
- 提交 ``POST /v1/videos``，请求体是 ``content[]`` + ``resolution`` / ``duration`` / ``ratio``；
- 参考图**只吃公网 HTTPS URL**，不收 base64 / data URI，需要先 presign 上传换 ``public_url``；
- 查询 ``GET /v1/videos/{task_id}``，状态与结果都在嵌套的 ``task`` 对象里，顶层通常没有 ``status``；
- 输出固定 ``768P``，音轨强制开启，无法关闭。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx

from astrbot.api import logger

from .grok_video_service import (
    _clamp_int,
    _compress_image_bytes_for_video,
    _deep_find_video_url,
    _guess_image_mime,
    _is_valid_video_url,
)

# 网关唯一支持的输出档位
_FIXED_RESOLUTION = "768P"

_ALLOWED_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
_ALLOWED_IMAGE_ROLES = ("first_frame", "last_frame", "reference_image")

_SUCCESS_STATUSES = frozenset(
    {"succeeded", "success", "completed", "finish", "finished", "done"}
)
_FAILED_STATUSES = frozenset(
    {"failed", "fail", "error", "cancelled", "canceled", "expired", "deleted"}
)
# 轮询阶段可继续等待的状态（含兼容层的写法）
_PENDING_STATUSES = frozenset(
    {"queued", "pending", "running", "processing", "in_progress", "generating", ""}
)

_RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
# 这类 503 是「该 key 分组没有这条模型渠道」，重试无意义
_NON_RETRYABLE_HINTS = ("model_not_found", "no available channel found")
# 结果兜底时排除图片地址，避免把回显的参考图当成成片
_IMAGE_URL_RE = re.compile(
    r"\.(?:png|jpe?g|webp|gif|heic|heif|bmp)(?:[?#]|$)", re.IGNORECASE
)


class _RetryableSubmitError(RuntimeError):
    """提交阶段可重试的错误（429 饱和 / 5xx / 网络抖动）。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = str(resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, 300.0)


def _extract_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return resp.text[:500]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "").strip()
            msg = str(err.get("message") or "").strip()
            if code or msg:
                return f"{code}: {msg}".strip(": ") or str(data)[:300]
        for key in ("message", "detail", "msg"):
            val = str(data.get(key) or "").strip()
            if val:
                return val[:300]
    return str(data)[:300]


class MiniMaxVideoService:
    """MiniMax-H3 视频生成后端。

    与其它视频后端保持同一契约：``generate_video_url(prompt, image_bytes, *, preset, **kwargs)``
    返回视频直链，下载与清理由 ``VideoManager`` 负责。
    """

    def __init__(self, *, settings: dict):
        self.settings = settings if isinstance(settings, dict) else {}

        self.base_url: str = str(
            self.settings.get("base_url") or "https://magic666.top"
        ).rstrip("/")
        self.api_key: str = str(self.settings.get("api_key") or "").strip()

        raw_keys = self.settings.get("api_keys")
        keys: list[str] = []
        if isinstance(raw_keys, list):
            keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        elif isinstance(raw_keys, str) and raw_keys.strip():
            keys = [raw_keys.strip()]
        if self.api_key:
            keys.insert(0, self.api_key)
        self.api_keys: list[str] = keys

        self.model: str = (
            str(self.settings.get("model") or "MiniMax-H3").strip() or "MiniMax-H3"
        )

        # 网关只接受 768P：其它取值一律纠正，避免把 2K 之类的幻想档位传上去
        raw_resolution = str(self.settings.get("resolution") or "").strip()
        if raw_resolution and raw_resolution.upper() != _FIXED_RESOLUTION:
            logger.warning(
                "[MiniMaxVideo] resolution=%s 不受网关支持，强制使用 %s",
                raw_resolution,
                _FIXED_RESOLUTION,
            )
        self.resolution: str = _FIXED_RESOLUTION

        self.default_ratio: str = str(self.settings.get("ratio") or "9:16").strip()
        self.default_duration: int = _clamp_int(
            self.settings.get("duration", 5), default=5, min_value=1, max_value=15
        )
        self.image_role: str = (
            str(self.settings.get("image_role") or "first_frame").strip().lower()
        )
        if self.image_role not in _ALLOWED_IMAGE_ROLES:
            logger.warning(
                "[MiniMaxVideo] image_role=%s 不合法，回退 first_frame", self.image_role
            )
            self.image_role = "first_frame"

        self.timeout_seconds: int = _clamp_int(
            self.settings.get("timeout_seconds", 900),
            default=900,
            min_value=60,
            max_value=3600,
        )
        self.polling_interval: int = _clamp_int(
            self.settings.get("polling_interval", 6),
            default=6,
            min_value=2,
            max_value=30,
        )
        self.max_retries: int = _clamp_int(
            self.settings.get("max_retries", 1), default=1, min_value=0, max_value=5
        )

        self.presets: dict[str, str] = self._load_presets()

        self.create_url: str = urljoin(self.base_url + "/", "v1/videos")
        self.presign_url: str = urljoin(self.base_url + "/", "api/upload/presign")

        logger.info(
            "[MiniMaxVideo] Initialized: base_url=%s, model=%s, resolution=%s, "
            "ratio=%s, duration=%ss, role=%s, timeout=%ss",
            self.base_url,
            self.model,
            self.resolution,
            self.default_ratio,
            self.default_duration,
            self.image_role,
            self.timeout_seconds,
        )

    # ------------------------------------------------------------------ 通用

    def _load_presets(self) -> dict[str, str]:
        presets: dict[str, str] = {}
        items = self.settings.get("presets", [])
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

    def get_preset_names(self) -> list[str]:
        return list(self.presets.keys())

    def build_prompt(self, prompt: str, preset: str | None = None) -> str:
        prompt = (prompt or "").strip()
        if preset and preset in self.presets:
            preset_prompt = self.presets[preset]
            if prompt:
                return f"{preset_prompt}, {prompt}"
            return preset_prompt
        return prompt

    def _get_key(self) -> str:
        if not self.api_keys:
            raise RuntimeError("MiniMax 未配置 API Key（api_key / api_keys）")
        return self.api_keys[0]

    @staticmethod
    def _decode_data_uri(value: str) -> bytes | None:
        """data URI → bytes。main.py 在「有 bytes 无远程 URL」时会兜底成 data URI。"""
        raw = str(value or "").strip()
        header, sep, payload = raw.partition(",")
        if not sep or ";base64" not in header.lower():
            return None
        try:
            data = base64.b64decode(payload, validate=False)
        except Exception:
            return None
        return data or None

    @staticmethod
    def _normalize_ratio(value: Any) -> str:
        ratio = str(value or "").strip()
        if ratio in _ALLOWED_RATIOS:
            return ratio
        if ratio:
            logger.warning("[MiniMaxVideo] ratio=%s 不受支持，回退 16:9", ratio)
        return "16:9"

    # ------------------------------------------------------------ 参考图上传

    async def _upload_image(self, image_bytes: bytes, *, api_key: str) -> str:
        """presign → PUT → 返回公网 HTTPS URL。

        MiniMax 只收公网 URL，data URI / base64 一律不收，所以必须走这一步。
        """
        compressed = _compress_image_bytes_for_video(image_bytes)
        mime = _guess_image_mime(compressed)
        ext = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(mime, "jpg")
        file_name = f"aiimg-{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"

        logger.debug(
            "[MiniMaxVideo] 上传参考图: 原始=%s bytes, 压缩后=%s bytes, mime=%s",
            len(image_bytes),
            len(compressed),
            mime,
        )

        timeout = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=150.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.post(
                    self.presign_url,
                    json={
                        "file_name": file_name,
                        "content_type": mime,
                        "expires_in": 900,
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"presign HTTP {resp.status_code}: {_extract_error_message(resp)}"
                    )
                try:
                    payload = resp.json()
                except Exception as e:
                    raise RuntimeError(
                        f"presign 响应不是 JSON: {e}, body={resp.text[:200]}"
                    ) from e
                if not isinstance(payload, dict):
                    raise RuntimeError(f"presign 响应格式异常: {str(payload)[:200]}")
                if payload.get("success") is False:
                    raise RuntimeError(
                        str(
                            payload.get("message")
                            or payload.get("error")
                            or "网关返回 success=false（该账户可能未配置对象存储）"
                        )
                    )

                data = payload.get("data")
                if not isinstance(data, dict):
                    data = payload
                upload_url = str(
                    data.get("upload_url") or data.get("presigned_url") or ""
                ).strip()
                public_url = str(
                    data.get("public_url") or data.get("url") or ""
                ).strip()
                if not upload_url or not public_url:
                    raise RuntimeError(
                        f"presign 响应缺少 upload_url/public_url: {str(payload)[:300]}"
                    )

                # 预签名地址自带鉴权，不要带网关的 Authorization 头
                put_resp = await client.put(
                    upload_url,
                    content=compressed,
                    headers={"Content-Type": mime},
                )
                if put_resp.status_code not in (200, 201, 204):
                    raise RuntimeError(
                        f"对象存储写入失败 HTTP {put_resp.status_code}: {put_resp.text[:300]}"
                    )
        except RuntimeError as e:
            raise RuntimeError(f"MiniMax 上传参考图失败: {e}") from e
        except Exception as e:
            raise RuntimeError(f"MiniMax 上传参考图失败: {e}") from e

        logger.debug("[MiniMaxVideo] 参考图已上传: %s", public_url)
        return public_url

    # ---------------------------------------------------------------- 提交任务

    async def _submit_task(self, payload: dict, *, api_key: str) -> str:
        """提交任务，429/5xx/网络错误按指数退避重试；幂等 Key 全程复用。"""
        idempotency_key = uuid.uuid4().hex
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        timeout = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=150.0)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._submit_once(payload, headers=headers, timeout=timeout)
            except _RetryableSubmitError as e:
                last_exc = e
                if attempt >= self.max_retries:
                    break
                base_delay = e.retry_after if e.retry_after else 15.0 * (2**attempt)
                delay = min(60.0, base_delay) + random.uniform(0, 1.0)
                logger.warning(
                    "[MiniMaxVideo] 提交失败(第 %s 次): %s，%.1fs 后重试",
                    attempt + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as e:
                last_exc = e
                if attempt >= self.max_retries:
                    break
                delay = min(60.0, 15.0 * (2**attempt)) + random.uniform(0, 1.0)
                logger.warning(
                    "[MiniMaxVideo] 提交网络异常(第 %s 次): %s，%.1fs 后重试",
                    attempt + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)

        raise last_exc or RuntimeError("MiniMax 提交任务失败")

    async def _submit_once(
        self, payload: dict, *, headers: dict, timeout: httpx.Timeout
    ) -> str:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(self.create_url, json=payload, headers=headers)

        if resp.status_code not in (200, 201):
            detail = _extract_error_message(resp)
            lowered = detail.lower()
            if resp.status_code == 401:
                raise RuntimeError("MiniMax API Key 无效或已过期 (401)")
            if resp.status_code == 403:
                raise RuntimeError(f"MiniMax 访问被拒绝 (403): {detail}")
            if resp.status_code in (400, 404, 422):
                raise RuntimeError(f"MiniMax 提交失败 HTTP {resp.status_code}: {detail}")
            if any(hint in lowered for hint in _NON_RETRYABLE_HINTS):
                raise RuntimeError(
                    f"MiniMax 无可用渠道 HTTP {resp.status_code}: {detail}"
                )
            if resp.status_code in _RETRYABLE_HTTP:
                raise _RetryableSubmitError(
                    f"HTTP {resp.status_code}: {detail}",
                    retry_after=_parse_retry_after(resp),
                )
            raise RuntimeError(f"MiniMax 提交失败 HTTP {resp.status_code}: {detail}")

        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(
                f"MiniMax 提交响应解析失败: {e}, body={resp.text[:200]}"
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError(f"MiniMax 提交响应格式异常: {str(data)[:200]}")

        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            task_obj = data.get("task")
            if isinstance(task_obj, dict):
                task_id = task_obj.get("id") or task_obj.get("task_id")
        if not task_id:
            raise RuntimeError(f"MiniMax 响应中无任务 ID: {str(data)[:200]}")

        logger.info(
            "[MiniMaxVideo] 任务创建: id=%s, model=%s, duration=%ss, ratio=%s",
            task_id,
            payload.get("model"),
            payload.get("duration"),
            payload.get("ratio"),
        )
        return str(task_id)

    # ---------------------------------------------------------------- 轮询

    async def _poll_task(self, task_id: str, *, api_key: str) -> str:
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=90.0)
        poll_url = f"{self.create_url}/{task_id}"

        t_start = time.perf_counter()
        last_status = ""

        while True:
            elapsed = time.perf_counter() - t_start
            if elapsed > self.timeout_seconds:
                raise TimeoutError(
                    f"MiniMax 任务超时 ({self.timeout_seconds}s, status={last_status or 'unknown'})"
                )

            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as client:
                    resp = await client.get(poll_url, headers=headers)
            except httpx.HTTPError as e:
                logger.warning("[MiniMaxVideo] 轮询网络异常: %s，%ss 后重试", e, self.polling_interval)
                await asyncio.sleep(self.polling_interval)
                continue

            if resp.status_code == 429:
                wait = _parse_retry_after(resp) or float(self.polling_interval)
                logger.warning("[MiniMaxVideo] 轮询被限流，%.0fs 后重试", wait)
                await asyncio.sleep(min(wait, 60.0))
                continue
            if resp.status_code == 401:
                raise RuntimeError("MiniMax API Key 无效或已过期 (401)")
            if resp.status_code == 404:
                raise RuntimeError(f"MiniMax 任务不存在或不属于当前账户: {task_id}")
            if resp.status_code != 200:
                logger.warning(
                    "[MiniMaxVideo] 轮询 HTTP %s: %s，%ss 后重试",
                    resp.status_code,
                    _extract_error_message(resp),
                    self.polling_interval,
                )
                await asyncio.sleep(self.polling_interval)
                continue

            try:
                data = resp.json()
            except Exception as e:
                logger.warning("[MiniMaxVideo] 轮询响应非 JSON: %s，%ss 后重试", e, self.polling_interval)
                await asyncio.sleep(self.polling_interval)
                continue
            if not isinstance(data, dict):
                logger.warning(
                    "[MiniMaxVideo] 轮询响应格式异常: %s，%ss 后重试",
                    str(data)[:200],
                    self.polling_interval,
                )
                await asyncio.sleep(self.polling_interval)
                continue

            # 优先读嵌套 task，兼容层也可能把字段放在顶层
            task_obj = data.get("task")
            task = task_obj if isinstance(task_obj, dict) else data

            status = str(task.get("status") or data.get("status") or "").strip().lower()
            if status != last_status:
                logger.info(
                    "[MiniMaxVideo] task=%s status=%s progress=%s",
                    task_id,
                    status or "unknown",
                    task.get("progress", data.get("progress")),
                )
                last_status = status

            if status in _SUCCESS_STATUSES:
                video_url = self._extract_video_url(task, data)
                if video_url:
                    logger.info(
                        "[MiniMaxVideo] 任务完成: task=%s, 耗时=%.0fs",
                        task_id,
                        time.perf_counter() - t_start,
                    )
                    return video_url
                err = task.get("error") or data.get("error")
                raise RuntimeError(
                    f"MiniMax 任务成功但无视频 URL: task={task_id}, "
                    f"error={self._format_error(err) or 'none'}"
                )

            if status in _FAILED_STATUSES:
                err = task.get("error") or data.get("error")
                message = self._format_error(err) or str(task)[:200]
                raise RuntimeError(f"MiniMax 任务失败 ({status}): {message}")

            if status not in _PENDING_STATUSES:
                logger.debug("[MiniMaxVideo] 未知状态 %s，继续轮询", status)

            await asyncio.sleep(self.polling_interval)

    @staticmethod
    def _format_error(err: Any) -> str:
        if isinstance(err, dict):
            code = str(err.get("code") or "").strip()
            message = str(err.get("message") or "").strip()
            if code and message:
                return f"{code}: {message}"
            if code or message:
                return code or message
            return ""
        if isinstance(err, str):
            return err.strip()
        return ""

    @staticmethod
    def _extract_video_url(task: dict, root: dict) -> str | None:
        """按显式字段取出结果直链，最后才做深度兜底。

        成功直链可能在 ``task.content.url``、顶层 ``video_url`` / ``url`` / ``content.url``。
        """
        candidates: list[str] = []

        def _collect(source: dict) -> None:
            content = source.get("content")
            if isinstance(content, dict):
                url = content.get("url") or content.get("video_url")
                if isinstance(url, str):
                    candidates.append(url)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        url = item.get("url")
                        if isinstance(url, str):
                            candidates.append(url)
            for key in ("video_url", "url", "download_url"):
                val = source.get(key)
                if isinstance(val, str):
                    candidates.append(val)
                elif isinstance(val, dict):
                    nested = val.get("url")
                    if isinstance(nested, str):
                        candidates.append(nested)

        _collect(task)
        if task is not root:
            _collect(root)

        for raw in candidates:
            url = raw.strip()
            if _is_valid_video_url(url):
                return url
        # 显式字段里即使没有 .mp4 后缀也先接受（网关可能返回带签名的直链），
        # 但要排除图片地址：响应若回显了参考图，不能当成本次成片
        for raw in candidates:
            url = raw.strip()
            if not url.startswith(("http://", "https://")) or len(url) <= 10:
                continue
            if _IMAGE_URL_RE.search(url):
                continue
            return url

        return _deep_find_video_url(root)

    # ---------------------------------------------------------------- 入口

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
        **kwargs: Any,
    ) -> str:
        api_key = self._get_key()

        final_prompt = self.build_prompt(prompt, preset=preset)
        if not final_prompt:
            raise ValueError("缺少提示词")

        media_url = ""
        if image_bytes:
            media_url = await self._upload_image(image_bytes, api_key=api_key)
        else:
            remote = str(kwargs.get("image_url") or "").strip()
            if remote.startswith(("http://", "https://")):
                # 已经是公网 URL，省掉一次上传
                media_url = remote
                logger.debug("[MiniMaxVideo] 复用远程参考图: %s", remote[:100])
            elif remote.startswith("data:"):
                # 只有 data URI 没有 bytes：解出来自己上传，不能静默丢掉参考图
                decoded = self._decode_data_uri(remote)
                if decoded:
                    media_url = await self._upload_image(decoded, api_key=api_key)
                else:
                    logger.warning(
                        "[MiniMaxVideo] image_url 是 data URI 但解析失败，退化为文生视频"
                    )
            else:
                logger.debug("[MiniMaxVideo] 无参考图，纯文生视频模式")

        duration = _clamp_int(
            kwargs.get("duration", self.default_duration),
            default=self.default_duration,
            min_value=1,
            max_value=15,
        )
        ratio = self._normalize_ratio(kwargs.get("ratio", self.default_ratio))

        content: list[dict[str, Any]] = [{"type": "text", "text": final_prompt}]
        if media_url:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": media_url},
                    "role": self.image_role,
                }
            )

        payload: dict[str, Any] = {
            # self.model 每次调用时读取，MultiModelVideoCascade 靠运行时改写它
            "model": self.model,
            "content": content,
            "resolution": self.resolution,
            "duration": duration,
            "ratio": ratio,
        }

        task_id = await self._submit_task(payload, api_key=api_key)
        return await self._poll_task(task_id, api_key=api_key)
