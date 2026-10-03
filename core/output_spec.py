from __future__ import annotations

import re

# 常用档位 × 宽高比 → 像素尺寸。用于「按提示词末尾的宽高比兜底覆盖 output」：
# 提供商若不按提示词里的比例出图，就把档位+比例提前换算成明确像素传过去。
RATIO_SIZE_TABLE: dict[str, dict[str, str]] = {
    "1.5k": {
        "1:1": "1536x1536",
        "4:3": "1792x1344",
        "3:4": "1344x1792",
        "16:9": "2048x1152",
        "9:16": "1152x2048",
        "3:2": "1872x1248",
        "2:3": "1248x1872",
        "21:9": "2352x1008",
    },
    "4k": {
        "1:1": "4096x4096",
        "4:3": "4704x3520",
        "3:4": "3520x4704",
        "16:9": "5504x3040",
        "9:16": "3040x5504",
        "3:2": "4992x3328",
        "2:3": "3328x4992",
        "21:9": "6240x2656",
    },
}

# 「宽高比为9:16」「宽高比 9：16」都能认；只认带「宽高比」字样的写法
_RATIO_RE = re.compile(r"宽高比\s*(?:为|是)?\s*(\d{1,4})\s*[:：]\s*(\d{1,4})")


def parse_output(output: str | None) -> tuple[str | None, str | None]:
    """Parse user output into (size, resolution).

    size: "2048x2048"
    resolution: "4K" / "2K" / "1K"
    """
    s = str(output or "").strip()
    if not s:
        return None, None
    if "x" in s.lower():
        return s, None
    return None, s


def extract_prompt_ratio(prompt: str | None) -> str | None:
    """取提示词里最后一个「宽高比为X」的 X，归一化成 "9:16"；没有则返回 None。"""
    matches = _RATIO_RE.findall(str(prompt or ""))
    if not matches:
        return None
    a, b = matches[-1]
    ia, ib = int(a), int(b)
    if ia <= 0 or ib <= 0:
        return None
    return f"{ia}:{ib}"


def resolve_size_from_ratio(prompt: str | None, tier: str | None) -> str | None:
    """档位（"4k"/"1.5k"）+ 提示词末尾的宽高比 → 像素尺寸。

    档位不在表里、提示词里没有可识别的宽高比、或该比例没有对应值时返回 None，
    调用方应保持原尺寸不变，交给提供商自己处理。
    """
    key = str(tier or "").strip().lower().replace(" ", "")
    table = RATIO_SIZE_TABLE.get(key)
    if not table:
        return None
    ratio = extract_prompt_ratio(prompt)
    if not ratio:
        return None
    return table.get(ratio)
