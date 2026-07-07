from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
import time
from collections import deque
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from astrbot.api import logger


def _clamp_int(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, value_int))


def _guess_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/jpeg"


def _build_data_url(image_bytes: bytes) -> str:
    mime = _guess_image_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _compress_image_bytes_for_video(
    image_bytes: bytes, *, max_side: int = 2048, quality: int = 85
) -> bytes:
    try:
        from PIL import Image as PILImage
    except Exception:
        logger.warning("[compress_video] PIL 不可用，返回原始图片")
        return image_bytes

    try:
        with PILImage.open(io.BytesIO(image_bytes)) as im:
            original_size = len(image_bytes)
            original_dims = im.size

            if im.mode != "RGB":
                im = im.convert("RGB")

            w, h = im.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                nw = max(1, int(w * scale))
                nh = max(1, int(h * scale))
                resampling = getattr(
                    getattr(PILImage, "Resampling", PILImage), "LANCZOS"
                )
                im = im.resize((nw, nh), resampling)

            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            compressed = buf.getvalue()

            if len(compressed) >= original_size:
                logger.info(
                    "[compress_video] 压缩后未减小，使用原始图片: %s -> %s bytes",
                    original_size,
                    len(compressed),
                )
                return image_bytes

            logger.info(
                "[compress_video] 图片压缩完成: %s -> %s, %s -> %s bytes",
                original_dims,
                im.size,
                original_size,
                len(compressed),
            )
            return compressed
    except Exception as e:
        logger.warning(
            "[compress_video] 压缩失败，返回原始图片: %s", e
        )
        return image_bytes


def _looks_like_proxy_video_url(url: str) -> bool:
    lowered = (url or "").strip().lower()
    if "generated_video" in lowered:
        return True
    try:
        path = urlsplit(url).path or ""
    except Exception:
        path = ""
    match = re.search(r"/images/p_([A-Za-z0-9+/_=-]+)", path)
    if not match:
        return False
    token = match.group(1)
    padded = token + ("=" * (-len(token) % 4))
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(padded.encode("ascii")).decode("utf-8", errors="ignore")
        except Exception:
            continue
        decoded_l = decoded.lower()
        if "generated_video" in decoded_l:
            return True
        if any(ext in decoded_l for ext in (".mp4", ".webm", ".mov")):
            return True
    return False


def _is_valid_video_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    if len(url) < 10:
        return False
    if not url.startswith(("http://", "https://")):
        return False
    lowered = url.lower()
    if any(c in url for c in ["<", ">", '"', "'", "\n", "\r", "\t"]):
        return False
    if any(ext in lowered for ext in (".mp4", ".webm", ".mov")):
        return True
    if _looks_like_proxy_video_url(url):
        return True
    return False


_VIDEO_URL_RE = re.compile(
    r"(https?://[^\s<>\"')\]\}]+?\.(?:mp4|webm|mov)(?:\?[^\s<>\"')\]\}]*)?)",
    re.IGNORECASE,
)
_GENERIC_URL_RE = re.compile(
    r"(https?://[^\s<>\"')\]\}]+)",
    re.IGNORECASE,
)


def _extract_video_url_from_content(content: str) -> str | None:
    if not content:
        return None
    if "<video" in content and "src=" in content:
        html_patterns = [
            r'<video[^>]*src=["\']([^"\'>]+)["\'][^>]*>',
            r'src=["\']([^"\'>]+\.mp4[^"\'>]*)["\']',
        ]
        for pattern in html_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1).strip()
                if _is_valid_video_url(url):
                    return url
    match = _VIDEO_URL_RE.search(content)
    if match:
        url = match.group(1).strip()
        if _is_valid_video_url(url):
            return url
    md_patterns = [
        r"!?\[[^\]]*\]\(([^\)]+\.(?:mp4|webm|mov)[^\)]*)\)",
        r"!?\[[^\]]*\]:\s*([^\s]+\.(?:mp4|webm|mov)[^\s]*)",
    ]
    for pattern in md_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            url = match.group(1).strip()
            if _is_valid_video_url(url):
                return url
    for match in _GENERIC_URL_RE.finditer(content):
        url = match.group(1).strip().rstrip(".,;")
        if _is_valid_video_url(url):
            return url
    return None


def _deep_find_video_url(
    data: Any, *, max_depth: int = 6, max_nodes: int = 2000
) -> str | None:
    queue: deque[tuple[Any, int]] = deque([(data, 0)])
    seen = 0
    while queue:
        obj, depth = queue.popleft()
        seen += 1
        if seen > max_nodes:
            return None
        if depth > max_depth:
            continue
        if isinstance(obj, str):
            url = _extract_video_url_from_content(obj) or (
                obj.strip() if _is_valid_video_url(obj) else None
            )
            if url:
                return url
            continue
        if isinstance(obj, dict):
            for key in ("video_url", "file_url", "url", "href", "download_url"):
                val = obj.get(key)
                if isinstance(val, str) and _is_valid_video_url(val):
                    return val.strip()
                if isinstance(val, dict):
                    nested_url = val.get("url") or val.get("file_url")
                    if isinstance(nested_url, str) and _is_valid_video_url(nested_url):
                        return nested_url.strip()
            for val in obj.values():
                queue.append((val, depth + 1))
            continue
        if isinstance(obj, list):
            for item in obj:
                queue.append((item, depth + 1))
            continue
    return None


def _extract_video_url_from_response(
    response_data: Any,
) -> tuple[str | None, str | None]:
    try:
        if not isinstance(response_data, dict):
            return None, f"无效的响应格式: {type(response_data).__name__}"
        direct = response_data.get("video_url")
        if isinstance(direct, str) and _is_valid_video_url(direct):
            return direct, None
        choices = response_data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None, "API 响应缺少 choices"
        choice0 = choices[0]
        if not isinstance(choice0, dict):
            return None, "choices[0] 格式错误"
        message = choice0.get("message")
        if not isinstance(message, dict):
            return None, "choices[0] 缺少 message"
        content = message.get("content")
        if isinstance(content, str):
            url = _extract_video_url_from_content(content)
            if url:
                return url, None
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    url = _extract_video_url_from_content(part)
                    if url:
                        return url, None
                if isinstance(part, dict):
                    part_url = (
                        part.get("url")
                        or part.get("video_url")
                        or (
                            part.get("video_url", {})
                            if isinstance(part.get("video_url"), dict)
                            else None
                        )
                    )
                    if isinstance(part_url, str) and _is_valid_video_url(part_url):
                        return part_url, None
                    if isinstance(part_url, dict):
                        nested = part_url.get("url")
                        if isinstance(nested, str) and _is_valid_video_url(nested):
                            return nested, None
                    text = part.get("text")
                    if isinstance(text, str):
                        url = _extract_video_url_from_content(text)
                        if url:
                            return url, None
        for field in ("attachments", "media", "files"):
            items = message.get(field)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        url = (
                            item.get("url")
                            or item.get("file_url")
                            or item.get("video_url")
                        )
                        if isinstance(url, str) and _is_valid_video_url(url):
                            return url, None
        deep = _deep_find_video_url(response_data)
        if deep:
            return deep, None
        content_preview = ""
        if isinstance(content, str):
            content_preview = content[:200]
        logger.warning(
            f"[GrokVideo] 未能提取视频 URL，content 片段: {content_preview}..."
        )
        return None, "未能从 API 响应中提取到有效的视频 URL"
    except Exception as e:
        logger.warning(f"[GrokVideo] URL 提取异常: {e}")
        return None, f"URL 提取失败: {e}"


# ==============================================================================
# 原有 Grok 服务 (保留，防止报错)
# ==============================================================================
class GrokVideoService:
    def __init__(self, *, settings: dict):
        self.settings = settings if isinstance(settings, dict) else {}

        self.server_url: str = str(
            self.settings.get("server_url", "https://api.x.ai")
        ).rstrip("/")
        self.api_key: str = str(self.settings.get("api_key", "")).strip()
        self.model: str = (
            str(self.settings.get("model", "grok-imagine-0.9")).strip()
            or "grok-imagine-0.9"
        )

        self.timeout_seconds: int = _clamp_int(
            self.settings.get("timeout_seconds", 180),
            default=180,
            min_value=1,
            max_value=3600,
        )
        self.max_retries: int = _clamp_int(
            self.settings.get("max_retries", 2),
            default=2,
            min_value=0,
            max_value=10,
        )
        self.empty_response_retry: int = _clamp_int(
            self.settings.get("empty_response_retry", 2),
            default=2,
            min_value=0,
            max_value=10,
        )
        self.retry_delay: int = _clamp_int(
            self.settings.get("retry_delay", 2),
            default=2,
            min_value=0,
            max_value=60,
        )

        self.presets: dict[str, str] = self._load_presets()
        self.api_url = urljoin(self.server_url + "/", "v1/chat/completions")

        logger.info(
            "[GrokVideo] Initialized: model=%s, timeout=%ss, retries=%s, empty_retry=%s, presets=%s",
            self.model,
            self.timeout_seconds,
            self.max_retries,
            self.empty_response_retry,
            len(self.presets),
        )

    def _load_presets(self) -> dict[str, str]:
        presets: dict[str, str] = {}
        items = self.settings.get("presets", [])
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

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("Missing API key for video provider (api_key)")
        if not image_bytes:
            raise ValueError("缺少参考图")

        final_prompt = self.build_prompt(prompt, preset=preset)
        if not final_prompt:
            raise ValueError("缺少提示词")

        image_url = _build_data_url(image_bytes)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": final_prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self.timeout_seconds),
            write=120.0,
            pool=float(self.timeout_seconds) + 100.0,
        )

        async def _request_once() -> Any:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.post(self.api_url, json=payload, headers=headers)

            if resp.status_code != 200:
                detail = resp.text[:500]
                if resp.status_code == 401:
                    raise RuntimeError("Grok API Key 无效或已过期 (401)")
                if resp.status_code == 403:
                    raise RuntimeError("Grok API 访问被拒绝 (403)")
                raise RuntimeError(
                    f"Grok API 请求失败 HTTP {resp.status_code}: {detail}"
                )

            try:
                return resp.json()
            except Exception as e:
                text = (resp.text or "").strip()
                if text.startswith("data:"):
                    lines = [
                        ln.strip()
                        for ln in text.splitlines()
                        if ln.strip().startswith("data:")
                    ]
                    chunks: list[dict[str, Any]] = []
                    for ln in lines:
                        data_str = ln[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            chunks.append(json.loads(data_str))
                        except Exception:
                            continue
                    if chunks:
                        if all(
                            isinstance(c, dict)
                            and str(c.get("object", "")).endswith(".chunk")
                            for c in chunks
                        ):
                            content_parts: list[str] = []
                            for c in chunks:
                                for ch in c.get("choices", []) or []:
                                    delta = ch.get("delta") or {}
                                    part = delta.get("content")
                                    if isinstance(part, str) and part:
                                        content_parts.append(part)
                            content = "".join(content_parts)
                            return {
                                "choices": [
                                    {"message": {"content": content}}
                                ]
                            }
                        return chunks[-1]
                raise RuntimeError(
                    f"API 响应 JSON 解析失败: {e}, body={resp.text[:200]}"
                ) from e

        async def _request_with_retries() -> Any:
            last_exc: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    logger.info(
                        f"[GrokVideo] 调用 API attempt={attempt + 1}/{self.max_retries + 1}, "
                        f"prompt={final_prompt[:60]}..."
                    )
                    return await _request_once()
                except Exception as e:
                    last_exc = e
                    if attempt >= self.max_retries:
                        break
                    delay = max(0, self.retry_delay) + random.uniform(0, 0.5)
                    logger.warning(f"[GrokVideo] 请求失败: {e}，{delay:.1f}s 后重试...")
                    await asyncio.sleep(delay)
            raise last_exc or RuntimeError("请求失败")

        t_start = time.perf_counter()
        last_parse_error: str | None = None

        for attempt in range(self.empty_response_retry + 1):
            data = await _request_with_retries()
            video_url, parse_error = _extract_video_url_from_response(data)
            if video_url:
                t_end = time.perf_counter()
                logger.info(
                    f"[GrokVideo] 成功: 耗时={t_end - t_start:.2f}s, url={video_url[:80]}..."
                )
                return video_url

            last_parse_error = parse_error or "API 响应未包含视频 URL"
            if attempt >= self.empty_response_retry:
                break

            delay = max(0, self.retry_delay) + random.uniform(0, 0.5)
            logger.warning(
                f"[GrokVideo] 响应无视频URL: {last_parse_error}，{delay:.1f}s 后重试..."
            )
            await asyncio.sleep(delay)

        raise RuntimeError(f"Grok 视频生成失败: {last_parse_error}")


# ==============================================================================
# 新增 豆包 Seedance 服务 (兼容异步任务模式)
# ==============================================================================
class DoubaoSeedanceService:
    def __init__(self, *, settings: dict):
        self.settings = settings if isinstance(settings, dict) else {}

        self.server_url: str = str(
            self.settings.get("server_url", "https://ark.cn-beijing.volces.com")
        ).rstrip("/")
        self.api_key: str = str(self.settings.get("api_key", "")).strip()
        self.model: str = (
            str(self.settings.get("model", "doubao-seedance-1-5-pro-251215")).strip()
            or "doubao-seedance-1-5-pro-251215"
        )

        self.timeout_seconds: int = _clamp_int(
            self.settings.get("timeout_seconds", 300), default=300, min_value=60, max_value=3600
        )
        self.max_retries: int = _clamp_int(
            self.settings.get("max_retries", 1), default=1, min_value=0, max_value=5
        )
        self.polling_interval: int = _clamp_int(
            self.settings.get("polling_interval", 10), default=10, min_value=2, max_value=30
        )
        self.retry_delay: int = _clamp_int(
            self.settings.get("retry_delay", 2), default=2, min_value=0, max_value=60
        )

        self.default_ratio: str = str(self.settings.get("ratio", "9:16"))
        self.default_duration: int = _clamp_int(
            self.settings.get("duration", 6), default=6, min_value=2, max_value=12
        )
        self.default_resolution: str = str(self.settings.get("resolution", "1080p"))
        self.watermark: bool = bool(self.settings.get("watermark", False))
        self.generate_audio: bool = bool(self.settings.get("generate_audio", False))

        self.presets: dict[str, str] = self._load_presets()
        self.create_task_url = urljoin(self.server_url + "/", "api/v3/contents/generations/tasks")
        
        logger.info(
            "[DoubaoVideo] Initialized: model=%s", self.model
        )

    def _load_presets(self) -> dict[str, str]:
        presets: dict[str, str] = {}
        items = self.settings.get("presets", [])
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

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
        **kwargs
    ) -> str:
        if not self.api_key:
            raise RuntimeError("Missing API key (api_key)")

        final_prompt = self.build_prompt(prompt, preset=preset)
        if not final_prompt:
            raise ValueError("缺少提示词")

        content = [{"type": "text", "text": final_prompt}]
        if image_bytes:
            image_url = _build_data_url(image_bytes)
            content.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })

        payload = {
            "model": self.model,
            "content": content,
            "ratio": kwargs.get("ratio", self.default_ratio),
            "duration": kwargs.get("duration", self.default_duration),
            "resolution": kwargs.get("resolution", self.default_resolution),
            "watermark": kwargs.get("watermark", self.watermark),
            "generate_audio": kwargs.get("generate_audio", self.generate_audio),
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        timeout = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=130.0)
        
        task_id = await self._create_task(payload, headers, timeout)
        return await self._poll_task_result(task_id, headers, timeout)

    async def _create_task(self, payload: dict, headers: dict, timeout: httpx.Timeout) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                logger.info(f"[DoubaoVideo] 创建任务 attempt={attempt + 1}")
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.post(self.create_task_url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                task_id = resp.json().get("id")
                if not task_id: raise RuntimeError("响应中无 task id")
                logger.info(f"[DoubaoVideo] 任务创建: {task_id}")
                return task_id
            except Exception as e:
                last_exc = e
                if attempt >= self.max_retries: break
                await asyncio.sleep(self.retry_delay)
        raise last_exc or RuntimeError("创建任务失败")

    async def _poll_task_result(self, task_id: str, headers: dict, timeout: httpx.Timeout) -> str:
        t_start = time.perf_counter()
        get_url = f"{self.create_task_url}/{task_id}"
        
        while True:
            if time.perf_counter() - t_start > self.timeout_seconds:
                raise TimeoutError(f"任务超时 ({self.timeout_seconds}s)")

            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.get(get_url, headers=headers)
                data = resp.json()
                status = data.get("status")
                
                if status == "succeeded":
                    video_url = data.get("content", {}).get("video_url")
                    if video_url: return video_url
                    raise RuntimeError("任务成功但无 video_url")
                elif status == "failed":
                    err = data.get("error", {})
                    raise RuntimeError(f"任务失败: {err.get('message', 'Unknown')}")
                elif status in ["queued", "running"]:
                    await asyncio.sleep(self.polling_interval)
                else:
                    await asyncio.sleep(self.polling_interval)
            except Exception as e:
                if isinstance(e, (RuntimeError, TimeoutError)): raise
                await asyncio.sleep(5)
GrokVideoService = DoubaoSeedanceService


class GrokVideo3AsyncService:
    """Grok Video 3 异步任务后端（multipart/form-data 协议）。

    适用于 s.apifox 与 poloapi 提供的 multipart 接口：
    - POST /v1/videos (multipart/form-data): model/prompt/aspect_ratio/seconds/size/input_reference
    - GET /v1/videos/{task_id} 轮询: status (queued/processing/completed/failed/cancelled)
    - 完成时返回 video_url

    模型与时长映射：
    - grok-video-3 → 6s
    - grok-video-3-pro → 10s
    - grok-video-3-max → 15s
    """

    def __init__(self, *, settings: dict):
        self.settings = settings if isinstance(settings, dict) else {}

        self.server_url: str = str(
            self.settings.get("server_url", "https://poloai.top")
        ).rstrip("/")
        self.api_key: str = str(self.settings.get("api_key", "")).strip()
        self.model: str = (
            str(self.settings.get("model", "grok-video-3")).strip()
            or "grok-video-3"
        )

        self.timeout_seconds: int = _clamp_int(
            self.settings.get("timeout_seconds", 900), default=900, min_value=60, max_value=3600
        )
        self.polling_interval: int = _clamp_int(
            self.settings.get("polling_interval", 10), default=10, min_value=2, max_value=30
        )
        self.retry_delay: int = _clamp_int(
            self.settings.get("retry_delay", 2), default=2, min_value=0, max_value=60
        )

        self.default_aspect_ratio: str = str(self.settings.get("aspect_ratio", "16:9"))
        self.default_seconds: int = _clamp_int(
            self.settings.get("seconds", 6), default=6, min_value=1, max_value=15
        )
        self.default_size: str = str(self.settings.get("size", "720P"))

        self.create_url = urljoin(self.server_url + "/", "v1/videos")

        logger.info("[GrokVideo3] Initialized: model=%s, url=%s", self.model, self.server_url)

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
        **kwargs
    ) -> str:
        if not self.api_key:
            raise RuntimeError("Missing API key")

        if not prompt.strip():
            raise ValueError("缺少提示词")

        aspect_ratio = kwargs.get("aspect_ratio", self.default_aspect_ratio)
        seconds = kwargs.get("seconds", self.default_seconds)
        size = kwargs.get("size", self.default_size)
        image_url: str | None = str(kwargs.get("image_url", "") or "").strip() or None

        # 构建 multipart form 数据
        form_data: dict[str, str] = {
            "model": self.model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "seconds": str(seconds),
            "size": size,
        }

        # 参考图：multipart 协议的 input_reference 是 file 类型，只能上传文件。
        # 远程 URL 需要先下载为 bytes 再上传；image_bytes 直接上传。
        files_payload: list | None = None
        if image_bytes:
            original_bytes_size = len(image_bytes)
            image_bytes = _compress_image_bytes_for_video(image_bytes)
            mime = _guess_image_mime(image_bytes)
            ext_map = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }
            ext = ext_map.get(mime, ".jpg")
            files_payload = [
                ("input_reference", (f"reference{ext}", image_bytes, mime))
            ]
            logger.info(
                "[GrokVideo3] 附带参考图文件: 原始=%s bytes, 压缩后=%s bytes, mime=%s",
                original_bytes_size,
                len(image_bytes),
                mime,
            )
        else:
            logger.info("[GrokVideo3] 无参考图，纯文生视频模式")

        # 注意：multipart 请求不要手动设置 Content-Type，httpx 会自动加上 boundary
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=130.0)

        last_exc: Exception | None = None
        for attempt in range(self.retry_delay + 1 if self.retry_delay > 0 else 1):
            try:
                logger.info(
                    "[GrokVideo3] 创建任务 attempt=%s, model=%s, prompt=%s...",
                    attempt + 1,
                    self.model,
                    prompt[:60],
                )
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.post(
                        self.create_url,
                        data=form_data,
                        files=files_payload,
                        headers=headers,
                    )
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")

                result = resp.json()
                # poloapi 同时返回 id 和 task_id；s.apifox 只返回 id。优先 task_id
                task_id = result.get("task_id") or result.get("id")
                if not task_id:
                    raise RuntimeError(f"响应中无 task_id/id: {str(result)[:200]}")
                logger.info(f"[GrokVideo3] 任务创建: {task_id}")
                return await self._poll_task_result(task_id, headers=headers, timeout=timeout)
            except Exception as e:
                last_exc = e
                if attempt >= self.retry_delay:
                    break
                await asyncio.sleep(max(0, self.retry_delay))
        raise last_exc or RuntimeError("创建任务失败")

    async def _poll_task_result(
        self,
        task_id: str,
        *,
        headers: dict,
        timeout: httpx.Timeout,
    ) -> str:
        """轮询 GET /v1/videos/{task_id} 直到任务完成或失败。"""
        poll_url = urljoin(self.server_url + "/", f"v1/videos/{task_id}")
        t_start = time.perf_counter()
        while True:
            if time.perf_counter() - t_start > self.timeout_seconds:
                raise TimeoutError(f"任务超时 ({self.timeout_seconds}s)")

            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    q_resp = await client.get(poll_url, headers=headers)
                if q_resp.status_code != 200:
                    raise RuntimeError(f"轮询失败 HTTP {q_resp.status_code}: {q_resp.text[:300]}")
                q_data = q_resp.json()
            except Exception as e:
                if isinstance(e, (RuntimeError, TimeoutError)):
                    raise
                logger.warning("[GrokVideo3] 轮询异常: %s，5s 后重试", e)
                await asyncio.sleep(5)
                continue

            status = q_data.get("status")

            if status in ("completed", "succeeded"):
                video_url = q_data.get("video_url")
                if video_url:
                    logger.info(f"[GrokVideo3] 任务完成: {task_id}")
                    return video_url
                raise RuntimeError(f"任务完成但无 video_url: {str(q_data)[:200]}")
            elif status in ("failed", "cancelled"):
                error_obj = q_data.get("error", {})
                error_msg = error_obj.get("message", str(q_data)) if isinstance(error_obj, dict) else str(q_data)
                raise RuntimeError(f"任务失败 ({status}): {error_msg}")
            else:  # queued / processing
                if int(time.perf_counter() - t_start) % 60 < self.polling_interval:
                    logger.info(
                        f"[GrokVideo3] 轮询中: task={task_id}, status={status}, "
                        f"已等待 {int(time.perf_counter() - t_start)}s"
                    )
                await asyncio.sleep(self.polling_interval)


class OfficialGrokVideoService:
    """官方 Grok 视频生成后端，严格遵循 xAI 官方 API 文档。

    接口：POST /v1/videos/generations 创建，GET /v1/videos/{request_id} 轮询
    参数：aspect_ratio + resolution（无 size 字段）
    模型：grok-imagine-video
    """

    def __init__(self, *, settings: dict):
        self.settings = settings if isinstance(settings, dict) else {}

        self.server_url: str = str(
            self.settings.get("server_url", "https://api.x.ai")
        ).rstrip("/")
        self.api_key: str = str(self.settings.get("api_key", "")).strip()
        self.model: str = (
            str(self.settings.get("model", "grok-imagine-video")).strip()
            or "grok-imagine-video"
        )

        self.timeout_seconds: int = _clamp_int(
            self.settings.get("timeout_seconds", 900), default=900, min_value=60, max_value=3600
        )
        self.polling_interval: int = _clamp_int(
            self.settings.get("polling_interval", 5), default=5, min_value=2, max_value=30
        )

        self.default_aspect_ratio: str = str(self.settings.get("aspect_ratio", "16:9"))
        self.default_resolution: str = str(self.settings.get("resolution", "720p"))
        self.default_duration: int = _clamp_int(
            self.settings.get("duration", 6), default=6, min_value=1, max_value=15
        )

        self.create_url = urljoin(self.server_url + "/", "v1/videos/generations")
        self.edit_url = urljoin(self.server_url + "/", "v1/videos/edits")

        logger.info("[OfficialGrok] Initialized: model=%s, url=%s", self.model, self.server_url)

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
        **kwargs
    ) -> str:
        if not self.api_key:
            raise RuntimeError("Missing API key")

        if not prompt.strip():
            raise ValueError("缺少提示词")

        duration = kwargs.get("duration", self.default_duration)
        aspect_ratio = kwargs.get("aspect_ratio", self.default_aspect_ratio)
        resolution = kwargs.get("resolution", self.default_resolution)
        image_url: str | None = str(kwargs.get("image_url", "") or "").strip() or None
        # reference_images: list of remote URLs, mutually exclusive with image
        raw_ref_images = kwargs.get("reference_images") or []
        reference_images: list[str] = [
            str(u or "").strip()
            for u in raw_ref_images
            if str(u or "").strip().startswith(("http://", "https://"))
        ]

        if image_bytes:
            original_bytes_size = len(image_bytes)
            image_bytes = _compress_image_bytes_for_video(image_bytes)
            if not image_url or not image_url.startswith(("http://", "https://")):
                image_url = _build_data_url(image_bytes)
                logger.info(
                    "[OfficialGrok] image_url 非远程链接，已从 image_bytes 构建 data URL: "
                    "原始=%s bytes, 压缩后=%s bytes, data URL 长度=%s",
                    original_bytes_size,
                    len(image_bytes),
                    len(image_url),
                )
            else:
                logger.info(
                    "[OfficialGrok] 图片已压缩: %s -> %s bytes, 使用传入的 image_url",
                    original_bytes_size,
                    len(image_bytes),
                )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=130.0)

        body: dict = {
            "model": self.model,
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }
        # image 与 reference_images 互斥：优先 image（单图），其次 reference_images（多图）
        if image_url:
            body["image"] = {"url": image_url}
            logger.info("[OfficialGrok] 附带参考图: image_url 长度=%s", len(image_url))
        elif reference_images:
            body["reference_images"] = [{"url": u} for u in reference_images]
            logger.info(
                "[OfficialGrok] 附带参考图组: reference_images 数量=%s", len(reference_images)
            )
        else:
            logger.info("[OfficialGrok] 无参考图，纯文生视频模式")

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(self.create_url, json=body, headers=headers)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        result = resp.json()
        # xAI 官方返回 request_id；PoloAI 官方兼容格式返回 id / task_id
        request_id = result.get("request_id") or result.get("id") or result.get("task_id")
        if not request_id:
            raise RuntimeError(f"响应中无 request_id/id/task_id: {str(result)[:200]}")
        logger.info(f"[OfficialGrok] 任务创建: {request_id}")

        return await self._poll_video_task(request_id, headers=headers, timeout=timeout)

    async def edit_video_url(
        self,
        prompt: str,
        video_url: str,
        *,
        preset: str | None = None,
        **kwargs,
    ) -> str:
        """编辑视频：POST /v1/videos/edits

        参数：model, prompt, resolution, aspect_ratio, video: {"url": "..."}
        与 generate 共用轮询逻辑：GET /v1/videos/{request_id}
        """
        if not self.api_key:
            raise RuntimeError("Missing API key")
        if not prompt.strip():
            raise ValueError("缺少提示词")
        video_url = str(video_url or "").strip()
        if not video_url.startswith(("http://", "https://")):
            raise ValueError("video_url 必须是 http/https 远程链接")

        aspect_ratio = kwargs.get("aspect_ratio", self.default_aspect_ratio)
        resolution = kwargs.get("resolution", self.default_resolution)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=130.0)

        body: dict = {
            "model": self.model,
            "prompt": prompt,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "video": {"url": video_url},
        }
        logger.info("[OfficialGrok] 编辑视频: video_url 长度=%s", len(video_url))

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(self.edit_url, json=body, headers=headers)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        result = resp.json()
        # xAI 官方返回 request_id；PoloAI 官方兼容格式返回 id / task_id
        request_id = result.get("request_id") or result.get("id") or result.get("task_id")
        if not request_id:
            raise RuntimeError(f"响应中无 request_id/id/task_id: {str(result)[:200]}")
        logger.info(f"[OfficialGrok] 编辑任务创建: {request_id}")

        return await self._poll_video_task(request_id, headers=headers, timeout=timeout)

    async def _poll_video_task(
        self,
        request_id: str,
        *,
        headers: dict,
        timeout: httpx.Timeout,
    ) -> str:
        """轮询 GET /v1/videos/{request_id} 直到任务完成或失败。"""
        poll_url = urljoin(self.server_url + "/", f"v1/videos/{request_id}")
        t_start = time.perf_counter()
        while True:
            if time.perf_counter() - t_start > self.timeout_seconds:
                raise TimeoutError(f"任务超时 ({self.timeout_seconds}s)")

            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                q_resp = await client.get(poll_url, headers=headers)
            if q_resp.status_code != 200:
                raise RuntimeError(f"轮询失败 HTTP {q_resp.status_code}: {q_resp.text[:300]}")

            q_data = q_resp.json()
            status = q_data.get("status")

            if status == "done":
                video_obj = q_data.get("video")
                if isinstance(video_obj, dict):
                    video_url = video_obj.get("url")
                else:
                    video_url = q_data.get("video_url")
                if video_url:
                    logger.info(f"[OfficialGrok] 任务完成: {request_id}")
                    return video_url
                raise RuntimeError(f"任务完成但无 video URL: {str(q_data)[:200]}")
            elif status == "failed":
                error_obj = q_data.get("error", {})
                error_msg = error_obj.get("message", str(q_data)) if isinstance(error_obj, dict) else str(q_data)
                raise RuntimeError(f"任务失败: {error_msg}")
            elif status == "expired":
                raise RuntimeError("任务已过期")
            else:  # pending
                if int(time.perf_counter() - t_start) % 60 < self.polling_interval:
                    logger.info(f"[OfficialGrok] 轮询中: request={request_id}, 已等待 {int(time.perf_counter() - t_start)}s")
                await asyncio.sleep(self.polling_interval)


class TrueGrokVideoService:
    def __init__(self, registry, provider: dict):
        self._registry = registry
        self.provider_id = str(provider.get("id") or "").strip()
        self.label = str(provider.get("label") or self.provider_id).strip()
        raw_chain = provider.get("fallback_chain")
        if isinstance(raw_chain, list):
            self.fallback_chain = [str(pid or "").strip() for pid in raw_chain[:3] if pid]
        else:
            self.fallback_chain = []

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
        **kwargs
    ) -> str:
        if not self.fallback_chain:
            raise RuntimeError(f"TrueGrok({self.provider_id}): fallback_chain 为空")

        total = len(self.fallback_chain)
        last_error: Exception | None = None
        for i, pid in enumerate(self.fallback_chain):
            if pid == self.provider_id:
                logger.warning(f"[TrueGrok] 跳过循环引用: {pid}")
                continue
            try:
                backend = self._registry.get_video_backend(pid)
                logger.info(
                    "[TrueGrok] 尝试 %s/%s: %s (prompt=%s...)",
                    i + 1,
                    total,
                    pid,
                    prompt[:30],
                )
                result = await backend.generate_video_url(
                    prompt=prompt,
                    image_bytes=image_bytes,
                    preset=preset,
                    **kwargs,
                )
                logger.info("[TrueGrok] 成功: %s", pid)
                return result
            except Exception as e:
                last_error = e
                logger.warning("[TrueGrok] %s 失败, 尝试下一个: %s", pid, e)

        raise RuntimeError(
            f"TrueGrok({self.provider_id}): 所有 {total} 个后端均失败; 最后错误: {last_error}"
        ) from last_error


class MultiModelVideoCascade:
    """视频后端多模型级联：同一个 baseurl/apikey 下按顺序尝试多个模型。

    用法：在 provider 配置中填写 models 字段（字符串列表），非空时由 registry
    包装底层视频后端。调用 generate_video_url 时，依次将 backend.model 设置为
    列表中的每个模型名并调用，第一个成功即返回；全部失败则抛出聚合错误。

    设计说明：
    - 仅适用于视频后端（图片后端不在范围内）
    - 不适用于 TrueGrokVideoService（其本身已是 provider 级级联，无 model 属性）
    - 通过运行时修改 backend.model 实现，要求底层后端的 model 属性在 generate
      时被读取（而非仅在 __init__ 时被消费用于构造 URL）。当前所有视频后端均满足。
    """

    def __init__(self, backend: object, models: list[str]):
        self._backend = backend
        cleaned = [str(m or "").strip() for m in models if str(m or "").strip()]
        if not cleaned:
            raise ValueError("models 列表为空")
        self._models: list[str] = cleaned

    async def generate_video_url(
        self,
        prompt: str,
        image_bytes: bytes | None = None,
        *,
        preset: str | None = None,
        **kwargs
    ) -> str:
        total = len(self._models)
        if total == 1:
            # 单模型：直接设置并调用，不打多余日志
            self._backend.model = self._models[0]
            return await self._backend.generate_video_url(
                prompt=prompt,
                image_bytes=image_bytes,
                preset=preset,
                **kwargs,
            )

        last_error: Exception | None = None
        for i, model_name in enumerate(self._models):
            try:
                logger.info(
                    "[MultiModel] 尝试 %s/%s: model=%s (prompt=%s...)",
                    i + 1,
                    total,
                    model_name,
                    prompt[:30],
                )
                self._backend.model = model_name
                result = await self._backend.generate_video_url(
                    prompt=prompt,
                    image_bytes=image_bytes,
                    preset=preset,
                    **kwargs,
                )
                logger.info("[MultiModel] 成功: model=%s", model_name)
                return result
            except Exception as e:
                last_error = e
                logger.warning("[MultiModel] model=%s 失败, 尝试下一个: %s", model_name, e)

        raise RuntimeError(
            f"MultiModel: 所有 {total} 个模型均失败; 最后错误: {last_error}"
        ) from last_error