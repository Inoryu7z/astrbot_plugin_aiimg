from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from astrbot.api import logger

from .gitee_sizes import normalize_size_text
from .output_spec import extract_prompt_ratio, resolve_size_from_ratio
from .image_format import guess_image_mime_and_ext
from .openai_compat_backend import _build_collage, resolution_to_size


def _origin(url: str) -> str:
    try:
        u = urlsplit(url)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return ""


def _is_http_url(url: str) -> bool:
    s = str(url or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _looks_like_ark_multi_image_generate_endpoint(url: str) -> bool:
    s = str(url or "").strip().lower()
    if not s:
        return False
    return (
        "volces.com" in s
        and "/images/generations" in s
        and ("/ark/" in s or "ark." in s or "/api/v3/" in s)
    )


# 明确不接受 sequential_image_generation 的模型：字节 Seedream 5.0 系（pro / flash）。
# 该参数只对旧版多图连续生成模型有效，5.0 系收到即 HTTP 400
# InvalidParameter: the parameter `sequential_image_generation` is not supported by the current model。
# 这里按模型名判定，不按 endpoint 猜——同一个火山 endpoint 也可能挂 4.0/4.5（仍需单图参数）。
_NO_SEQUENTIAL_MODEL_RE = re.compile(
    r"seedream[-_.]?5([-_.]?0)?([-_.]?(pro|flash))?(\b|$)", re.IGNORECASE
)

# 只认 1K / 1.5K / 2K 档位与「宽x高」像素值的模型：把 4K 之类的档位打回默认，
# 否则请求在调大模型之前就被服务端参数校验挡掉。
_SEEDREAM_5_ALLOWED_SIZE_RE = re.compile(r"^(\d+)\s*[xX*]\s*(\d+)$")

# 5.0 系像素值约束（官方文档：总像素 [1280x720, 2048x2048x1.1025]，宽高比 [1/16, 16]）
_SEEDREAM_5_MIN_PIXELS = 1280 * 720
_SEEDREAM_5_MAX_PIXELS = int(2048 * 2048 * 1.1025)


def _is_seedream_5_model(model: str) -> bool:
    # 注意：seedream-5-5 这类未来型号也会命中（5 后跟 -/词边界即视为 5.0 系）。
    # 方向安全：对 5.x 多剥一个参数无害；若后续版本恢复支持该参数，再收紧正则。
    return bool(_NO_SEQUENTIAL_MODEL_RE.search(str(model or "").strip()))


def _size_supported_by_seedream_5(size: str) -> bool:
    """size 是否在 5.0 系可接受范围内（档位 1K/1.5K/2K 或合规像素值）。

    AUTO 只在图层拆分场景合法，图片生成场景同样会被拒，故不列入。
    """
    s = str(size or "").strip()
    if not s:
        return True
    if s.upper() in {"1K", "1.5K", "2K"}:
        return True
    m = _SEEDREAM_5_ALLOWED_SIZE_RE.match(s)
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return False
    px = w * h
    if not (_SEEDREAM_5_MIN_PIXELS <= px <= _SEEDREAM_5_MAX_PIXELS):
        return False
    return 1 / 16 <= w / h <= 16


def _extract_image_ref(data: Any) -> str | None:
    """Extract first image ref from OpenAI-like response body."""
    if isinstance(data, dict):
        items = data.get("data")
        if isinstance(items, list):
            for item in items:
                ref = _extract_image_ref(item)
                if ref:
                    return ref

        output = data.get("output")
        if isinstance(output, (list, dict)):
            ref = _extract_image_ref(output)
            if ref:
                return ref

        b64 = data.get("b64_json")
        if isinstance(b64, str) and b64.strip():
            return f"data:image/png;base64,{b64.strip()}"

        for key in ("url", "image_url", "image"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            ref = _extract_image_ref(value)
            if ref:
                return ref
        return None

    if isinstance(data, list):
        for item in data:
            ref = _extract_image_ref(item)
            if ref:
                return ref
        return None

    if isinstance(data, str):
        s = data.strip()
        if s.startswith("data:image/"):
            return s
        if s.startswith(("http://", "https://", "/")):
            return s
        return None

    return None


class OpenAIFullURLBackend:
    """OpenAI-compatible image backend using user-provided full endpoint URLs."""

    def __init__(
            self,
            *,
            imgr,
            full_generate_url: str,
            api_keys: list[str],
            default_model: str = "",
            full_edit_url: str = "",
            timeout: int = 120,
            max_retries: int = 2,
            default_size: str = "4096x4096",
            supports_edit: bool = True,
            extra_body: dict | None = None,
            user_agent: str | None = None,
    ):
        self.imgr = imgr
        self.full_generate_url = str(full_generate_url or "").strip()
        self.full_edit_url = str(full_edit_url or "").strip()
        self.api_keys = [str(k).strip() for k in (api_keys or []) if str(k).strip()]
        self.default_model = str(default_model or "").strip()
        self.timeout = max(1, int(timeout or 120))
        self.max_retries = max(0, int(max_retries or 0))
        self.default_size = normalize_size_text(
            str(default_size or "4096x4096").strip()
        )
        self.supports_edit = bool(supports_edit)
        self.extra_body = extra_body or {}

        self._key_index = 0
        self._client: httpx.AsyncClient | None = None
        self._user_agent = user_agent

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers: dict[str, str] = {}
            if self._user_agent:
                headers["User-Agent"] = self._user_agent
            self._client = httpx.AsyncClient(
                timeout=float(self.timeout), follow_redirects=True,
                headers=headers or None,
            )
        return self._client

    def _next_key(self) -> str:
        if not self.api_keys:
            raise RuntimeError("未配置 API Key")
        key = self.api_keys[self._key_index]
        self._key_index = (self._key_index + 1) % len(self.api_keys)
        return key

    @staticmethod
    def _headers(api_key: str, *, is_json: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {api_key}"}
        if is_json:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
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

    def _resolve_size(
        self, size: str | None, resolution: str | None, prompt: str = "",
        model: str | None = None,
    ) -> str:
        """决定最终 size。

        优先级：显式 size → resolution → provider 默认尺寸。
        再加一层兜底：若最终值是「档位」（如 4k / 1.5k）且提示词里写了「宽高比为X」，
        就换算成该档位下对应比例的像素尺寸，免得提供商不按提示词的比例出图。
        换算不成立（档位不在表里、提示词里没有宽高比）时保持原值，交给提供商处理。

        Seedream 5.0 系只认 1K / 1.5K / 2K 与合规像素值，4K 档的换算结果必然被服务端
        拒绝，因此对这类模型跳过换算，档位不合规时直接换成自身默认合法尺寸。
        """
        final_model = str(model or self.default_model or "").strip()
        is_sd5 = _is_seedream_5_model(final_model)

        raw_size = normalize_size_text(size)
        raw_res = normalize_size_text(resolution)
        tier = raw_size or raw_res or self.default_size
        if is_sd5 and not _size_supported_by_seedream_5(tier):
            fallback = self._seedream_5_fallback_size()
            logger.info(
                "[OpenAIFullURL] 模型 %s 不支持档位 %s，改用 %s（宽高比交给提示词）",
                final_model,
                tier,
                fallback,
            )
            return fallback
        pixel = resolve_size_from_ratio(prompt, tier)
        if pixel:
            if is_sd5 and not _size_supported_by_seedream_5(pixel):
                logger.info(
                    "[OpenAIFullURL] 模型 %s 不接受换算尺寸 %s，保留档位 %s",
                    final_model,
                    pixel,
                    tier,
                )
                return tier
            logger.debug(
                "[OpenAIFullURL] 按提示词宽高比换算尺寸: %s + %s → %s",
                tier,
                extract_prompt_ratio(prompt),
                pixel,
            )
            return pixel
        if raw_size:
            return raw_size
        converted = normalize_size_text(resolution_to_size(str(resolution or "")))
        if converted:
            return converted
        return self.default_size

    @staticmethod
    def _collect_local_options(*sources: dict | None) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        for src in sources:
            if not isinstance(src, dict):
                continue
            for key, val in src.items():
                k = str(key or "").strip()
                if not k.startswith("__"):
                    continue
                opts[k] = val
        return opts

    @staticmethod
    def _strip_local_keys(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in payload.items() if not str(k or "").strip().startswith("__")
        }

    def _merge_payload(
            self, payload: dict[str, Any], extra_body: dict | None = None
    ) -> dict[str, Any]:
        out = dict(payload)
        if isinstance(self.extra_body, dict):
            out.update(self.extra_body)
        if isinstance(extra_body, dict):
            out.update(extra_body)
        out["watermark"] = False
        # 模型名校验必须放在合并之后：extra_body 里的 model 会覆盖构造时传入的默认值，
        # 只有以最终 payload 的 model 为准才判断得准
        final_model = str(out.get("model") or self.default_model or "").strip()
        if _is_seedream_5_model(final_model):
            out.pop("sequential_image_generation", None)
            size = out.get("size")
            if size and not _size_supported_by_seedream_5(str(size)):
                fallback = self._seedream_5_fallback_size()
                logger.info(
                    "[OpenAIFullURL] 模型 %s 不支持 size=%s，改用 %s",
                    final_model,
                    size,
                    fallback,
                )
                out["size"] = fallback
        return self._strip_local_keys(out)

    def _seedream_5_fallback_size(self) -> str:
        """5.0 系的尺寸兜底：服务商配的档位合法就沿用，否则 2K（5.0 支持的最高档）。"""
        if _size_supported_by_seedream_5(self.default_size):
            return self.default_size
        return "2K"

    async def _post_json(
        self,
        url: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        client = self._get_client()
        attempts = self.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = await client.post(
                    url,
                    headers=self._headers(api_key, is_json=True),
                    json=payload,
                )
                if (
                    self._is_retryable_status(resp.status_code)
                    and attempt + 1 < attempts
                ):
                    await asyncio.sleep(min(2.0, 0.4 * (2**attempt)))
                    continue
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2.0, 0.4 * (2**attempt)))
                    continue

        raise RuntimeError(
            f"请求失败（已重试 {self.max_retries} 次）: {last_exc}"
        ) from last_exc

    async def _post_multipart(
        self,
        url: str,
        api_key: str,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> httpx.Response:
        client = self._get_client()
        attempts = self.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = await client.post(
                    url,
                    headers=self._headers(api_key, is_json=False),
                    data=data,
                    files=files,
                )
                if (
                    self._is_retryable_status(resp.status_code)
                    and attempt + 1 < attempts
                ):
                    await asyncio.sleep(min(2.0, 0.4 * (2**attempt)))
                    continue
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2.0, 0.4 * (2**attempt)))
                    continue

        raise RuntimeError(
            f"请求失败（已重试 {self.max_retries} 次）: {last_exc}"
        ) from last_exc

    @staticmethod
    def _coerce_form_value(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (str, int, float, bool)):
            return str(v)
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)

    async def _save_ref(self, ref: str, *, endpoint_url: str) -> Path:
        ref = (ref or "").strip()
        if not ref:
            raise RuntimeError("空图片引用")

        if ref.startswith("data:image/"):
            try:
                _header, b64_data = ref.split(",", 1)
            except ValueError:
                raise RuntimeError("data:image 缺少 base64 数据") from None
            image_bytes = base64.b64decode((b64_data or "").strip())
            return await self.imgr.save_image(image_bytes)

        if _is_http_url(ref):
            return await self.imgr.download_image(ref)

        origin = _origin(endpoint_url)
        if origin and ref.startswith("/"):
            return await self.imgr.download_image(
                urljoin(origin + "/", ref.lstrip("/"))
            )
        if origin:
            return await self.imgr.download_image(urljoin(origin + "/", ref))
        raise RuntimeError(f"不支持的图片引用: {ref}")

    async def _save_response(self, resp: httpx.Response, *, endpoint_url: str) -> Path:
        if resp.status_code != 200:
            raise RuntimeError(
                f"请求失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}"
            )

        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type.startswith("image/"):
            return await self.imgr.save_image(resp.content)

        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"返回内容不是有效 JSON: {(resp.text or '')[:200]}") from exc

        ref = _extract_image_ref(data)
        if not ref:
            raise RuntimeError(f"未在响应中找到图片地址/数据: {str(data)[:200]}")

        return await self._save_ref(ref, endpoint_url=endpoint_url)

    def _effective_model(self, model: str | None, extra_body: dict | None) -> str:
        """预合并视角的最终模型名：extra_body 里的 model 会覆盖其他来源，
        _resolve_size 的模型判定必须与 _merge_payload 用同一个值，否则
        「默认 5.0 + extra_body 覆盖成 4.0」这类配置会出现 size 判定错位。
        """
        for src in (
            extra_body.get("model") if isinstance(extra_body, dict) else None,
            self.extra_body.get("model") if isinstance(self.extra_body, dict) else None,
            model,
            self.default_model,
        ):
            v = str(src or "").strip()
            if v:
                return v
        return ""

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
        extra_body: dict | None = None,
    ) -> Path:
        if not self.full_generate_url:
            raise RuntimeError("未配置 full_generate_url（完整文生图 URL）")

        final_model = str(model or self.default_model or "").strip()
        if not final_model:
            raise RuntimeError("未配置 model")

        final_size = self._resolve_size(
            size, resolution, prompt,
            model=self._effective_model(final_model, extra_body),
        )
        payload: dict[str, Any] = {
            "model": final_model,
            "prompt": (prompt or "").strip() or "a high quality image",
        }
        if final_size:
            payload["size"] = final_size
        payload = self._merge_payload(payload, extra_body)

        key = self._next_key()
        t0 = time.perf_counter()
        resp = await self._post_json(self.full_generate_url, key, payload)
        out = await self._save_response(resp, endpoint_url=self.full_generate_url)
        logger.debug("[OpenAIFullURL][generate] 耗时: %.2fs", time.perf_counter() - t0)
        return out

    async def edit(
        self,
        prompt: str,
        images: list[bytes],
        *,
        model: str | None = None,
        size: str | None = None,
        resolution: str | None = None,
        extra_body: dict | None = None,
    ) -> Path:
        if not self.supports_edit:
            raise RuntimeError("该后端不支持改图/图生图")
        if not images:
            raise ValueError("至少需要一张图片")

        endpoint = self.full_edit_url or self.full_generate_url
        if not endpoint:
            raise RuntimeError("未配置 full_edit_url 或 full_generate_url")

        final_model = str(model or self.default_model or "").strip()
        if not final_model:
            raise RuntimeError("未配置 model")

        # 注入闸与 _resolve_size 必须用同一个「预合并视角」的模型名：
        # sequential 在 merge 之后才注入，此时用 pre-merge 的 final_model 会放行
        # 「extra_body 把 model 覆盖成 5.0」的请求
        check_model = self._effective_model(final_model, extra_body)
        final_size = self._resolve_size(
            size, resolution, prompt, model=check_model,
        )
        local_opts = self._collect_local_options(self.extra_body, extra_body)
        edit_mode = str(local_opts.get("__edit_mode") or "auto").strip().lower()
        if not edit_mode or edit_mode == "auto":
            edit_mode = "json_image_array"

        base_payload: dict[str, Any] = {
            "model": final_model,
            "prompt": (prompt or "").strip() or "Edit this image",
        }
        if final_size:
            base_payload["size"] = final_size
        base_payload = self._merge_payload(base_payload, extra_body)

        key = self._next_key()
        t0 = time.perf_counter()

        if edit_mode == "json_image_array":
            image_field = str(local_opts.get("__edit_image_field") or "image").strip() or "image"
            single_as_array = self._as_bool(
                local_opts.get("__edit_single_image_as_array"), default=False
            )
            force_single_output = self._as_bool(
                local_opts.get("__edit_force_single_output"),
                default=_looks_like_ark_multi_image_generate_endpoint(endpoint),
            )

            refs: list[str] = []
            for img in images:
                mime, _ext = guess_image_mime_and_ext(img)
                b64 = base64.b64encode(img).decode("utf-8")
                refs.append(f"data:{mime};base64,{b64}")

            payload = dict(base_payload)
            payload[image_field] = refs if (len(refs) > 1 or single_as_array) else refs[0]
            if (
                force_single_output
                and "sequential_image_generation" not in payload
                # Seedream 5.0 系（pro/flash）不支持该参数，收到即 HTTP 400
                and not _is_seedream_5_model(check_model)
            ):
                payload["sequential_image_generation"] = "disabled"

            logger.debug(
                "[OpenAIFullURL][edit] mode=json_image_array refs=%s field=%s endpoint=%s",
                len(refs),
                image_field,
                endpoint,
            )
            response = await self._post_json(endpoint, key, payload)
            out = await self._save_response(response, endpoint_url=endpoint)
            logger.debug("[OpenAIFullURL][edit] 耗时: %.2fs", time.perf_counter() - t0)
            return out

        merged_img = _build_collage(images)
        mime, ext = guess_image_mime_and_ext(merged_img)
        b64 = base64.b64encode(merged_img).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

        logger.debug(
            "[OpenAIFullURL][edit] mode=collage refs=%s endpoint=%s",
            len(images),
            endpoint,
        )

        response: httpx.Response | None = None
        for json_payload in (
            dict(base_payload, image=data_url),
            dict(base_payload, images=[data_url]),
            dict(base_payload, image_url=data_url),
        ):
            resp = await self._post_json(endpoint, key, json_payload)
            response = resp
            if resp.status_code == 200:
                break
            if resp.status_code not in {400, 415, 422}:
                break

        if response is None:
            raise RuntimeError("改图请求未得到有效响应")

        if response.status_code != 200:
            form_data = {
                str(k): self._coerce_form_value(v) for k, v in base_payload.items()
            }
            response = await self._post_multipart(
                endpoint,
                key,
                data=form_data,
                files={"image": (f"image.{ext}", merged_img, mime)},
            )

        out = await self._save_response(response, endpoint_url=endpoint)
        logger.debug("[OpenAIFullURL][edit] 耗时: %.2fs", time.perf_counter() - t0)
        return out
