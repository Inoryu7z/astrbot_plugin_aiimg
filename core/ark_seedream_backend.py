from __future__ import annotations

from .openai_full_url_backend import OpenAIFullURLBackend


class ArkSeedreamBackend(OpenAIFullURLBackend):
    """ByteDance ARK Seedream 系列专用后端。

    行为与 :class:`OpenAIFullURLBackend` 完全一致，唯一区别：
    永远不会在改图请求里注入 ``sequential_image_generation`` 参数。

    原因：Seedream 5.0 pro 不支持 ``sequential_image_generation`` 参数，
    即使值为 ``"disabled"`` 也会被服务端拒绝并返回 HTTP 400：
    ``The parameter `sequential_image_generation` is not supported by the current model``。

    本后端通过覆盖 ``_collect_local_options`` 强制将内部的
    ``__edit_force_single_output`` 置为 ``False``，从而跳过父类在
    ``edit()`` 中添加 ``sequential_image_generation: "disabled"`` 的逻辑。
    其它所有行为（json_image_array 改图模式、image 字段格式探测、
    重试、超时、watermark=False、响应解析等）均与父类一致。
    """

    @staticmethod
    def _collect_local_options(*sources) -> dict:
        opts = OpenAIFullURLBackend._collect_local_options(*sources)
        # 强制不注入 sequential_image_generation（Seedream 5.0 pro 不支持该参数）
        opts["__edit_force_single_output"] = False
        return opts
