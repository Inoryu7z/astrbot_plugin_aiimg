"""自拍参考图裁剪（ark_seedream 约束）。

独立成模块的原因：这是纯函数，不依赖 AstrBot / aiohttp，
可以在没有运行环境的前提下直接跑断言。
"""

from __future__ import annotations


def trim_persona_refs(images: list, persona_ref_count: int) -> list:
    """ark_seedream 约束：人设参考图只保留第一张。

    入参 images 的顺序约定为 [人设图...] + [衣橱图 / 部位素材 / 用户附图...]，
    persona_ref_count 是其中人设图的张数。裁剪只作用于人设图区间，
    人设图之后的参考图（衣橱图、部位素材、用户附图）原样保留。

    - persona_ref_count ≤ 1、非法值、空列表 → 原样返回
    - persona_ref_count ≥ 总张数 → 只留第一张
    - 其余 → 第一张 + 人设图区间之后的全部

    返回新列表，不修改入参。
    """
    seq = list(images or [])
    try:
        count = int(persona_ref_count)
    except (TypeError, ValueError):
        return seq
    if count <= 1 or not seq:
        return seq
    if count >= len(seq):
        return seq[:1]
    return seq[:1] + seq[count:]
