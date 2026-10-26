from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from astrbot.api import logger

from .gitee_edit import GiteeEditBackend
from .output_spec import parse_output
from .provider_chain import as_dict, as_list, candidates_from_chain
from .provider_registry import ProviderRegistry

# 火山 ARK 输入文本敏感拦截的错误码（HTTP 400 body 里带）。
# 这是内容问题不是服务问题：换个 provider / 重试同样的内容大概率还是被拦，
# 因此连续出现 2 次就直接终止本 provider 的剩余重试与整条兜底链，立刻报错。
_INPUT_SENSITIVE_MARK = "InputTextSensitiveContentDetected"
_INPUT_SENSITIVE_STRIKES = 2


class EditRouter:
    """Image-to-Image router for v4 config (provider chain + presets)."""

    def __init__(
        self,
        config: dict,
        imgr,
        data_dir: Path,
        *,
        registry: ProviderRegistry | None = None,
    ):
        self.config = config if isinstance(config, dict) else {}
        self.imgr = imgr
        self.data_dir = Path(data_dir)
        self.registry = registry or ProviderRegistry(
            self.config, imgr=self.imgr, data_dir=self.data_dir
        )

        self.presets = self._load_presets()

        self.last_success_provider: str | None = None

        logger.info(
            "[EditRouter] Initialized: presets=%s, providers=%s",
            len(self.presets),
            len(self.registry.provider_ids()),
        )

    def _feature_conf(self) -> dict:
        feats = as_dict(self.config.get("features"))
        return as_dict(feats.get("edit"))

    def _default_output(self) -> str:
        return str(self._feature_conf().get("default_output") or "").strip()

    def _chain(self) -> list:
        return as_list(self._feature_conf().get("chain"))

    def _load_presets(self) -> dict[str, str]:
        presets: dict[str, str] = {}
        for item in as_list(self._feature_conf().get("presets")):
            if isinstance(item, str) and ":" in item:
                key, val = item.split(":", 1)
                key = key.strip()
                val = val.strip()
                if key and val:
                    presets[key] = val
        return presets

    def get_preset_names(self) -> list[str]:
        return list(self.presets.keys())

    def get_available_backends(self) -> list[str]:
        out: list[str] = []
        for pid in self.registry.provider_ids():
            try:
                backend = self.registry.get_backend(pid)
                edit_fn = getattr(backend, "edit", None)
                if callable(edit_fn):
                    out.append(pid)
            except Exception:
                continue
        return out

    async def close(self) -> None:
        await self.registry.close()

    @staticmethod
    def _candidates_from_chain(raw_chain: list) -> list[tuple[str, str]]:
        return candidates_from_chain(raw_chain)

    def _candidate_chain(
        self, backend: str | None, chain_override: list | None
    ) -> list[tuple[str, str]]:
        if backend:
            return [(str(backend).strip(), "")]
        if chain_override is not None:
            return self._candidates_from_chain(chain_override)
        return self._candidates_from_chain(self._chain())

    def _default_gitee_task_types(self) -> list[str]:
        raw = as_list(self._feature_conf().get("gitee_task_types"))
        out = [str(x).strip() for x in raw if str(x).strip()]
        return out or ["id", "background", "style"]

    async def edit(
        self,
        prompt: str,
        images: list[bytes],
        backend: str | None = None,
        preset: str | None = None,
        task_types: Iterable[str] = ("id",),
        *,
        size: str | None = None,
        resolution: str | None = None,
        default_output: str | None = None,
        chain_override: list | None = None,
        prepare_images: Callable[[str], list[bytes]] | None = None,
        prepare_prompt: Callable[[str], str] | None = None,
        on_attempt_error: Callable[[str, int, Exception], None] | None = None,
    ) -> Path:
        feature = self._feature_conf()
        if not bool(feature.get("enabled", True)):
            raise RuntimeError("Image edit is disabled (features.edit.enabled=false)")
        if not images:
            raise ValueError("At least one image is required")

        if preset and preset in self.presets:
            p = self.presets[preset]
            prompt = f"{p}, {prompt}" if prompt else p
        if not prompt:
            prompt = "Transform this image with artistic style"

        candidates = self._candidate_chain(backend, chain_override)
        if not candidates:
            raise RuntimeError(
                "No edit providers configured. Please add providers and set features.edit.chain."
            )
        logger.debug(
            "[edit] candidates=%s backend_override=%s chain_override=%s",
            [pid for pid, _ in candidates],
            str(backend or ""),
            chain_override is not None,
        )
        if backend:
            logger.debug("[edit] backend override active, chain fallback disabled")

        if default_output is not None:
            effective_default_output = str(default_output).strip()
        else:
            effective_default_output = self._default_output()
        gitee_default_task_types = self._default_gitee_task_types()
        final_task_types = (
            list(task_types) if list(task_types) else gitee_default_task_types
        )

        last_error: Exception | None = None
        last_failed_pid: str | None = None

        def _report_attempt_error(pid_str: str, attempt_no: int, err: Exception) -> None:
            """把单次尝试的失败如实上报给调用方；回调自身异常不影响主流程。"""
            if on_attempt_error is None:
                return
            try:
                on_attempt_error(pid_str, attempt_no, err)
            except Exception as cb_err:
                logger.debug("[edit] on_attempt_error 回调异常: %s", cb_err)

        # 敏感内容拦截连续计数：成功会直接 return，所以能走到这里说明中间没有成功，
        # strikes 天然是「连续」的（同 provider 重试或换 provider 都算续上）
        sensitive_strikes = 0
        t_start = time.perf_counter()

        for pid, out_override in candidates:
            logger.info("[edit] 尝试 Provider=%s ...", pid)

            try:
                backend_obj = self.registry.get_backend(pid)
            except Exception as e:
                last_error = e
                last_failed_pid = pid
                _report_attempt_error(pid, 0, e)
                logger.info("[edit] Provider=%s 后端构建失败: %s", pid, e)
                continue

            # 按本次实际命中的 provider 准备输入图（如 ark_seedream 仅保留第一张人设图）。
            # 回调缺省 None 时行为与旧版一致，所见即 images。
            call_images = images
            if prepare_images is not None:
                try:
                    prepared = prepare_images(pid)
                except Exception as e:
                    logger.warning(
                        "[edit] prepare_images(%s) 失败，使用原始图: %s", pid, e
                    )
                    prepared = None
                if prepared:
                    call_images = prepared

            # 提示词同样按命中者准备：人设图张数一变，额外参考图的「参考图N」编号要跟着平移
            call_prompt = prompt
            if prepare_prompt is not None:
                try:
                    prepared_prompt = prepare_prompt(pid)
                except Exception as e:
                    logger.warning(
                        "[edit] prepare_prompt(%s) 失败，使用原始提示词: %s", pid, e
                    )
                    prepared_prompt = None
                if prepared_prompt:
                    call_prompt = prepared_prompt

            if size or resolution:
                final_size = size
                final_res = resolution
            else:
                output = out_override or effective_default_output
                out_size, out_res = parse_output(output)
                final_size = out_size
                final_res = out_res

            provider_conf = self.registry.get(pid) or {}
            max_retries = int(provider_conf.get("max_retries") or 0)
            max_attempts = max(1, max_retries + 1)

            for attempt in range(max_attempts):
                try:
                    self.last_success_provider = pid
                    edit_fn = getattr(backend_obj, "edit", None)
                    if not callable(edit_fn):
                        raise RuntimeError("Provider does not support edit()")
                    if isinstance(backend_obj, GiteeEditBackend):
                        result = await backend_obj.edit(
                            call_prompt, call_images, task_types=final_task_types
                        )
                    else:
                        result = await edit_fn(
                            call_prompt,
                            call_images,
                            size=final_size,
                            resolution=final_res,
                        )
                    if not result:
                        raise RuntimeError("Provider returned empty edit result")
                    logger.info(
                        "[edit] Provider=%s 成功（第%d/%d次尝试），耗时 %.2fs",
                        pid, attempt + 1, max_attempts,
                        time.perf_counter() - t_start,
                    )
                    return result
                except Exception as e:
                    last_error = e
                    last_failed_pid = pid
                    _report_attempt_error(pid, attempt + 1, e)
                    if _INPUT_SENSITIVE_MARK in str(e):
                        sensitive_strikes += 1
                        if sensitive_strikes >= _INPUT_SENSITIVE_STRIKES:
                            logger.info(
                                "[edit] 连续 %d 次命中敏感内容拦截（InputTextSensitiveContentDetected），"
                                "终止剩余重试与兜底链",
                                sensitive_strikes,
                            )
                            raise RuntimeError(
                                f"输入文本疑似包含敏感内容，已被模型服务连续 {sensitive_strikes} 次拒绝，"
                                f"已终止重试与兜底（last provider: {pid}）: {e}"
                            ) from e
                    else:
                        # 「连续」语义：中间夹了其他错误就重新计数
                        sensitive_strikes = 0
                    if attempt + 1 < max_attempts:
                        logger.info(
                            "[edit] Provider=%s 第%d次失败: %s，准备重试...",
                            pid, attempt + 1, e,
                        )
                        await asyncio.sleep(0.5 * (2 ** attempt))
                    else:
                        logger.info(
                            "[edit] Provider=%s 第%d次失败（已达上限%d次）: %s",
                            pid, attempt + 1, max_attempts, e,
                        )

        raise RuntimeError(
            f"Edit failed (last provider: {last_failed_pid}): {last_error}"
        ) from last_error
