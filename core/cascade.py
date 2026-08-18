from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .gitee_edit import GiteeEditBackend

# Gitee 改图接口只接受 prompt/images/task_types，无法透传 size/resolution。
# 与 edit_router 的默认 gitee_task_types 保持一致。
_DEFAULT_GITEE_TASK_TYPES = ("id", "background", "style")


class TrueGrokImageService:
    """图片后端级联：fallback_chain 内依次调用 generate/edit，失败自动顺延。

    对应 provider 的 __template_key = "truegrok"，kind 推断为 "image"。
    fallback_chain 内必须全部是图片后端（如 openai_images / openai_full_url_images /
    ark_seedream / jimeng 等），不支持与视频后端混用，也不支持嵌套级联。

    支持 LLM / 命令显式传参调用：把该 provider 的 id 或显示名称作为
    backend / @provider 传入即可，走 draw_service / edit_router 的既有链路。
    """

    def __init__(self, registry, provider: dict):
        self._registry = registry
        self.provider_id = str(provider.get("id") or "").strip()
        self.label = str(provider.get("label") or self.provider_id).strip()
        raw_chain = provider.get("fallback_chain")
        if isinstance(raw_chain, list):
            self.fallback_chain = [
                str(pid or "").strip() for pid in raw_chain[:3] if pid
            ]
        else:
            self.fallback_chain = []

    async def generate(
        self,
        prompt: str,
        *,
        size: str | None = None,
        resolution: str | None = None,
    ) -> Any:
        return await self._call(
            "generate", prompt=prompt, size=size, resolution=resolution
        )

    async def edit(
        self,
        prompt: str,
        images: list,
        *,
        size: str | None = None,
        resolution: str | None = None,
    ) -> Any:
        return await self._call(
            "edit",
            prompt=prompt,
            images=images,
            size=size,
            resolution=resolution,
        )

    async def _call(self, method: str, **kwargs) -> Any:
        if not self.fallback_chain:
            raise RuntimeError(f"TrueGrok({self.provider_id}): fallback_chain 为空")
        total = len(self.fallback_chain)
        last_error: Exception | None = None
        for i, pid in enumerate(self.fallback_chain):
            if pid == self.provider_id:
                logger.warning("[TrueGrokImage] 跳过循环引用: %s", pid)
                continue
            try:
                backend = self._registry.get_backend(pid)
                logger.info(
                    "[TrueGrokImage] 尝试 %s/%s: %s (method=%s)",
                    i + 1, total, pid, method,
                )
                if method == "edit" and isinstance(backend, GiteeEditBackend):
                    result = await backend.edit(
                        kwargs["prompt"],
                        kwargs["images"],
                        task_types=_DEFAULT_GITEE_TASK_TYPES,
                    )
                else:
                    fn = getattr(backend, method, None)
                    if not callable(fn):
                        raise RuntimeError(f"{pid} 不支持 {method}()")
                    result = await fn(**kwargs)
                if not result:
                    raise RuntimeError(f"{pid} 返回空结果")
                logger.info("[TrueGrokImage] 成功: %s", pid)
                return result
            except Exception as e:
                last_error = e
                logger.warning("[TrueGrokImage] %s 失败, 尝试下一个: %s", pid, e)
        raise RuntimeError(
            f"TrueGrok({self.provider_id}): 所有 {total} 个后端均失败; 最后错误: {last_error}"
        ) from last_error
