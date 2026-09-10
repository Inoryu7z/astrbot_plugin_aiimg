from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .image_format import guess_image_mime_and_ext
from .openai_full_url_backend import OpenAIFullURLBackend


class VercelSeedreamBackend(OpenAIFullURLBackend):
    """Vercel AI Gateway 上的豆包 Seedream 专用后端（OpenAI 兼容 /v1/images/edits）。

    契约要点（详见 VERCEL_AI_GATEWAY_BACKEND.md，结论均经真实请求实测）：
    - 图生图必须走 images/edits；images/generations 不认参考图字段。
    - 参考图必须是 ``images`` 数组，每项包 ``{"image_url": "data:<mime>;base64,..."}``，
      纯字符串数组或单条字符串都不符合网关契约。
    - 去水印必须双参数同时传：顶层 ``watermark: false``（父类已强制注入）+ 嵌套
      ``providerOptions.bytedance.watermark: false``（本类强制注入），缺一不可。
    - 输出恒为 JPEG（含 c2pa/jumb 内容凭据块，属正常，不是水印残留）。
    - ``size`` 最小像素硬下限 3,686,400（9:16 建议 1440x2560），低于直接 400。
    """

    def _merge_payload(
            self, payload: dict[str, Any], extra_body: dict | None = None
    ) -> dict[str, Any]:
        out = super()._merge_payload(payload, extra_body)
        # 强制关闭 bytedance 层英文水印：与父类注入的顶层 watermark=False 配合才能彻底去水印
        provider_options = out.get("providerOptions")
        if not isinstance(provider_options, dict):
            provider_options = {}
        bytedance = provider_options.get("bytedance")
        if not isinstance(bytedance, dict):
            bytedance = {}
        bytedance["watermark"] = False
        provider_options["bytedance"] = bytedance
        out["providerOptions"] = provider_options
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

        final_size = self._resolve_size(size, resolution)

        # 参考图全部 base64 data-URI 内联，绝不外传到第三方公网
        refs: list[dict[str, str]] = []
        for img in images:
            mime, _ext = guess_image_mime_and_ext(img)
            b64 = base64.b64encode(img).decode("utf-8")
            refs.append({"image_url": f"data:{mime};base64,{b64}"})

        base_payload: dict[str, Any] = {
            "model": final_model,
            "prompt": (prompt or "").strip() or "Edit this image",
        }
        if final_size:
            base_payload["size"] = final_size
        payload = self._merge_payload(base_payload, extra_body)
        # Vercel 网关契约：images 必须是对象数组，每项带 image_url 键
        payload["images"] = refs

        key = self._next_key()
        t0 = time.perf_counter()
        logger.debug(
            "[VercelSeedream][edit] refs=%d size=%s endpoint=%s",
            len(refs),
            final_size,
            endpoint,
        )
        response = await self._post_json(endpoint, key, payload)
        out = await self._save_response(response, endpoint_url=endpoint)
        logger.debug("[VercelSeedream][edit] 耗时: %.2fs", time.perf_counter() - t0)
        return out