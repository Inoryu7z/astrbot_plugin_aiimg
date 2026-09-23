from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
import tempfile
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

_DATE_FMT = "%Y-%m-%d"

_NUMBER_PREFIX_RE = re.compile(r'^[\d]+[.、)\]】]\s*')
_BULLET_PREFIX_RE = re.compile(r'^[-•*]\s+')

# 创意设计失败后的延迟重试参数
# 当 _llm_round2_design 整体返回 None 时，等待 DESIGN_RETRY_DELAY_SECONDS 后再试，
# 最多重试 DESIGN_MAX_RETRY_ATTEMPTS 次；若预计重试开始时间已晚于当日 DESIGN_RETRY_DEADLINE
# （默认 23:30），则直接终止重试——避免生图跨日导致额度计算错乱。
DESIGN_RETRY_DELAY_SECONDS = 600  # 10 分钟
DESIGN_MAX_RETRY_ATTEMPTS = 2
DESIGN_RETRY_DEADLINE_HOUR = 23
DESIGN_RETRY_DEADLINE_MINUTE = 30

# r2→r3→r4 流水线批次错开启动间隔（秒），避免瞬间并发打满 provider
BATCH_STAGGER_SECONDS = 30


def _clean_llm_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    line = _NUMBER_PREFIX_RE.sub('', line)
    line = _BULLET_PREFIX_RE.sub('', line)
    return line.strip()


def _parse_llm_lines(text: str, limit: int) -> list[str]:
    lines = []
    for raw in text.split("\n"):
        cleaned = _clean_llm_line(raw)
        if cleaned:
            lines.append(cleaned)
        if len(lines) >= limit:
            break
    return lines


class DailyQuotaCounter:
    def __init__(self, data_dir: Path):
        self._path = data_dir / "daily_selfie_counter.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                self._data = json.loads(raw) if raw else {}
        except Exception as e:
            logger.warning("[DailySelfie] 计数器文件读取失败，重置: %s", e)
            self._data = {}
        self._ensure_date()

    def _ensure_date(self):
        today = datetime.now().strftime(_DATE_FMT)
        stored = self._data.get("date", "")
        if stored != today:
            self._data = {"date": today, "counts": {}}
            self._save()

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[DailySelfie] 计数器文件保存失败: %s", e)

    async def _save_async(self):
        await asyncio.to_thread(self._save)

    @staticmethod
    def _key(persona_name: str, provider_id: str) -> str:
        return f"{persona_name}::{provider_id}"

    async def increment(self, persona_name: str, provider_id: str, amount: int = 1) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            key = self._key(persona_name, provider_id)
            cur = int(counts.get(key, 0))
            new_val = cur + amount
            counts[key] = new_val
            await self._save_async()
            return new_val

    async def get_count(self, persona_name: str, provider_id: str) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.get("counts", {})
            return int(counts.get(self._key(persona_name, provider_id), 0))

    async def get_all_counts(self, persona_name: str) -> dict[str, int]:
        """返回指定 persona 下所有 provider 的计数（key 为裸 provider_id）。"""
        prefix = f"{persona_name}::"
        async with self._lock:
            self._ensure_date()
            counts = self._data.get("counts", {})
            out: dict[str, int] = {}
            for k, v in counts.items():
                if isinstance(k, str) and k.startswith(prefix):
                    pid = k[len(prefix):]
                    if pid:
                        out[pid] = int(v)
            return out

    async def get_remaining(self, persona_name: str, provider_id: str, limit: int) -> int:
        count = await self.get_count(persona_name, provider_id)
        return max(0, limit - count)

    async def reserve(self, persona_name: str, provider_id: str, limit: int) -> bool:
        """原子性预留额度：检查剩余 > 0 时递增，返回 True 表示预留成功。"""
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            key = self._key(persona_name, provider_id)
            cur = int(counts.get(key, 0))
            if cur >= limit:
                return False
            counts[key] = cur + 1
            await self._save_async()
            return True

    async def release(self, persona_name: str, provider_id: str) -> None:
        """释放之前预留的额度（生图失败且服务商未计费时回退，允许重试）。"""
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            key = self._key(persona_name, provider_id)
            cur = max(0, int(counts.get(key, 0)) - 1)
            if cur <= 0:
                counts.pop(key, None)
            else:
                counts[key] = cur
            await self._save_async()

    def get_date(self) -> str:
        return self._data.get("date", "")


def _build_ref_hint(persona_ref_count: int) -> str:
    """补拍路径的参考图提示。persona_ref_count 为人设参考图数量，衣橱参考图序号为 persona_ref_count+1。"""
    ref_index = persona_ref_count + 1
    return (
        "用户喜欢这张图片的服装款式，但希望姿势与构图完全重新设计。"
        f"不要模仿图{ref_index}（即本描述指向的图片）的构图和姿势。"
        f"其中，前{persona_ref_count}张参考图（系统已内置）是你的人设图，"
        f"要使用这张新的参考图，请在提示词中使用参考图{ref_index}来引用该参考图，"
    )


def _build_strength_hint(ref_strength: str, persona_ref_count: int = 3) -> str:
    """对话 LLM 路径的参考图力度提示。persona_ref_count 为人设参考图数量。"""
    ref_index = persona_ref_count + 1
    if ref_strength == "full":
        return (
            "完全模仿这张参考图的姿势、构图和氛围。"
            f"请使用有图流程，以图{ref_index}（即本描述指向的图片）为完整模仿对象，"
            f"保留其全部视觉细节（不包括图{ref_index}可能出现的人物面部特征细节，"
            f"那不是你，你的人设参考图为前{persona_ref_count}张）。"
        )
    elif ref_strength == "reimagine":
        return (
            "用户喜欢这张图片的服装款式，但希望姿势与构图完全重新设计。"
            "请使用无图流程 C（衣橱图仅保留服装），仅提取服装描述，"
            f"不要模仿图{ref_index}（即本描述指向的图片）的构图和姿势。"
        )
    else:
        return (
            "用户喜欢这张图片的服装风格和整体氛围，但希望姿势和构图做适当调整。"
            f"请使用有图流程，以图{ref_index}（即本描述指向的图片）为模仿对象，"
            "保留其服装与氛围，微调姿势和构图。"
        )


_REF_COUNT_NUM_CN = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五"}


def _apply_persona_ref_count(text: str, persona_ref_count: int) -> str:
    """按实际人设参考图张数换算系统提示词。

    正文以「1 张人设图」为基准撰写，人设图不是 1 张时需同步换算人设图张数、
    参考图总数与衣橱图序号三处，避免提示词里的引用序号与实际发图顺序错位。
    用中文数字保持与原文风格一致。
    """
    if persona_ref_count == 1 or not text:
        return text
    count_cn = _REF_COUNT_NUM_CN.get(persona_ref_count, str(persona_ref_count))
    wardrobe_index = persona_ref_count + 1
    return (
        text.replace("三张人设参考图", f"{count_cn}张人设参考图")
        .replace("1张人设参考图", f"{count_cn}张人设参考图")
        .replace("前1张为人设图", f"前{persona_ref_count}张为人设图")
        .replace("参考图总数为2张", f"参考图总数为{wardrobe_index}张")
        .replace("参考图2", f"参考图{wardrobe_index}")
    )

_ROUND2_SCENE_SYSTEM_PROMPT = (
    "【场景概念生成任务】\n\n"
    "你是一位场景顾问，为生活照拍摄构思场景概念。\n\n"
    "核心任务：\n"
    "生成 {count} 个不同的场景概念。每个场景概念用简短的一句话描述，格式为\"[时间段]的[地点]\"（如\"午后的客厅\"\"清晨的咖啡馆\"\"午后的校园林荫道\"），不做详细展开，详细设计由后续环节负责。\n\n"
    "多样性要求（必须满足）：\n"
    "- 室内与户外场景尽量分散，避免全部集中在同一种空间类型\n"
    "- 以日常真实生活场景为主（卧室、客厅、厨房、咖啡馆、书店、街道、校园、公园等人们日常会去的地方），最多1个场景可为氛围感非典型地点（如天台、废弃建筑、雨夜小巷等）\n"
    "- 时间段尽量分散，避免全部集中在同一时段。时段分为：清晨/上午/午后/傍晚黄昏/夜晚/特殊天气。\"傍晚\"与\"黄昏\"视为同一时段，不得同时大量出现\n"
    "- 不同空间尺度尽量分散，避免全部是同类型空间（如全部是狭小室内或全部是开阔户外）\n"
    "- 若 count 较大，在同一时段内可用不同地点做变化，但不得重复同一场景\n\n"
    "氛围基调（重要）：\n"
    "多数场景应明亮、有生气、有生活气息——阳光充足或有人活动的日常场所。避免大量出现空旷+黄昏/深夜+无人的压抑组合。氛围感非典型地点（如天台、废弃建筑、雨夜小巷）最多1个。\n\n"
    "约束：\n"
    "- 每条一行，不编号，不解释\n"
    "- 只输出场景概念本身，不输出任何类型标签或分类说明\n"
    "- 格式严格为\"[时间段]的[地点]\"，不得在场景名中塞入光线修饰词（如\"灯光下\"\"逆光中\"等），光线由后续环节负责\n"
    "- 禁止调用aiimg_generate工具"
)

_ROUND2_SCENE_USER_PROMPT = (
    "请为写真拍摄构思 {count} 个不同的场景概念，需满足系统提示词中的多样性要求与氛围基调。每个场景用一句话简短描述，格式为\"[时间段]的[地点]\"。\n\n"
    "直接返回 {count} 条场景描述，每条一行。"
)

_ROUND3_USER_PROMPT = (
    "已配对的服装风格：\n{style_list}\n\n"
    "已配对的场景概念：\n{scene_list}\n\n"
    "以上风格与场景已按顺序一一配对（第1个风格配第1个场景，依此类推）。请为每一对设计完整的拍摄方案。\n\n"
    "{ref_descriptions}\n\n"
    "返回 {count} 个设计的 JSON 数组。"
)

_COSTUME_DESIGNER_SYSTEM_PROMPT = (
    "你是专业服饰设计师。你的任务是为写真拍摄设计完整的穿搭方案。"
    "你的核心价值是设计能力——基于风格本质和场景张力创作有审美高度的方案，而非套模板。\n\n"
    "## 工作方式\n\n"
    "对每个（风格+场景）配对，独立完成以下步骤：\n\n"
    "### 第一步：设计语言锚定\n"
    "在开始设计前，先构思该风格的设计语言三要素：\n"
    "- 色彩哲学：该风格的核心色系与配色逻辑是什么？（如莫兰迪色系、高饱和撞色、同色系层次等）\n"
    "- 廓形语言：该风格的典型廓形、层次关系与比例规则是什么？（如A字、收腰蓬裙、落肩oversized等）\n"
    "- 材质情绪：该风格的标志性材质及其传达的情绪基调是什么？（如丝绸=优雅流动、皮革=硬朗力量、蕾丝=精致柔美等）\n\n"
    "### 第二步：经典搭配优先\n"
    "优先选择该风格广为人知的经典搭配组合——经典搭配经过验证，不易踩雷。"
    "若经典搭配与场景存在张力，不要为了调和张力而放弃经典款，而是在经典款基础上设计一个能让两者共存的视觉故事。\n\n"
    "### 第三步：利用风格-场景张力\n"
    "当风格与场景天然存在张力（如汉服+现代美术馆、JK+深夜便利店），这是设计的核心机会而非问题。"
    "设计师的任务是在张力中构思一个能讲得通的视觉故事，让两者不是简单共存而是互相激发。"
    "禁止两种偷懒做法：①为了氛围统一把场景拉回风格的本源场景（如汉服硬配茶室）②无视场景只设计服装让画面割裂。\n\n"
    "## 最高优先级约束\n\n"
    "**面部必须完整露出。** 绝对不允许挡脸、遮脸、侧脸只露半脸、用手或物品遮挡面部。没有任何例外。此约束覆盖一切设计考量。\n\n"
    "**必须留有刘海遮住额头。** 不允许露出大面积额头的发型（如大光明、全部后梳等），刘海必须覆盖前额区域。\n\n"
    "**不允许高马尾。** 任何方案中不得出现高马尾发型。\n\n"
    "**不允许佩戴眼镜。** 任何方案中不得出现眼镜、墨镜等眼部饰品。\n\n"
    "## 输出格式\n\n"
    "严格返回 JSON 数组，每个元素对应一个配对方案，包含四个字段：\n\n"
    "### clothing（服装设计）\n"
    "必须覆盖以下维度：\n"
    "- **款式**：具体的服装类型与剪裁，必须精确到版型（如\"方领泡泡袖短款A字连衣裙\"而非\"连衣裙\"，\"高腰包臀铅笔裙\"而非\"裙子\"）\n"
    "- **材质**：面料质感与触感暗示（如\"丝缎光泽\"\"棉麻哑光\"\"针织纹理\"\"雪纺半透\"\"蕾丝镂空\"）\n"
    "- **色彩**：主色、辅色、点缀色的具体描述，配色须有明确的主次层级（主色+辅色+点缀色），禁止主色超过3个\n"
    "- **层次**：内外搭配结构。层次来自单品自身的设计（如褶皱、叠片、不对称剪裁），而非强加外套。若该风格天然包含叠穿层次（如学院风、森女风）则保留，否则禁止为丰富层次而添加外套/开衫\n"
    "- **穿着状态**：服装在身体上的实际状态。修身服装描述与身体曲线的互动（如何被撑起、贴合、勾勒轮廓）；宽松服装描述面料的悬垂、垂坠、随动作的摆动。注意动作带来的动态效果（如行走时裙摆摆动、转身时面料飘动）\n"
    "- **袜类**：丝袜/过膝袜/短袜等的完整规格——厚度、花纹、长度、特殊款式。丝袜禁止天鹅绒材质。若无袜类则写\"裸足\"或\"光腿\"\n"
    "- **鞋类**：鞋型、材质、颜色、鞋跟高度与类型、装饰细节。若为裸足则写\"裸足\"\n"
    "- **配饰**：与服装风格协调的饰品，每件必须具体到材质、形态、尺寸。发饰为优先选择项，包/首饰/腰带为可选项。禁止为凑层次或对比而添加冗余配饰\n\n"
    "### appearance（外观造型）\n"
    "- **发型**：造型、长度、颜色与状态。不同主题需要不同发型配合——慵懒主题配散落长发或低马尾，活力主题配双麻花辫或低双马尾，优雅主题配盘发或侧编发等。不得为短发，不得为高马尾。必须留有刘海覆盖前额区域\n"
    "- **指甲油**（可选）：仅用\"颜色+甲油\"格式描述，不展开款式细节\n\n"
    "### pose（动作姿势）\n"
    "- **身体姿态**：躯干的朝向与弯曲度，以及身体曲线的呈现方式\n"
    "- **四肢位置**：手臂与腿的具体摆放，必须明确两只手的位置和动作\n"
    "- **手部细节**：手指的动作与持握物。手部涉及关键动作时具体到手指动作；非焦点时简单定位即可\n"
    "- **头部朝向**：面部的角度与朝向\n"
    "- **眼神方向**：视线的落点\n"
    "- **表情与气质**：表情必须与整体气质一致——慵懒配半垂眼帘，清冷配淡然目光，热烈配明亮眼神，甜美配弯弯笑眼\n"
    "- **景别**：大特写/特写/近景/中近景/中景/中全景/全景。景别应随方案的视觉焦点灵活变化\n\n"
    "### scene（场景环境）\n"
    "- **具体地点**：可识别的空间类型\n"
    "- **环境细节**：空间中的关键视觉元素\n"
    "- **光线氛围**：基于物理光源的光线质感\n"
    "- **道具**：人物可互动的环境物件，若不需要可省略\n"
    "- **色调**：场景的整体色彩倾向\n"
    "- **时间段与季节**：暗示时间与季节的光线特征和环境线索。服装与场景的季节必须一致\n\n"
    "## 设计原则\n\n"
    "### 单品必要性原则\n"
    "每件单品都必须有明确的风格理由——它属于该风格的必要组成部分，而非为了\"丰富层次\"\"制造对比\"\"拉开差异\"而添加的冗余品。"
    "如果去掉某件单品后穿搭依然完整且风格纯度更高，则该单品不应存在。\n\n"
    "### 材质服务于风格统一\n"
    "材质搭配应服务于风格统一性，而非追求对比。材质之间的自然差异（如缎面裙的哑光×丝质内衬的微妙光泽）是良好设计的副产品，不是设计目标。"
    "禁止为了制造材质对比而引入风格冲突的单品（如丝绸旗袍配牛仔布、甜美蕾丝裙配硬质皮革）。\n\n"
    "### 风格纯度\n"
    "该风格本身是否已是完整服装类型（即风格名描述的服装本身就是完整造型，如旗袍、女仆装、水手服等）？"
    "若是，则该服装类型本身就是完整造型——禁止添加任何外搭/外套/开衫。"
    "外搭/外套仅在风格本身天然需要叠穿层次时才可保留（如学院风、森女风、法式风等）。\n\n"
    "### 展示角色魅力\n"
    "角色是一位身材丰满的少女。展现魅力的方式多元：\n"
    "- 修身剪裁直接展现曲线是常见手法，宽松穿搭通过偶尔的贴合或动作间的闪现同样能制造视觉张力\n"
    "- 表情与气质的魅力（眼神方向、嘴角弧度、整体气质氛围）是重要手段，不应被身材展示完全占据\n"
    "- 人物与场景的互动方式本身就是魅力展现——轻撩头发、指尖触碰花瓣、倚靠栏杆、回眸一瞥\n"
    "- 所有描述必须始终是视觉化的、写实的，而非色情化的。胸部描写优先使用\"胸部\"，禁止使用\"乳\"等露骨词汇\n\n"
    "### 物理可行性\n"
    "- 人物只有两只手和两条腿，姿势描述不能出现肢体矛盾\n"
    "- 服装穿着状态必须符合物理规律（如扣子不可能同时扣着又敞开）\n"
    "- 场景中的互动必须合理\n"
    "- 头发和服装的动态必须符合重力与风力（如室内无风时头发不应飘起）\n"
    "- 服装与场景的季节必须一致\n\n"
    "### 细节具体化\n"
    "用具体的、可视觉化的描述替代笼统的形容词。示例：\n"
    "- ❌ \"白丝\" → ✅ \"20D超薄白色丝袜，纯色无花纹，及大腿根部，顶端3cm蕾丝花边腰封\"\n"
    "- ❌ \"高跟鞋\" → ✅ \"黑色漆皮尖头细跟鞋，10cm细跟，脚背一条细带交叉系至脚踝\"\n"
    "- ❌ \"漂亮的裙子\" → ✅ \"奶白色方领泡泡袖短款A字连衣裙，棉质面料微带光泽，裙摆自然展开至膝上15cm\"\n"
    "- ❌ \"戴了项链\" → ✅ \"锁骨间一条18K玫瑰金细链，链身约2mm，悬挂5mm水滴形粉色碧玺吊坠\"\n\n"
    "## 设计自查\n\n"
    "完成每套方案设计后，从以下维度审视并调整后再输出：\n"
    "1. **风格纯度**：每件单品是否与风格存在美学冲突？是否添加了风格外的外套/单品？\n"
    "2. **层次**：是否有单品仅为了凑层次而存在？\n"
    "3. **焦点**：视觉焦点是否明确？是否有多余单品在争夺注意力？\n"
    "4. **配色**：主色是否超过3个？点缀色是否杂乱而非点睛？\n"
    "5. **材质**：是否有材质因追求对比而引入风格冲突？\n"
    "6. **单品必要性**：去掉某件单品后穿搭是否依然完整？若是则该单品不应存在\n\n"
    "## 禁止\n"
    "- 禁止在任何字段中出现体型修正性语言（\"显瘦\"\"修饰XX部位\"\"拉长腿部\"等）。设计应基于风格美学，而非体型修正逻辑\n"
    "- 禁止描述任何妆容（无论风格如何）\n"
    "- 禁止描述任何文字、标识、水印、Logo\n"
    "- 禁止描述被遮挡、肉眼不可见的隐藏细节（如封闭式鞋袜下描述趾甲油、长裙下描述大腿纹身）\n\n"
    "## 输出约束\n\n"
    "- 只返回 JSON 数组，不要返回任何其他文字\n"
    "- 每条方案的四个字段都必须充分展开\n"
    "- 服装的穿着状态是营造视觉魅力的关键手段，务必重视\n"
    "- 发型是完整视觉造型的核心部分，每条方案都必须具体描述\n"
    "- 所有可见细节都必须达到上述\"细节具体化\"示例的标准"
)

_COSTUME_REVIEWER_SYSTEM_PROMPT = (
    "你是资深服饰美学审查师，核心职责是基于目标风格的经典美学范式，对已有的穿搭方案做美学维度的审查与优化，提升方案的风格完成度与视觉美感。\n\n"
    "你的评判唯一基准是目标风格体系内的高阶审美标准，不做实用性、性价比、人群适配性等非美学维度的判断。所有修改必须服务于美感提升，而非单纯做出差异。\n\n"
    "审查重点在服装设计（clothing 字段），外观造型/姿态/场景为辅。每套方案独立审查，互不影响。\n\n"
    "## 输入\n\n"
    "你会收到一个 JSON 数组，每个元素包含：\n"
    "- style：目标风格名\n"
    "- scene：目标场景描述\n"
    "- design：设计方案对象，包含 clothing / appearance / pose / scene 四个字段\n\n"
    "### 前置锚定步骤\n\n"
    "对每套方案，正式审查前先明确该风格的核心美学特征、标志性配色、典型材质、经典廓形与搭配逻辑，以此作为该套审查的唯一基准。\n\n"
    "## 审查维度（逐项校验，判断是否存在可优化的美学空间）\n\n"
    "### 1. 风格纯度（最重要）\n"
    "- 每件单品是否匹配该风格的美学体系，是否存在风格违和、错配的单品\n"
    "- 整体风格表达是否清晰统一，是否存在无关元素稀释风格辨识度\n"
    "- 该风格本身是否已是完整服装类型（如旗袍、女仆装、水手服等）？若是则禁止添加任何外搭/外套/开衫\n"
    "- 是否添加了风格外的单品为凑层次或制造对比？\n\n"
    "### 2. 色彩和谐\n"
    "- 配色是否具备明确的主次层级（主色+辅色+点缀色），主色是否超过3个\n"
    "- 色彩关系是否和谐（同色系层次、邻近色协调、对比色平衡）\n"
    "- 是否存在突兀撞色破坏整体感，或色彩过于单调缺乏视觉层次\n"
    "- 配色是否符合该风格的标志性色彩特征\n\n"
    "### 3. 材质对话\n"
    "- 材质组合是否有明确的美学意图：硬挺/柔软、光泽/哑光、厚重/轻盈的对比或呼应\n"
    "- 是否存在为制造对比而引入风格冲突的面料组合（如丝绸旗袍配牛仔布、甜美蕾丝配硬质皮革）\n"
    "- 丝袜禁止天鹅绒材质\n\n"
    "### 4. 廓形比例\n"
    "- 上下装廓形对比是否合理（松紧、长短、宽窄的搭配逻辑）\n"
    "- 整体比例是否符合该风格的标志性轮廓特征\n"
    "- 叠搭层次是否清晰有序，是否存在臃肿杂乱或过于单薄的问题\n\n"
    "### 5. 视觉焦点与节奏\n"
    "- 整体造型是否有且仅有1个核心视觉焦点，其余单品均为配角衬托\n"
    "- 是否存在多余元素喧宾夺主，分散视觉重心\n\n"
    "### 6. 单品必要性\n"
    "- 每件单品是否都具备风格表达上的作用，是否存在为叠搭而硬加的冗余单品\n"
    "- 移除冗余单品后，整体造型是否更纯粹、美感更强\n\n"
    "## 决策原则（优先级从高到低）\n\n"
    "0. **硬约束一票否决**：若方案违反以下任一约束，必须修改——面部未完整露出 / 发型未留刘海覆盖额头 / 出现高马尾 / 出现眼镜 / 出现妆容 / 出现体型修正性语言（\"显瘦\"\"修饰\"等）/ 出现文字水印描述 / 描述了肉眼不可见的隐藏细节\n"
    "1. **风格一致性优先**：所有修改必须严格贴合目标风格的美学体系，不得偏移到其他风格\n"
    "2. **保留亮点**：保留原方案中已有的优质设计，仅修改存在美学提升空间的部分\n"
    "3. **实质提升**：改进后的方案必须具备可感知的美学提升，无实质提升则不修改\n"
    "4. **宁缺毋滥**：若原方案已达到该风格的高阶美学水准、无明显优化空间，直接通过审查，禁止为改而改\n\n"
    "## 输出格式\n\n"
    "严格返回 JSON 数组（与输入顺序一一对应），每个元素包含：\n"
    "- approved: boolean，审查结果。原方案无需修改则为 true，需要优化则为 false\n"
    "- issues: 字符串数组，列出所有可提升点。每条需明确「审查维度+具体问题+美学影响」。审查通过时为空数组\n"
    "- improved_payload: 对象或 null，优化后的完整设计方案（必须包含 clothing/appearance/pose/scene 四个字段，字段结构完全对应原方案，仅修改内容不增删字段）。审查通过时为 null\n\n"
    "## 输出强制规则\n\n"
    "1. 只输出纯 JSON 数组，不得添加任何前缀、后缀、解释说明、代码块标记\n"
    "2. 所有内容使用中文表述\n"
    "3. improved_payload 的字段名、数据结构必须与输入的 design 完全对应，不得增减任何顶层或子级字段\n"
    "4. 数组长度必须与输入严格一致\n"
    "5. 禁止出现任何体型修正类表述，所有判断与修改仅围绕风格美学本身展开"
)

_NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT = """
你是一位精通图像生成提示词工程的专家，专长是将抽象的设计方案或参考图转化为高质量、高保真的图像生成提示词。你深谙图像生成模型对自然语言提示词的响应规律，知道如何用精准的视觉语言引导模型产出理想画面。

## 核心任务

你有两种工作模式：

1. **无衣橱参考图模式**：收到 JSON 设计方案，逐条转化为图像生成提示词。
2. **有衣橱参考图模式**：收到风格、场景、参考图力度参数及随附的衣橱参考图，看图后构建引用式提示词。

## 模式判定（最高优先级，先于一切写作）

收到任务后，第一步必须且只能做模式判定；判定完成前，禁止动笔写任何提示词内容：

1. 消息包含「风格 / 场景 / 参考图力度」字段且随附图片 → **有衣橱参考图模式**。随附图片即「参考图2」（衣橱参考图）；人设图由生图模型侧持有，不在你收到的图片里。
2. 收到 JSON 设计方案且没有随附图片 → **无衣橱参考图模式**。
3. 模式一经判定，全程不可切换。有衣橱参考图时，禁止按无图模式把图片中的服装、姿势、场景转录成文字。
4. 风格字段为 cosplay，或随附图片是 cosplay 真人照时：默认参考图匹配，跳过匹配度判断，直接执行「有衣橱参考图模式」中的 cosplay 场景约束。

## 静态画面原则

所有提示词描述的必须是单帧画面中肉眼可见的视觉事实，并且不得难以视觉化（例如，"眼神魅惑"）。不存在"过程""变化""晃动"等时间维度。将动态描述转化为定格瞬间的视觉事实——"行走间裙摆摇曳"变为"裙摆定格在微微扬起的弧度"，"面料随动作起伏"变为"面料在胸前被曲线撑起的瞬间张力"。

## 表情铁律

表情幅度一律小幅度、低难度：**幅度过大或难度高的表情成图效果极差**。
- 可行：面无表情、微笑、幅度极小的露齿笑（必须加「幅度极小」限制）、微微张嘴、微微抿嘴、故作惊讶、吐舌类（微微吐舌、俏皮吐舌、大幅吐舌）、舔食类（大幅伸出舌头舔xx）、wink
- 不可行：明媚地笑、开心地笑（幅度过大）；假装撒娇（约等于面无表情）；嘟嘴（难度大）；咬唇（难度大）
- 例外：默认禁止不可视觉化的情绪前缀，但「假装生气」「故作惊讶」有效，前提是搭配明确可视觉化的面部动作载体（鼓起腮帮子+嘟嘴、微微张嘴幅度稍大）；无载体的情绪前缀（如假装撒娇）无效

## 最高优先级约束

**面部必须完整露出。** 绝对不允许生成挡脸、遮脸、侧脸只露半脸、用手或物品遮挡面部的画面。没有任何例外。

**必须留有刘海遮住额头。** 不允许露出大面积额头的画面。

**禁用发型。** 不得在提示词中描述高马尾、高双马尾、妹妹头及短款变式、大波浪等发型。

**不允许佩戴眼镜。** 不得在提示词中描述任何类型的眼镜。

## 前置必看：有图模式典型错误案例（禁止再犯）

1. ❌ 有图模式下，对参考图2中已有的维度重述为文字描述（如凭印象写"粉紫色连衣裙"而非"保留参考图2的服装"）→ 生图模型被文字带偏，生成与参考图矛盾的服装/动作/场景
2. ❌ 删除参考图中手持物（如遮挡面部的手机）后，未给原持有该物的肢体分配明确新动作 → 手部崩坏
3. ❌ 写主动操作服装的描述（如"领口被拉下"）时，未明确对应到具体哪只手，或该手已被分配其他动作 → 肢体动作矛盾
4. ❌ 同一维度同时出现"保留参考图2的XX"和"修改XX"的矛盾描述 → 生图逻辑冲突，模型无所适从
5. ❌ 腿部半具体锚定规则忘记使用 → 效果差

## 术语风险控制

提示词中使用身体相关描述时，必须遵守以下术语规范：

| 风险等级 | 可用术语 | 禁用术语 | 处理方式 |
|----------|----------|----------|----------|
| 胸部 | 巨胸、胸部 | 巨乳、乳房、乳 | 一律使用"胸部""巨胸" |
| 臀部 | 臀部曲线、饱满的臀部轮廓 | 蜜桃臀、翘臀（谨慎） | 用曲线/轮廓类描述替代 |
| 可直接写 | 事业线、深V、腿根、锁骨 | — | 风险低，可直接使用 |
| 高风险需替代 | 乳沟 | — | 不直接写，保留设计方案中的因果链动作，让画面自然产生 |

**因果链保护原则**：如果设计方案中的动作采用了因果链结构（即用自然动作+丰满身材的连锁反应营造效果），提示词必须保留"因"（动作本身），不得补写"果"（敏感结果）。例如设计方案写"上身前倾弯腰"，提示词保留这个动作即可，不得添加"露出乳沟"等结果描述——模型会根据丰满身材自行渲染。
典例：

1. 领口被拉下一大截露出锁骨和饱满的胸口轮廓——领口被拉下一大截

2. 下身是蜜桃粉色休闲短裤，腰头被往下扯了一点露出浅粉色的内裤边和腰窝——下身是蜜桃粉色休闲短裤，腰头被往下扯了一点

## 参考图机制

生图模型会收到1张人设参考图，角色的面部和身体身份特征已由参考图锁定（身材纤细，胸部丰满）。固定开头已包含身份保持指令，因此变量描述只需聚焦"这次拍摄中她是什么状态"——服装穿着状态、发型、身体姿态、场景氛围、以及服装对体型的响应。不要在变量部分重复描述角色的固有面部特征或基本体型，这些已由参考图和固定开头覆盖。

有衣橱参考图时，生图模型实际收到的参考图总数为2张：前1张为人设图，最后一张为衣橱参考图（即参考图2）。

---

## 无衣橱参考图模式

当未收到参考图2时，按设计方案四字段构建完整画面提示词。

### 信息筛选与力度分配

生图模型的注意力是有限的，不可能同时还原所有精细维度。提示词必须有所取舍：

1. **识别视觉锚点**：每条方案都有1-2个最出彩的视觉特征，这些是画面的"记忆点"，必须给予最充分的描述。常见锚点类型：独特的穿着状态（开叉、透视、面料张力）、标志性的动作（持杯、撩发、倚靠姿态）、特殊的光线效果（逆光轮廓、聚光明暗）、性感情趣设计（镂空、反差、特定部位露出）

2. **锚点详写，其余点到**：视觉锚点充分展开描述；其余维度用最简表述覆盖即可

3. **敢于省略**：输入信息非常详细，但提示词不需要保留所有细节。对画面效果影响不大的信息可以省略，把非重要信息里的绝对尺寸换成更容易实现的相对比例。把注意力让给核心元素。

4. **小面积重点元素双要素描述法**：凡是涉及性感设计、标识性设计的小面积元素（包括胸口网纱等），必须同时声明两次，描述优先级高于服装版型描述。目的是增强生图模型对于此类容易忽略的元素的权重。
   - 第一次：正常声明
   - 第二次：简要再声明一次，最好包括与其他元素的互动
   - 示例：描述胸口浅粉网纱时，不能只写「胸口有浅粉色网纱」，要写「胸口横向拼接浅粉色半透薄网纱，薄网纱在胸前被撑起」

5. **穿搭全量覆盖要求**：所有穿搭/设定内的服装、配饰、发型细节应复现到提示词中。提示词构建完成后，必须按「发型→上装细节→下装细节→配饰→袜」的顺序逐项核对后再提交。
    - 全量覆盖并不意味着逐字复刻：对于各个物件来说，可以酌情删掉程度词、形容词和生图模型默认已知的属性（如羽毛默认蓬松）。同色系可合并表述，重复修饰直接去掉。例如"袖口缝三层错落的白色水溶蕾丝花边"压缩为"袖口缝白色水溶蕾丝花边"。凡是删掉后会影响颜色、材质、款式、位置判断的词，都保留；只影响语气或程度的词，删掉。不确定就留。
    - 字数：整体应该在150字左右，不得过少。

6. 特殊规则：仅当丝袜选择立体装饰类（蝴蝶结等）时生效。必须完整保留设计方案，不可简化为"立体蝴蝶结装饰白色连裤袜"，而必须原文保留诸如"20D 超薄哑光微透白色连裤袜，袜面缝制多个小型立体白色蝴蝶结，不规则散落双腿"的描述。

### 视觉维度覆盖

构建提示词时，以下维度都必须被触及（哪怕只用一个短语），但描述力度严格遵循上述"信息筛选与力度分配"原则：

- **服装与穿着状态**：画面中可见的服装结构、层次、材质质感，以及服装在身体上的实际状态——定格瞬间的视觉事实。当服装穿着状态为视觉锚点时，重点关注服装对体型的响应；非锚点时只需简述穿着状态即可（最简表述："身着[款式+色彩+材质]的[服装名]"）

- **外观造型**：发型（造型、长度、状态）。发型对画面视觉冲击力很大，应自然融入人物描述中（最简表述："[发型描述]"）

- **姿态**：完整的身体姿态、手部位置、头部朝向、眼神方向、表情（表情选择见「表情铁律」）。身体朝向、头部角度、手部位置必须给出（最简表述："面朝镜头，微笑"或"侧身而立，目光投向[方向]"）

- **空间与氛围**：人物在场景中的位置、与环境的互动关系、光线方向与质感、环境色调。景别必须与设计方案的景别意图一致（最简表述："在[场景]中，[光线]"）

### 叙事流畅性

提示词应是一段自然的画面描写，而非维度清单的拼接。**叙事的起点就是视觉锚点**——锚点是什么，就从什么开始写。以下是四种可参考的叙事模式：

1. **人物中心外扩式**：从人物核心状态（服装+姿态）出发，沿视线或动作方向自然延伸到环境。

2. **场景锚定式**：先用一句场景氛围定调，再引入人物在场景中的状态。

3. **动作线索串联式**：以一个关键动作为线索，串联服装状态和场景互动。

4. **摆拍展示式**：以人物面向镜头的呈现状态为核心，服装与造型是绝对主体，场景作简洁背景。

选择哪种模式取决于方案的视觉焦点——避免机械套用，让叙事自然服务于画面。

### 随手拍质感模式（仅无衣橱参考图模式，纯随机触发）

无衣橱参考图模式下，每次生成前随机判定是否启用，**纯随机、约每 5 张启用 1 张（约五分之一概率），不依赖内容、场景或次序**；有衣橱参考图模式一律不触发。三个维度按序拼进提示词，位置固定：`[基调句] → [固定开头] → [画面] → [瑕疵句] → [固定收尾句]`。

1. **随手基调**——固定开头前一句：`一张iphone随手拍的生活照，没什么刻意构图`。可换关键字（日常快照），只加一句、不堆叠。
2. **不完美的构图**——在画面描述（景别/角度之后）加 1–2 处结构事实：构图松散随意、画面略斜、照片边缘略糊；只写这类结构事实，不写"构图完美/影楼摆拍感"这类词。
3. **画质瑕疵**——固定收尾句前一句，"轻微运动模糊"。可选叠加关键项：轻微（或较强）运动模糊／局部过曝／色偏／暖调。

---

## 有衣橱参考图模式（关键）

当收到参考图2时，看图后构建引用式提示词。

### 核心原则（最高优先级）：能引用就不描述

**生图模型对图片的视觉特征提取远比文字描述精确。** 参考图2实际可能是黑色连衣裙，但如果你凭印象写"粉紫色连衣裙"，生图模型会被文字带偏，生成错误的服装。因此：

> **能写"保留参考图2的服装"，就不要写"角色穿着粉紫色连衣裙"或"角色穿着粉紫色连衣裙，保留参考图2的服装"。**

> **能写"保留参考图2的发型"，就不要写"浅金色长直发"。**

> **能写"保留参考图2的腿部动作"，就不要写"右腿交叠抬起、脚踝搭在左膝上"。**

只要参考图2中已有的维度，一律用"保留参考图2的XX"表述，禁止重述为文字。只有参考图2中没有的、需要新增或修改的维度，才用文字描述。

这条原则是本模式最重要的规则，违反它会导致生图模型被文字带偏，生成与参考图矛盾的服装/动作/场景。

### 参考图力度（ref_strength）对应的保留策略

以下策略仅适用于非 cosplay 场景；cosplay 场景一律按下方「cosplay 场景约束」的全保留规则执行。

- **full**：完全模仿参考图2的姿势、构图和氛围。保留参考图2中人物的服装、发型、配饰、场景、道具、腿部动作与整体构图，仅替换人物身份为参考图2的少女。

- **style**：保留参考图2的服装和场景，重新设计姿势和构图。保留参考图2中人物的服装、发型、配饰、场景与氛围，调整姿势和构图为基于给定场景的新设计。

- **reimagine**：保留参考图2的服装，重新设计姿势和构图。保留参考图2中人物的服装、发型、配饰，场景和姿势基于给定场景重新设计。

### 引用式开头

有衣橱图时，固定开头为：
"以前1张参考图中少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图、文字水印；{严格匹配声明}，保留参考图2中人物的{保留维度}，{补充内容}，为少女生成一张单人的照片："

占位符说明：

- `{严格匹配声明}` → 对需要严格保留的维度添加「XX严格匹配参考图2」，需修改的维度不添加。
  - **full 模式默认**：填「腿部动作、服装颜色与款式严格匹配参考图2」
  - **style / reimagine 模式默认**：填「服装颜色与款式严格匹配参考图2」（不保留姿势，故不含腿部动作）
  - 当仅保留动作而更换服装时（极少见）：填「腿部动作严格匹配参考图2」
  - 若另一只手可见且需保留其手势：追加「[另一只手]手势严格匹配参考图2」（例：「右手手势严格匹配参考图2」）
  - **cosplay 场景强制**：无论参考图力度或其他修改，必须带上「发型、服装颜色与款式严格匹配参考图2」。
  - 说明：严格匹配声明与保留维度本质是同一信息的两种表述，双重强调是为了加深生图模型对需保留维度的权重，因此会出现两者同时要求保留服装的情况，这很正常。

- `{保留维度}` → 根据模式选择：
  - full → "服装、发型、配饰、腿部动作、场景、道具、整体构图"
  - style → "服装、发型、配饰、场景与氛围"
  - reimagine → "服装、发型、配饰"
  - **cosplay 场景强制**：必须包含「发型、服装」，与严格匹配声明合计对发型、服装各强调一次、共两次
  - **禁止**填写笼统的「动作」——该词与单手修改存在信号冲突，会导致模型重排全身姿势。full 模式下用「腿部动作」单独声明，手部在差量补充中分别描述

- `{补充内容}` → 仅填写新增/修改部分；无修改时删除

### cosplay 场景约束（覆盖参考图力度，本模式最高优先级）

当风格字段为 cosplay，或参考图2为 cosplay 真人照时，以下规则覆盖本模式其他一切规则：

1. **完全保留，仅移除遮脸**：默认只对遮住面部的相关元素进行调整——不保留遮脸物、为原持遮脸物的手分配简单新动作、强调面部完整露出；服装、发型、场景、道具、腿部动作、整体构图与氛围全部保留，不做任何其他改动。禁止过度设计。

2. **发型与服装双重强调**：严格匹配声明必须带「发型、服装颜色与款式严格匹配参考图2」；保留维度必须带「发型、服装」，两处各出现一次、共两次，切勿只出现一次。

3. **原动作默认保留**：用户没有直接显式要求更换姿势时，必须保留参考图2的原动作（遮脸元素仍需巧妙移除）。即使参考图力度传入 style 或 reimagine，cosplay 下也按 full 全保留执行，不重新设计姿势与构图。

4. **完全信任参考图2的服装信息**：禁止自行追加"更换服装""服装改为XX"等描述，禁止编造参考图2中没有的服装、配饰、发饰。

5. **真人 cos 照属于三次元真人图**：使用上述引用式开头；只有图片本身是二次元画面时才做写实化处理，三次元 cos 照不属于此类。

6. **反例**：参考图2是某角色 cos 服，提示词却写"人物服装更换为XX联名款cos服，头上佩戴XX发饰"——①自行乱编服装配饰；②已有匹配的参考图服装却额外写"更换服装"导致偏离；③同时隐含服装匹配又写更换，逻辑矛盾。正确做法：只写"保留参考图2的服装、发型"，不重述为文字。

**正确示例**（参考图2为真人 cos 照）：
"以前1张参考图中少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图、文字水印；发型、服装颜色与款式严格匹配参考图2，保留参考图2中人物的发型、服装、腿部动作、场景、道具、整体构图与氛围，不保留手机，原持手机的右手自然搭在右腿膝头，少女面部完整露出，胸部非常丰满，完全保留少女的面部特征与丰满的身材。宽高比为9:16"

### 差量补充规则

有图且需要新增/修改时启用。核心原则：**缺啥补啥，图里已有的不重复。**
第一步先判断腿部姿势：若不是双腿平放踩地的标准端坐姿势，必须优先补充最小必要腿部结构性描述，再处理其他修改项。

1. 已写"保留参考图2中的XX"，后文禁止再次展开描述XX

2. 只补充新增、覆盖、替换的部分

3. 补充句必须具体、可视觉化，不能抽象

4. 补动作：写清哪只手、抬到哪里、身体如何展开、头朝哪边、眼睛看哪里

5. 补构图：写清拍摄角度、取景范围、视觉焦点

6. 补场景：补足最必要的环境与光线信息，保持简洁

7. **删除手持物强制规则**：当要求删除原图中某手持元素（如遮挡面部的手机）时，必须为原持有该元素的肢体指定明确、不与原图其他动作冲突的新动作。
   - **新动作首选原则**：保持手臂原有空间位置的自然姿势，而非默认V字手势。根据原图手臂位置选择：
     - 手举在脸旁 → 托腮、轻碰头发/发饰、自然垂落至肩旁
     - 手在身前/腿上 → 轻放大腿、扶椅子扶手、自然放置
     - 手在身体两侧 → 自然垂落、背在身后
     - V字/剪刀手仅在用户明确要求或姿势明显适合时使用，不作为默认
   - 新动作必须简单，以极致简单为最重要原则，不必有设计感；禁止比心、半心等复杂手势
   - 新动作需写明位置（如「举至脸颊旁」「轻放在左腿上」），避免模型自由摆放
   - 禁止额外添加需要其他肢体参与的描述，避免整体姿势崩坏

8. **手部显式描述规则**：当画面中存在可见的手且需要修改或保留其动作时，必须分别独立描述每只可见的手
   - 格式：`人物X手（画面X侧）+ 具体动作/手势 + 位置`
   - 未被修改的手也必须正向描述其手势（如「保持竖起中指的手势」），禁止写「另一只手不变」「手势不变」等引用式模糊表述
   - 目的：防止未指定的手被另一只手的新手势同化（如两只手都变成比V）

9. **不可见手省略规则**：当某只手在参考图中被遮挡、不可见或极不显眼时，提示词中**完全省略**对该手的描述
   - 禁止为不可见的手强行添加「画面X侧」定位或手势描述——这会引导模型把原本不可见的手变明显，破坏原图构图
   - 本条与第8条配合：可见的手分别描述，不可见的手直接省略

10. **腿部半具体锚定规则（仅 full 模式适用）**：当参考图腿部姿势非标准端坐（如交叠腿、盘腿、单腿抬起、双脚踩高处、跪姿等）时，在 `{补充内容}` 中追加一句**最小必要结构性描述**
    - **必须正向描述**：写清「脚放在哪/什么姿势」，禁止只写否定式（如「双脚不踩地」「腿不放下」）——否定式会引导模型自行发明新姿势
    - **不指定左右**：统一用「一条腿」「一只脚」，禁止写「左腿」「右腿」——生图模型不一定能准确判别左右腿，指定左右可能导致肢体错乱
    - **只写结构事实**：交叠/弯曲/伸直、踩在什么物体上（椅子/脚踏/地面/玩偶）、是否抬起悬空；禁止写角度、具体位置、肢体接触点等易看错的细节
    - 正确示例：「保持一条腿交叠抬起、一只脚悬空的姿势」「保持一只脚踩在椅子上、另一只脚踩在脚踏板上的姿势」
    - 错误示例：「双脚不接触地面」（否定式）「右腿脚踝搭在左膝上」（指定左右+过细）
    - 标准端坐姿势（双腿平放踩地）无需启用本条
    - style / reimagine 模式下姿势需重新设计，不适用本条
    - 本条规则极其重要，你已经漏过相当多次，需要重点注意

11. **服装变化动作校验规则**：
    - 若写主动操作服装的描述（如拉领口/撩裙摆），必须明确对应到具体哪只手，且该手没有被分配其他动作；
    - 无空闲手时，服装状态变化必须写为自然状态（如「领口自然滑落露出肩颈」而非「被拉下」），避免暗示额外手部动作导致肢体矛盾。

### 为什么不能重述为文字（重申）

生图模型接收到参考图时，会从图片直接提取视觉特征（服装款式、配色比例、剪裁、褶皱、材质、刺绣图案等），还原度远高于文字描述。文字描述不仅可能和图片实际内容矛盾（如颜色、款式判断错误），还会分散生图模型注意力，降低还原度。

**永远记住：能使用"保留参考图2的XX"，就不要使用对参考图2的文字描述。** 这是本模式的最高原则。

### 擦边增强模式

本条目为低频可选项，频率基本为6次中选择1次即可；cosplay 场景不启用。

> ⚠️ 静态图片——所有描述必须是单帧画面中肉眼可见的视觉事实，不存在"过程""变化""晃动"等时间维度。
核心：擦边感来自主角对镜头的主动迎合——用姿态、眼神、表情与服装状态共同传递色气感。每次选取合适维度重点展开，切忌逐条罗列。
- 服装：做刻意的凌乱——衣摆撩高、领口拉低、纽扣解开。
- 姿势：做向镜头的敞开——塌腰翘臀正对镜头、双腿不加掩饰地分开、展示足部、展示腿部、展示腋下。姿态是"我在给你看"的主动挑逗
- 表情（使用擦边时必选）：做享受与挑逗的混合——吐舌头、含住手指、舔食等可视觉化的面部动作。拒绝单一微笑或空洞迷离，禁止需要情绪解读的描写。（注：生图模型的能力有限，吐舌头描述仅限以下：吐出舌头、微微吐舌、俏皮吐舌、大幅吐舌。典型反例为"舌尖无意识地轻抵着下唇内侧"，动作难度大并且出现不可视觉化元素；舔食的推荐表述为"大幅伸出舌头舔xx"，舔手指是被允许的）。
- 氛围：场景不受限，日常公开场景中的反差同样有效。以光影处理为核心
- 皮肤：做身体表面的湿润光泽——轻微反光附着在锁骨、肩颈、大腿等暴露区域。光泽是可视觉化的生理信号（水类）；更进一步的是让面部与身体其他部位有沐浴露（不必在意场景、服装是否合理。只能是沐浴露，而非泡沫）
> 以上措辞仅用于展示各维度的方向、尺度与为你提供灵感。
例：
1. 带有情欲看向镜头（根本没法渲染）——嘴巴含住右手手指（用一个合适的方法表示出挑逗即可）

---

## 景别控制（两种模式通用）

生图模型对景别的判断容易"过一档"（写"特写"实际生成近景，写"近景"实际生成中近景，以此类推），因此提示词里写的景别要比期望的低一档。构建提示词时：

- 如果想要**特写**（头顶到肩部），提示词里写"大特写"或"大特写景别"
- 如果想要**近景**（头顶到腰部以上），提示词里写"特写景别"
- 如果想要**中近景**（头顶到大腿中部），提示词里写"近景"

无图模式：必须声明景别。根据设计方案的视觉焦点选择期望景别，然后按上述规则在提示词里写低一档的景别词。
有图模式：参考图2的构图已确定景别（full 模式），或按上述规则微调（reimagine/style 模式）；若参考图中人物占比略小（实际景别为中景及以上），需改写景别以拉近——在差量补充中追加「景别改为X」（X 按映射写低一档）。

---

## 常见生成失败预防

- **多人出现**：始终使用单人表述，避免"她们""人们"等复数词

- **风格偏移**：坚持写实基调，避免"插画感""海报风""动漫"等词汇

- **手部畸形**：手部涉及关键动作时，必须具体到手指动作和相对位置；手部非焦点时，简单明确地定位即可，避免过度聚焦手指细节反而引发畸形

- **文字水印**：禁止描述任何文字、标识、水印、Logo

- **肢体冗余**：始终明确两只手的位置和动作，避免模糊描述导致多出手臂

- **额头裸露**：避免描述无刘海的发型，所有发型描述必须包含刘海覆盖前额的表述

## 硬性规则

1. **输出格式**：每条设计方案/参考图对应一段完整的提示词，只输出提示词本身，不输出分析、编号、分点或规则解释

2. **只描述可见内容**：只描述镜头可以直接捕捉到的视觉信息，禁止写声音、气味、触感、情绪标签等不可见内容；只描述画面中能看到的服装结构与层次，被完全遮挡的部分不写；禁止写难以视觉化的内容（「假装生气」「故作惊讶」除外，见「表情铁律」）

3. **静态画面**：所有描述必须是单帧画面中肉眼可见的视觉事实，不存在"过程""变化""晃动"等时间维度

4. **动作完整性**：姿态描述必须给出足够信息让生图模型理解人物的整体姿态。身体朝向、头部角度、手部位置必须给出；眼神方向和表情在非焦点时可用最简表述，但不可完全缺失

5. **物理可行性**：所有姿势必须符合人体工学，人物只有两只手和两条腿，不能出现物理矛盾。此规则在过往执行中被多次违反，构建时必须格外注意。典型反例：描述里写"领口被轻轻拉下露出锁骨"，同时写"一只手搭在胸口，另一只手抓着半裙衣角"——没有空闲手执行拉领口的动作，逻辑矛盾。涉及删除手持物、服装变化等场景时，须同时遵守"差量补充规则"中的对应强制规则。

6. **光影自然**：光线描述应基于物理光源，避免抽象的光影形容词堆砌

7. **中文输出，无文字元素**：提示词必须使用中文；不得描述任何文字、标识、水印或象征性符号

8. **语言直白**：最终提示词应简单直白，这不是在写散文，避免如"柔樱粉甲油点缀指尖"此类表述

9. **魅力呈现方式**：通过服装的穿着状态和服装对体型的响应自然呈现角色魅力。忠实保留设计方案中的性感情趣元素，不得以规避风险为由弱化或删除。术语规范见"术语风险控制"章节，因果链保护见该章节同名小节——提示词仅需遵守这两项即可，不得额外保守化

10. **固定首尾**：
    - **无衣橱图时**：每条提示词以"以前1张参考图中少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图、文字水印，使用少女的面部特征为她本人生成一张新的照片："开头，以"胸部非常丰满，完全保留少女的面部特征与丰满的身材。"为固定收尾句结尾
    - **有衣橱图时**：每条提示词以"以前1张参考图中少女为基准，完整保留少女五官、身材等全部人体身份特征，绝对禁止任何拼图、文字水印；{严格匹配声明}，保留参考图2中人物的{保留维度}，{补充内容}，为少女生成一张单人的照片："开头，以"胸部非常丰满，完全保留少女的面部特征与丰满的身材。"为固定收尾句结尾；宽高比按规则13追加在收尾句之后

11. **提示词长度**：建议控制在150-300字之间（含固定首尾约70字，变量部分约80-230字），信息贵精不贵多，过载反而分散生图模型注意力。cosplay 场景以引用为主，不设字数下限，禁止为凑字数转录参考图2的细节。

12. **关键词避免**：禁止写"完全裸露"，该词极容易被拦截。需要露肩时直白写露肩即可

13. **分辨率比例追加（有图模式专属）**：看参考图2后，判断其横竖比例，在提示词最末尾（固定收尾句之后）以句号连接追加图片比例：竖屏参考图追加"宽高比为9:16"，横屏参考图追加"宽高比为16:9"，方形参考图追加"宽高比为9:16"（默认9:16）。追加后整体形如"…丰满的身材。宽高比为9:16"。其余可用的比例：4:3、3:4

14. 禁止直接使用"睫毛蕾丝"的表述方式，必须转换为"细毛边蕾丝"

## 输入

### 无衣橱图模式
你会收到一个 JSON 数组，每个元素包含：

- clothing：服装设计描述

- appearance：外观造型描述

- pose：动作姿势描述

- scene：场景环境描述

保留最关键的视觉细节，用自然的语序和节奏重新组织。设计方案中的因果链动作必须原样保留，不得补写敏感结果。

### 有衣橱图模式
你会收到：

- 风格（style）：如"cosplay""JK"

- 场景（scene）：如"午后的咖啡馆"

- 参考图力度（ref_strength）：full / style / reimagine

- 参考图2（图片）

看图后根据模式规则选择保留策略，构建引用式提示词。**切记：能引用就不描述，参考图2中已有的维度一律用"保留参考图2的XX"表述；cosplay 场景只移除遮脸相关元素，其余全部保留。**
"""

_NO_REF_PROMPT_ENGINEER_USER_PROMPT = (
    "请将以下 {count} 条服装设计方案转化为图像生成提示词：\n\n"
    "{designs}\n\n"
    "直接返回 {count} 条提示词，每条一行。"
)


class DailySelfieService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.counter = DailyQuotaCounter(plugin.data_dir)
        self._running = False
        self._cron_task: Optional[asyncio.Task] = None
        self._selfie_tasks: dict[str, asyncio.Task] = {}

        # 补拍调试事件内存缓冲区：供 /补拍debug 命令读取，避免依赖日志文件路径
        # （Docker 环境下日志路径与本地不同，硬编码路径会失效）
        # maxlen=300 保证内存占用可控，旧事件自动滚动淘汰
        self._debug_events: deque = deque(maxlen=300)
        self._debug_current_persona: str = ""

        # 今日已用参考图 id 集合：每张图每天补拍只用 1 次，避免单图风格（如护士服）反复引用同一张
        # 跨补拍任务保持（同一天内多次 /补拍 共享），日期变更时清空
        self._today_used_image_ids: set[str] = set()
        self._today_used_date: str = ""

        # token_router 插件实例缓存（跨插件上报补拍 LLM 阶段的 token 用量）
        self._token_router: Optional[Any] = None
        self._token_router_checked = False

    def _find_token_router(self):
        """跨插件查找 token_router 实例（需具备 record_storage_usage 方法）。

        首次查找后缓存结果；找不到或 token_router 未加载时返回 None。
        """
        if self._token_router_checked:
            return self._token_router
        self._token_router_checked = True
        try:
            stars = self.plugin.context.get_all_stars()
        except Exception:
            return None
        for meta in stars or []:
            p_id = str(getattr(meta, "id", "") or "")
            p_name = str(getattr(meta, "name", "") or "")
            root_dir_name = str(getattr(meta, "root_dir_name", "") or "")
            if (
                "token_router" not in p_id
                and "token_router" not in p_name
                and "token_router" not in root_dir_name
            ):
                continue
            for attr in ("star_instance", "instance", "star_cls"):
                candidate = getattr(meta, attr, None)
                if candidate is not None and hasattr(candidate, "record_storage_usage"):
                    self._token_router = candidate
                    return candidate
        return None

    def _report_llm_tokens(self, provider_id: str, resp) -> None:
        """将一次补拍 LLM 阶段调用的 token 用量上报给 token_router。

        上传走 record_plugin_usage：与聊天共享同一每日额度桶，合计到限时聊天照常顺延；
        补拍自身不参与路由，仍使用配置的 provider。
        """
        if not provider_id:
            return
        try:
            total_tokens = int(getattr(getattr(resp, "usage", None), "total", 0) or 0)
        except (ValueError, TypeError):
            return
        if total_tokens <= 0:
            return
        router = self._find_token_router()
        if router is None:
            return
        record = getattr(router, "record_plugin_usage", None)
        if record is None:
            record = getattr(router, "record_storage_usage", None)
        if record is None:
            return
        try:
            record(provider_id, total_tokens)
        except Exception as e:
            logger.warning(
                "[DailySelfie] 上报补拍token用量失败 provider=%s: %s", provider_id, e
            )

    def _record_debug(self, level: str, message: str) -> None:
        """记录一条补拍调试事件到内存缓冲区。

        level: "INFO" / "WARN" / "ERROR"
        message: 事件正文（不含 [DailySelfie] 前缀）
        """
        self._debug_events.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "persona": self._debug_current_persona,
            "message": message,
        })

    def get_debug_events(self) -> list[dict]:
        """返回当前缓冲区中所有调试事件（按时间顺序）。供 /补拍debug 命令调用。"""
        return list(self._debug_events)

    def clear_debug_events(self) -> None:
        """清空调试事件缓冲区。"""
        self._debug_events.clear()

    async def start(self):
        self._running = True
        self._cron_task = asyncio.create_task(self._cron_loop())
        logger.info("[DailySelfie] 服务已启动")

    async def stop(self):
        self._running = False
        if self._cron_task:
            self._cron_task.cancel()
            self._cron_task = None
        for name, task in list(self._selfie_tasks.items()):
            task.cancel()
        if self._selfie_tasks:
            await asyncio.gather(*self._selfie_tasks.values(), return_exceptions=True)
            self._selfie_tasks.clear()
        logger.info("[DailySelfie] 服务已停止")

    def _get_global_schedule_time(self) -> str:
        selfie_conf = self.plugin._get_feature("selfie")
        return str(selfie_conf.get("daily_selfie_schedule_time", "23:30") or "23:30").strip()

    def _get_provider_schedule_time(self, persona_name: str, provider: dict) -> str:
        provider_time = str(provider.get("schedule_time", "") or "").strip()
        if provider_time:
            return provider_time
        return self._get_global_schedule_time()

    def _parse_time_str(self, time_str: str) -> tuple[int, int]:
        try:
            parts = time_str.split(":")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return 23, 30

    def _seconds_until(self, hour: int, minute: int) -> float:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _seconds_until_next_run(self) -> float:
        schedules = self._get_all_schedule_times()
        if not schedules:
            return self._seconds_until(23, 30)
        min_seconds = float("inf")
        for hour, minute in schedules.values():
            s = self._seconds_until(hour, minute)
            if s < min_seconds:
                min_seconds = s
        return min_seconds

    def _get_all_schedule_times(self) -> dict[tuple[str, str], tuple[int, int]]:
        schedules = {}
        personas = self._get_enabled_personas()
        for p in personas:
            pname = p["persona_name"]
            for pv in p["providers"]:
                pid = pv["provider_id"]
                time_str = self._get_provider_schedule_time(pname, pv)
                schedules[(pname, pid)] = self._parse_time_str(time_str)
        return schedules

    async def _cron_loop(self):
        while self._running:
            try:
                wait_seconds = self._seconds_until_next_run()
                await asyncio.sleep(wait_seconds)
                if not self._running:
                    break
                await self._run_scheduled_personas()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[DailySelfie] 定时任务异常: %s", e)
                await asyncio.sleep(60)

    async def _run_scheduled_personas(self):
        now = datetime.now()
        current_h, current_m = now.hour, now.minute
        scheduled_entries: list[tuple[dict, str]] = []
        for p in self._get_enabled_personas():
            pname = p["persona_name"]
            for pv in p["providers"]:
                pid = pv["provider_id"]
                h, m = self._parse_time_str(self._get_provider_schedule_time(pname, pv))
                if h == current_h and m == current_m:
                    persona_copy = {
                        "index": p["index"],
                        "persona_name": pname,
                        "providers": [pv],
                        "config": p["config"],
                    }
                    scheduled_entries.append((persona_copy, pid))
        if not scheduled_entries:
            logger.debug("[DailySelfie] 当前时间无匹配的补画提供商，跳过")
            return
        unique_personas: dict[str, dict] = {}
        for persona_copy, _pid in scheduled_entries:
            pname = persona_copy["persona_name"]
            if pname not in unique_personas:
                unique_personas[pname] = {
                    "index": persona_copy["index"],
                    "persona_name": pname,
                    "providers": [],
                    "config": persona_copy["config"],
                }
            unique_personas[pname]["providers"].extend(persona_copy["providers"])
        merged = list(unique_personas.values())
        logger.debug("[DailySelfie] 触发补画: %s", ", ".join(
            f"{p['persona_name']}({', '.join(v['provider_id'] for v in p['providers'])})"
            for p in merged
        ))
        await self._run_personas(merged)

    def _get_enabled_personas(self) -> list[dict[str, Any]]:
        personas = []
        for idx in [1, 2, 3]:
            conf = self.plugin._get_selfie_persona_config(idx)
            if not conf:
                continue
            if not self.plugin._as_bool(conf.get("daily_selfie_enabled", False), default=False):
                continue

            providers = self._parse_providers_from_conf(conf, idx)

            if not providers:
                continue

            persona_name = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            if not persona_name or persona_name == "default":
                continue

            logger.debug(
                "[DailySelfie] selfie_persona_%d 已启用: persona=%s providers=%s",
                idx, persona_name, [p["provider_id"] for p in providers],
            )
            personas.append({
                "index": idx,
                "persona_name": persona_name,
                "providers": providers,
                "config": conf,
            })
        return personas

    def _parse_providers_from_conf(self, conf: dict, idx: int) -> list[dict]:
        providers_raw = conf.get("daily_selfie_providers", [])
        providers = []

        if isinstance(providers_raw, list) and providers_raw:
            for pv in providers_raw:
                if not isinstance(pv, dict):
                    continue
                pid = str(pv.get("provider_id", "") or "").strip()
                if not pid:
                    continue
                limit = self.plugin._as_int(pv.get("daily_limit", 10), default=10)
                schedule_time = str(pv.get("schedule_time", "") or "").strip()
                providers.append({
                    "provider_id": pid,
                    "daily_limit": limit,
                    "schedule_time": schedule_time,
                })

        if not providers:
            legacy_pid = str(conf.get("daily_selfie_provider_id", "") or "").strip()
            if legacy_pid:
                legacy_limit = self.plugin._as_int(conf.get("daily_selfie_limit", 10), default=10)
                legacy_schedule = str(conf.get("daily_selfie_schedule_time", "") or "").strip()
                logger.debug(
                    "[DailySelfie] selfie_persona_%d 从旧格式字段迁移: provider=%s limit=%d schedule=%s",
                    idx, legacy_pid, legacy_limit, legacy_schedule,
                )
                providers.append({
                    "provider_id": legacy_pid,
                    "daily_limit": legacy_limit,
                    "schedule_time": legacy_schedule,
                })

        return providers

    async def run_daily_selfie(self, persona_name: str = "", umo: str = ""):
        personas = self._get_enabled_personas()
        if not personas:
            logger.debug("[DailySelfie] 没有启用补画的人格，跳过")
            return

        if persona_name:
            personas = [p for p in personas if p["persona_name"] == persona_name]
            if not personas:
                logger.debug("[DailySelfie] 人格 %s 未启用补画，跳过", persona_name)
                return

        await self._run_personas(personas, umo)

    async def run_daily_selfie_single_provider(
        self, provider_id: str, umo: str = ""
    ) -> tuple[str, str]:
        """针对单个 provider 立即补拍。

        用于 /补拍 @provider_id 命令。行为：
        1. 查找配置了该 provider_id 的启用补画的 persona。
           注：多人格同时配置同一 provider 的情况按设计不应出现。
           若真的出现，只取第一个人格补拍，第二个人格忽略（见下方 "多 persona 冲突兜底"）。
        2. 检查该 persona + provider 今日剩余额度：
           - 若已耗尽，返回 ("no_quota", ...)，不启动任务。
           - 若 > 0，启动补拍任务（仅消耗该 provider 的额度，不影响同 persona 下其它 provider）。
        3. 若该 persona 已有补拍任务正在运行，返回 ("running", ...)，不启动新任务。

        :return: (status, message)
            status ∈ {"started", "no_quota", "running", "not_found", "no_wardrobe"}
        """
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            return ("not_found", "未指定提供商 ID")

        personas = self._get_enabled_personas()

        # 多 persona 冲突兜底：按设计不应出现同一 provider 被多个 persona 配置的情况。
        # 若真的出现，只取第一个人格，第二个人格忽略。
        matched_persona: dict | None = None
        for p in personas:
            for pv in p["providers"]:
                if pv["provider_id"] == provider_id:
                    matched_persona = p
                    break
            if matched_persona:
                break

        if not matched_persona:
            return ("not_found", f"未找到配置了 {provider_id} 提供商的补画人格，请检查配置")

        pname = matched_persona["persona_name"]

        # 检查任务冲突
        existing = self._selfie_tasks.get(pname)
        if existing and not existing.done():
            return ("running", f"人格 {pname} 补拍任务正在运行中，请稍后再试")

        # 预检查衣橱插件（_run_personas 内部也会检查，但那里是静默 return，
        # 这里提前返回避免误报 started）
        if not self.plugin._get_wardrobe_instance():
            return ("no_wardrobe", "衣橱插件不可用，无法补拍")

        # 检查该 provider 今日剩余额度
        remaining = 0
        limit = 0
        for pv in matched_persona["providers"]:
            if pv["provider_id"] == provider_id:
                limit = pv["daily_limit"]
                remaining = await self.counter.get_remaining(pname, provider_id, limit)
                break

        if remaining <= 0:
            return ("no_quota", f"人格 {pname} 的 {provider_id} 今日额度已用完（{limit}/{limit}），无需补拍")

        # 启动补拍任务（仅用指定 provider）
        await self._run_personas([matched_persona], umo, only_pid=provider_id)
        return ("started", f"已启动人格 {pname} 的 {provider_id} 补拍任务，剩余额度 {remaining} 张")

    async def _run_personas(self, personas: list[dict], umo: str = "", only_pid: str = ""):
        wardrobe = self.plugin._get_wardrobe_instance()
        if not wardrobe:
            logger.warning("[DailySelfie] 衣橱插件不可用，跳过补画")
            return

        launched = []
        for p in personas:
            pname = p["persona_name"]
            existing = self._selfie_tasks.get(pname)
            if existing and not existing.done():
                logger.warning("[DailySelfie] 人格 %s 补画任务正在运行中，跳过", pname)
                continue
            task = asyncio.create_task(
                self._execute_daily_selfie([p], wardrobe, umo, only_pid=only_pid)
            )
            self._selfie_tasks[pname] = task
            task.add_done_callback(lambda t, n=pname: self._selfie_tasks.pop(n, None))
            launched.append(pname)

        if launched:
            logger.info("[DailySelfie] 已启动补画任务: %s", ", ".join(launched))

    async def _execute_daily_selfie(self, personas: list[dict], wardrobe: Any, umo: str = "", only_pid: str = ""):
        total_success = 0
        total_fail = 0
        request_interval = 30

        # 日期变更时清空今日已用参考图集合
        today_str = datetime.now().strftime(_DATE_FMT)
        if self._today_used_date != today_str:
            self._today_used_image_ids.clear()
            self._today_used_date = today_str

        debug_mode = self._is_debug()
        selfie_conf = self.plugin._get_feature("selfie")
        logger.debug(
            "[DailySelfie] 补画开始: 人格数=%d debug=%s only_pid=%s selfie_conf_keys=%s",
            len(personas), debug_mode, only_pid or "无", list(selfie_conf.keys()),
        )

        try:
            recent_styles = await self._get_recent_styles(wardrobe)

            for p in personas:
                total_remaining = 0
                for pv in p["providers"]:
                    # only_pid 指定时只算该 provider 的剩余额度
                    if only_pid and pv["provider_id"] != only_pid:
                        continue
                    total_remaining += await self.counter.get_remaining(p["persona_name"], pv["provider_id"], pv["daily_limit"])
                if total_remaining <= 0:
                    logger.debug("[DailySelfie] 人格 %s 提供商 %s 额度已用完，跳过", p["persona_name"], only_pid or "全部")
                    continue

                style_pool = await self._get_style_pool(wardrobe, p["persona_name"])

                s, f = await self._process_persona_selfie(
                    p, wardrobe, style_pool, recent_styles, total_remaining, request_interval, umo, only_pid=only_pid
                )
                total_success += s
                total_fail += f

        except asyncio.CancelledError:
            logger.debug("[DailySelfie] 补画任务被取消")
        except Exception as e:
            logger.error("[DailySelfie] 补画任务异常: %s", e)
        finally:
            logger.info(
                "[DailySelfie] 补画完成: 成功=%d 失败=%d",
                total_success, total_fail,
            )
            self._record_debug(
                "INFO",
                f"补画完成: 成功={total_success} 失败={total_fail}",
            )
            # 补画流程结束，清空当前人格上下文
            self._debug_current_persona = ""

    def _get_persona_system_prompt(self, persona_name: str) -> str:
        try:
            persona_mgr = getattr(self.plugin.context, "persona_manager", None)
            if not persona_mgr:
                return ""
            if hasattr(persona_mgr, "get_persona_v3_by_id"):
                persona = persona_mgr.get_persona_v3_by_id(persona_name)
                if persona and isinstance(persona, dict):
                    return persona.get("prompt", "") or ""
            return ""
        except Exception as e:
            logger.warning("[DailySelfie] 获取人格 system prompt 失败: %s", e)
            return ""

    def _get_chat_provider_id(self, umo: str = "") -> str | None:
        selfie_conf = self.plugin._get_feature("selfie")
        configured = str(selfie_conf.get("daily_selfie_chat_provider_id", "") or "").strip()
        if configured:
            return configured
        if umo:
            try:
                provider = self.plugin.context.get_using_provider(umo=umo)
                if provider:
                    meta = provider.meta()
                    if meta and getattr(meta, "id", None):
                        return str(meta.id).strip() or None
            except Exception:
                pass
        try:
            provider = self.plugin.context.get_using_provider()
            if provider:
                meta = provider.meta()
                if meta and getattr(meta, "id", None):
                    return str(meta.id).strip() or None
        except Exception:
            pass
        try:
            pm = getattr(self.plugin.context, "provider_manager", None)
            if pm and hasattr(pm, "provider_insts"):
                for p in pm.provider_insts:
                    try:
                        m = p.meta()
                        if m and getattr(m, "id", None):
                            return str(m.id).strip()
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _get_costume_designer_system_prompt(self, persona: dict) -> str:
        """读取人格级创意设计系统提示词，留空则回退到内置默认常量。"""
        persona_conf = persona.get("config", {})
        configured = str(persona_conf.get("costume_designer_system_prompt", "") or "").strip()
        if configured:
            return configured
        return _COSTUME_DESIGNER_SYSTEM_PROMPT

    def _get_selfie_provider(self, stage: str, umo: str = "") -> str | None:
        """获取补拍指定轮次的 LLM provider。

        优先级：stage 全局配置 > daily_selfie_chat_provider_id > umo 会话 > 系统默认。
        stage ∈ {"scene"(r1), "designer"(r2), "reviewer"(r3), "prompt_engineer"(r4)}。
        """
        selfie_conf = self.plugin._get_feature("selfie")
        key_map = {
            "scene": "daily_selfie_scene_provider_id",
            "designer": "daily_selfie_designer_provider_id",
            "reviewer": "daily_selfie_reviewer_provider_id",
            "prompt_engineer": "daily_selfie_prompt_engineer_provider_id",
        }
        configured = str(selfie_conf.get(key_map.get(stage, ""), "") or "").strip()
        if configured:
            return configured
        return self._get_chat_provider_id(umo)

    def _get_prompt_engineer_system_prompt(self, persona: dict, persona_ref_count: int = 3) -> str:
        """读取人格级提示词构建系统提示词，留空则回退到内置默认常量。

        persona_ref_count 为人设参考图数量，用于把提示词里"以 1 张人设图为准"的表述
        换算成实际张数（人设图张数、参考图总数、衣橱图序号三处）。
        """
        persona_conf = persona.get("config", {})
        configured = str(persona_conf.get("prompt_engineer_system_prompt", "") or "").strip()
        base = configured if configured else _NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT
        return _apply_persona_ref_count(base, persona_ref_count)

    def _get_reviewer_system_prompt(self, persona: dict) -> str:
        """读取人格级审核师系统提示词，留空则回退到内置默认常量。

        与设计师/提示词工程师保持一致的配置模式：每个 persona 可独立定制审核尺度，
        例如某个角色对发型的硬约束更严格、或某个角色希望审核更激进/保守。
        """
        persona_conf = persona.get("config", {})
        configured = str(persona_conf.get("reviewer_system_prompt", "") or "").strip()
        if configured:
            return configured
        return _COSTUME_REVIEWER_SYSTEM_PROMPT

    @staticmethod
    def _parse_costume_designer_json(text: str, expected_count: int) -> list[dict] | None:
        text = text.strip()
        if text.startswith("```"):
            first_newline = text.index("\n") if "\n" in text else -1
            if first_newline >= 0:
                text = text[first_newline + 1:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if not isinstance(result, list):
            return None

        valid: list[dict] = []
        for item in result:
            if isinstance(item, dict):
                clothing = str(item.get("clothing", "") or "").strip()
                appearance = str(item.get("appearance", "") or "").strip()
                pose = str(item.get("pose", "") or "").strip()
                scene = str(item.get("scene", "") or "").strip()
                if clothing or appearance or pose or scene:
                    valid.append({"clothing": clothing, "appearance": appearance, "pose": pose, "scene": scene})

        if len(valid) < expected_count:
            logger.warning("[DailySelfie] 创意设计返回 %d/%d 条", len(valid), expected_count)

        return valid if valid else None

    async def _process_persona_selfie(
        self,
        persona: dict,
        wardrobe: Any,
        style_pool: list[str],
        recent_styles: list[str],
        remaining: int,
        request_interval: int,
        umo: str = "",
        only_pid: str = "",
    ) -> tuple[int, int]:
        persona_name = persona["persona_name"]
        success = 0
        fail = 0

        provider_names = [pv.get("provider_id", "?") for pv in persona["providers"]]
        self._debug_current_persona = persona_name
        logger.debug(
            "[DailySelfie] 开始处理人格 %s，总剩余额度 %d（提供商: %s）",
            persona_name,
            remaining,
            ", ".join(provider_names) if provider_names else "无",
        )
        self._record_debug(
            "INFO",
            f"开始处理人格 {persona_name}，总剩余额度 {remaining}（提供商: {', '.join(provider_names) if provider_names else '无'}）",
        )

        chat_provider_id = self._get_chat_provider_id(umo)
        if not chat_provider_id:
            logger.error("[DailySelfie] 无法获取默认 LLM Provider，跳过人格 %s", persona_name)
            return 0, 0

        # 各轮次专用 provider（留空则回退到 chat_provider_id）
        scene_provider_id = self._get_selfie_provider("scene", umo) or chat_provider_id
        designer_provider_id = self._get_selfie_provider("designer", umo) or chat_provider_id
        reviewer_provider_id = self._get_selfie_provider("reviewer", umo) or chat_provider_id
        prompt_engineer_provider_id = self._get_selfie_provider("prompt_engineer", umo) or chat_provider_id

        styles_task = self._select_styles_by_algorithm(remaining, style_pool, recent_styles)
        scenes_task = self._llm_round1_scene(scene_provider_id, remaining)

        styles, scenes = await asyncio.gather(styles_task, scenes_task)

        if not styles:
            logger.warning("[DailySelfie] 人格 %s r0算法选风格未返回结果", persona_name)
            return 0, 0
        if not scenes:
            logger.warning("[DailySelfie] 人格 %s r1场景未返回结果", persona_name)
            return 0, 0

        pair_count = min(len(styles), len(scenes))
        styles = styles[:pair_count]
        scenes = scenes[:pair_count]

        logger.debug(
            "[DailySelfie] 人格 %s r0算法选风格返回 %d 条，r1场景返回 %d 条场景，配对 %d 组",
            persona_name, len(styles), len(scenes), pair_count,
        )

        search_queries = [f"{s} {c}" for s, c in zip(styles, scenes)]

        selfie_conf = self.plugin._get_feature("selfie")
        daily_ref_min_sim_raw = float(selfie_conf.get("daily_selfie_ref_min_similarity", 0) or 0)
        daily_ref_min_sim = daily_ref_min_sim_raw if daily_ref_min_sim_raw > 0 else None
        if daily_ref_min_sim is not None:
            logger.debug("[DailySelfie] 人格 %s 补拍搜图阈值: %s", persona_name, daily_ref_min_sim)

        # cosplay 选中时强制阈值=0，确保能搜到参考图
        # 注意：传 None 会被 wardrobe.vector_searcher 回退为全局阈值（默认0.5），
        # 必须显式传 0.0 才能真正不过滤
        per_query_sim: list[float | None] | None = None
        if any(s == "cosplay" for s in styles):
            per_query_sim = [0.0 if s == "cosplay" else daily_ref_min_sim for s in styles]

        # 搜图对所有后端一视同仁（ark_seedream 也不例外）：
        # ark 的唯一区别是生图时人设图只保留第一张，衣橱图照常注入
        ref_results = await self._search_reference_images(search_queries, wardrobe, persona_name, min_similarity=daily_ref_min_sim, per_query_min_similarity=per_query_sim)

        # 任何风格无参考图 → 排除当前风格和近期风格，换一个风格重搜1次
        # 解决单图风格（如护士服）今日已用时搜不到图的问题
        recent_set = set(recent_styles)
        for i in range(pair_count):
            if ref_results[i] is None:
                alt_pool = [s for s in style_pool if s != styles[i] and s not in recent_set]
                if not alt_pool:
                    alt_pool = [s for s in style_pool if s != styles[i]]
                if alt_pool:
                    new_style = random.choice(alt_pool)
                    new_query = f"{new_style} {scenes[i]}"
                    # 换到 cosplay 时用 0.0 阈值，其他用常规阈值
                    retry_sim = 0.0 if new_style == "cosplay" else daily_ref_min_sim
                    logger.debug(
                        "[DailySelfie] 人格 %s 无参考图，换风格: %s→%s",
                        persona_name, styles[i], new_style,
                    )
                    retry_ref = await self._search_reference_images(
                        [new_query], wardrobe, persona_name, min_similarity=retry_sim,
                    )
                    styles[i] = new_style
                    if retry_ref and retry_ref[0] is not None:
                        ref_results[i] = retry_ref[0]

        ref_by_pair: dict[int, dict] = {}
        for i, ref in enumerate(ref_results):
            if ref is not None and i < pair_count:
                ref_by_pair[i] = ref

        ref_found_count = len([r for r in ref_results if r is not None])
        logger.debug("[DailySelfie] 人格 %s 搜图完成，找到 %d 张参考图（共 %d 组配对）", persona_name, ref_found_count, pair_count)
        self._record_debug("INFO", f"搜图完成，找到 {ref_found_count} 张参考图（共 {pair_count} 组配对）")

        persona_ref_count = len(self.plugin._get_persona_config_selfie_reference_paths(persona_name))
        search_ref_index = persona_ref_count + 1

        ref_descriptions: list[str] = []
        ref_by_index: list[dict | None] = []
        for i in range(pair_count):
            ref = ref_by_pair.get(i)
            if ref:
                desc = ref.get("description", "")
                if desc:
                    ref_descriptions.append(
                        f"参考图{search_ref_index}描述：{desc}\n\n{_build_ref_hint(persona_ref_count)}\n\n"
                        f"这张参考图的序号为{search_ref_index}，请在提示词中使用序号{search_ref_index}来引用该参考图。"
                    )
                else:
                    ref_descriptions.append("")
                ref_by_index.append(ref)
            else:
                ref_descriptions.append("")
                ref_by_index.append(None)

        costume_system_prompt = self._get_costume_designer_system_prompt(persona)
        prompt_engineer_system_prompt = self._get_prompt_engineer_system_prompt(persona, persona_ref_count)
        reviewer_system_prompt = self._get_reviewer_system_prompt(persona)

        batch_size = 2
        total_batches = (pair_count + batch_size - 1) // batch_size

        # r2→r3→r4→画图 流水线：每条线独立完成四步，不再被其他批次的延迟重试阻塞
        # r2 启动错开：主循环 sleep BATCH_STAGGER_SECONDS
        # r3/r4/画图 启动错开：调度器确保距上次启动至少 BATCH_STAGGER_SECONDS
        r3_scheduler = {"last_start": None, "lock": asyncio.Lock()}
        r4_scheduler = {"last_start": None, "lock": asyncio.Lock()}
        image_scheduler = {"last_start": None, "lock": asyncio.Lock()}

        batch_tasks: list[asyncio.Task] = []
        for batch_num, batch_start in enumerate(range(0, pair_count, batch_size), 1):
            if batch_num > 1:
                logger.debug(
                    "[DailySelfie] 人格 %s 错开 %ds 启动 r2设计 批次 %d/%d",
                    persona_name, BATCH_STAGGER_SECONDS, batch_num, total_batches,
                )
                await asyncio.sleep(BATCH_STAGGER_SECONDS)

            batch_styles = styles[batch_start:batch_start + batch_size]
            batch_scenes = scenes[batch_start:batch_start + batch_size]
            batch_refs_desc = ref_descriptions[batch_start:batch_start + batch_size]
            batch_refs = ref_by_index[batch_start:batch_start + batch_size]

            task = asyncio.create_task(
                self._process_design_batch(
                    persona_name, batch_num, total_batches,
                    batch_styles, batch_scenes, batch_refs_desc, batch_refs,
                    designer_provider_id, reviewer_provider_id, prompt_engineer_provider_id,
                    costume_system_prompt, reviewer_system_prompt, prompt_engineer_system_prompt,
                    r3_scheduler, r4_scheduler, image_scheduler,
                    persona, only_pid, persona_ref_count,
                )
            )
            batch_tasks.append(task)

        # 聚合各批次结果（return_exceptions=True 作为防御性深度保护，
        # _process_design_batch 顶层 try/except 已保证单批次异常返回 (0,0,{},[]) ）
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

        failed_items: list[tuple[str, str, str, str]] = []
        provider_success: dict[str, list[Path]] = {}

        for idx, br in enumerate(batch_results):
            # _process_design_batch 顶层 try/except 已保证不抛异常，这里兜底防御
            if isinstance(br, Exception):
                logger.error(
                    "[DailySelfie] 人格 %s 批次 %d 聚合时发现未捕获异常: %s",
                    persona_name, idx + 1, br,
                )
                continue
            if not isinstance(br, tuple) or len(br) != 4:
                logger.error(
                    "[DailySelfie] 人格 %s 批次 %d 返回结构非法: %r",
                    persona_name, idx + 1, br,
                )
                continue
            b_success, b_fail, b_provider_success, b_failed_items = br
            success += b_success
            fail += b_fail
            for pid, paths in b_provider_success.items():
                provider_success.setdefault(pid, []).extend(paths)
            failed_items.extend(b_failed_items)

        logger.debug(
            "[DailySelfie] 人格 %s 所有批次聚合完成: success=%d fail=%d failed_items=%d",
            persona_name, success, fail, len(failed_items),
        )
        self._record_debug(
            "INFO",
            f"所有批次聚合完成: success={success} fail={fail} failed_items={len(failed_items)}",
        )

        # 所有批次在设计阶段就全部失败（success=0, fail=0, failed_items=0）：
        # 额度全部计入失败。注意 fail==0 是关键——若 r4 返回空字符串提示词，
        # fail>0 但 failed_items 仍为空，此时不应 early return（应返回实际的 fail 计数）。
        if success == 0 and fail == 0 and not failed_items:
            logger.warning(
                "[DailySelfie] 人格 %s 所有批次均失败（success=0, fail=0, failed_items=0），%d 个额度全部计入失败",
                persona_name, remaining,
            )
            return 0, remaining

        if failed_items:
            retry_enabled = self._is_retry_on_fail()
            logger.debug(
                "[DailySelfie] 人格 %s 失败 %d 张，重试开关=%s",
                persona_name, len(failed_items), retry_enabled,
            )

            if retry_enabled:
                for prompt_text, ref_path, ref_strength, ref_user_tags in failed_items:
                    selected_pid = await self._reserve_provider(persona, only_pid=only_pid)
                    if selected_pid is None:
                        logger.debug("[DailySelfie] 人格 %s 重试时所有提供商额度用完，停止", persona_name)
                        break

                    logger.debug(
                        "[DailySelfie] 人格 %s 重试画图: provider=%s ref=%s",
                        persona_name, selected_pid, ref_path[:50] if ref_path else "纯文生图",
                    )
                    await asyncio.sleep(request_interval)

                    try:
                        image_path = await asyncio.wait_for(
                            self.plugin._generate_daily_selfie_image(
                                persona_name=persona_name,
                                prompt=prompt_text,
                                ref_image_path=ref_path,
                                ref_strength=ref_strength,
                                persona_conf=persona["config"],
                                provider_id=selected_pid,
                            ),
                            timeout=300,
                        )
                        if image_path:
                            logger.debug("[DailySelfie] 人格 %s 重试成功: %s provider=%s", persona_name, image_path, selected_pid)
                            await self._save_to_wardrobe(image_path, persona_name, prompt=prompt_text, ref_user_tags=ref_user_tags)
                            provider_success.setdefault(selected_pid, []).append(image_path)
                            success += 1
                            fail -= 1
                        else:
                            await self.counter.release(persona_name, selected_pid)
                            logger.warning("[DailySelfie] 人格 %s 重试返回空路径", persona_name)
                    except asyncio.TimeoutError:
                        await self.counter.release(persona_name, selected_pid)
                        logger.error("[DailySelfie] 人格 %s 重试超时(300s)", persona_name)
                    except Exception as e:
                        await self.counter.release(persona_name, selected_pid)
                        logger.error("[DailySelfie] 人格 %s 重试失败: %s", persona_name, e)

        for pid, paths in provider_success.items():
            if paths:
                logger.debug("[DailySelfie] 人格 %s 提供商 %s 完成 %d 张，发布空间", persona_name, pid, len(paths))
                self._record_debug("INFO", f"提供商 {pid} 完成 {len(paths)} 张，发布空间")
                await self._publish_to_qzone(persona_name, paths, persona["config"])

        return success, fail

    async def _reserve_provider(self, persona: dict, only_pid: str = "") -> str | None:
        """预留画图额度。

        :param only_pid: 若指定，则只尝试该 provider_id，不会顺序尝试其它 provider。
            用于 /补拍 @provider_id 命令——只消耗指定 provider 的额度，不影响同 persona 下其它 provider。
        """
        pname = persona["persona_name"]
        for pv in persona["providers"]:
            pid = pv["provider_id"]
            if only_pid and pid != only_pid:
                continue
            limit = pv["daily_limit"]
            if await self.counter.reserve(pname, pid, limit):
                logger.debug("[DailySelfie] 预留额度: persona=%s provider=%s limit=%s", pname, pid, limit)
                return pid
        return None

    async def _generate_one_selfie(
        self,
        persona_name: str,
        prompt: str,
        ref_image_path: str,
        ref_strength: str,
        persona: dict,
        provider_id: str = "",
        ref_user_tags: str = "",
    ) -> Path | None:
        logger.debug("[DailySelfie] 人格 %s 开始画图: provider=%s ref=%s prompt_len=%d", persona_name, provider_id, ref_image_path[:50] if ref_image_path else "空", len(prompt))
        try:
            image_path = await asyncio.wait_for(
                self.plugin._generate_daily_selfie_image(
                    persona_name=persona_name,
                    prompt=prompt,
                    ref_image_path=ref_image_path,
                    ref_strength=ref_strength,
                    persona_conf=persona["config"],
                    provider_id=provider_id,
                ),
                timeout=300,
            )
            if image_path:
                logger.info("[DailySelfie] 人格 %s 补画成功: %s", persona_name, image_path)
                await self._save_to_wardrobe(image_path, persona_name, prompt=prompt, ref_user_tags=ref_user_tags)
                return image_path
            else:
                logger.warning("[DailySelfie] 人格 %s 补画返回空路径", persona_name)
                return None
        except asyncio.TimeoutError:
            logger.error("[DailySelfie] 人格 %s 画图超时(300s)", persona_name)
            return None
        except Exception as e:
            logger.error("[DailySelfie] 人格 %s 生图失败: %s", persona_name, e, exc_info=True)
            return None

    def _is_debug(self) -> bool:
        selfie_conf = self.plugin._get_feature("selfie")
        return bool(selfie_conf.get("daily_selfie_debug", False))

    def _is_retry_on_fail(self) -> bool:
        selfie_conf = self.plugin._get_feature("selfie")
        return bool(selfie_conf.get("daily_selfie_retry_on_fail", True))

    async def _save_to_wardrobe(self, image_path: Path, persona_name: str, prompt: str = "", ref_user_tags: str = "") -> None:
        wardrobe = self.plugin._get_wardrobe_instance()
        if not wardrobe or not hasattr(wardrobe, "_save_image_from_bytes"):
            return
        try:
            import aiofiles
            async with aiofiles.open(image_path, "rb") as f:
                image_bytes = await f.read()
            if not image_bytes:
                return
            # 若本张补画图调用了衣橱参考图，则把该参考图的用户备注(user_tags)透传入库，
            # 使新图继承同样的备注。参考图无备注时为空，不影响原有行为。
            image_id, attrs, duplicate = await wardrobe._save_image_from_bytes(
                image_bytes, persona=persona_name, created_by="daily_selfie", ai_prompt=prompt or "",
                user_description=ref_user_tags,
            )
            if duplicate:
                logger.debug("[DailySelfie] 补画图片已存在于衣橱，跳过: %s", image_id)
            elif image_id:
                logger.debug("[DailySelfie] 补画图片已保存到衣橱: %s", image_id)
        except Exception as e:
            logger.debug("[DailySelfie] 补画图片保存到衣橱失败: %s", e)

    async def _publish_to_qzone(
        self,
        persona_name: str,
        image_paths: list[Path],
        persona_conf: dict,
    ) -> None:
        if not image_paths:
            return

        enabled = self.plugin._as_bool(
            persona_conf.get("daily_selfie_qzone_publish_enabled", False), default=False
        )
        provider_id = str(
            persona_conf.get("daily_selfie_qzone_chat_provider_id", "") or ""
        ).strip()

        if not enabled or not provider_id:
            logger.debug(
                "[DailySelfie] 人格 %s 未启用空间发布或未配置多模态提供商，跳过",
                persona_name,
            )
            self._record_debug("INFO", "未启用空间发布或未配置多模态提供商，跳过")
            return

        caption = await self._generate_qzone_caption(
            persona_name, image_paths, provider_id
        )
        if not caption:
            caption = datetime.now().strftime("%Y-%m-%d")
            logger.warning(
                "[DailySelfie] 人格 %s 生成空间配文失败，使用日期作为回退配文",
                persona_name,
            )

        image_data: list[bytes] = []
        for p in image_paths[:9]:
            if p.exists():
                try:
                    raw = await asyncio.to_thread(p.read_bytes)
                    logger.debug(
                        "[DailySelfie] 读取图片: path=%s size=%d bytes magic=%s",
                        p, len(raw), raw[:16].hex() if len(raw) >= 16 else raw.hex(),
                    )
                    converted = self._ensure_qzone_compatible_image(raw)
                    if converted is not None:
                        image_data.append(converted)
                    else:
                        logger.warning(
                            "[DailySelfie] 图片格式转换失败，跳过: %s", p
                        )
                except Exception as e:
                    logger.warning("[DailySelfie] 读取图片失败，跳过: %s, err=%s", p, e)

        if not image_data:
            return

        qzone_star = self.plugin.context.get_registered_star(
            "astrbot_plugin_qzone_Inoryu7z"
        )
        if not qzone_star or not qzone_star.activated:
            logger.warning("[DailySelfie] qzone 插件未启用，跳过发布")
            return

        qzone_plugin = qzone_star.star_cls
        if not hasattr(qzone_plugin, "controller") or qzone_plugin.controller is None:
            logger.warning("[DailySelfie] qzone 插件 controller 不可用，跳过发布")
            return

        media_items: list[dict] = []
        tmp_dir = Path(tempfile.gettempdir()) / "aiimg_qzone_publish"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for idx, img_bytes in enumerate(image_data):
            tmp_path = tmp_dir / f"qzone_publish_{uuid.uuid4().hex[:8]}_{idx}.jpg"
            await asyncio.to_thread(tmp_path.write_bytes, img_bytes)
            media_items.append({"source": str(tmp_path), "kind": "image", "trusted_local": True})

        try:
            await qzone_plugin.controller.publish_post(
                content=caption, media=media_items, content_sanitized=True
            )
            logger.info(
                "[DailySelfie] 人格 %s 空间说说发布成功，共 %d 张图",
                persona_name,
                len(image_data),
            )
        except Exception as e:
            logger.error(
                "[DailySelfie] 人格 %s 空间说说发布失败: %s", persona_name, e
            )

    async def _generate_qzone_caption(
        self,
        persona_name: str,
        image_paths: list[Path],
        provider_id: str,
    ) -> str:
        persona_system_prompt = self._get_persona_system_prompt(persona_name)

        tmp_dir = Path(tempfile.gettempdir()) / "aiimg_qzone"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        caption_image_paths: list[str] = []
        tmp_files: list[Path] = []
        for p in image_paths[:8]:
            try:
                tmp_file = tmp_dir / f"qzone_{persona_name}_{uuid.uuid4().hex[:8]}_{p.stem}.jpg"
                await asyncio.to_thread(
                    self._compress_image_for_caption, p, tmp_file, 1024, 80
                )
                caption_image_paths.append(tmp_file.as_uri())
                tmp_files.append(tmp_file)
            except Exception as e:
                logger.warning(
                    "[DailySelfie] 准备配文图片失败: %s, err=%s", p, e
                )

        user_prompt = (
            "你今天拍了一些照片，请以第一人称写一条QQ空间说说配文。"
            "要求：像日常分享一样随意自然，不要逐张图片描述，可以聊聊今天的心情、做了什么事、或者对照片的随意点评。"
            "禁止使用任何markdown格式、编号、标签、emoji。"
        )

        result_text = ""
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=user_prompt,
                        image_urls=caption_image_paths if caption_image_paths else None,
                        system_prompt=persona_system_prompt,
                    ),
                    timeout=600,
                )
                self._report_llm_tokens(provider_id, resp)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if text:
                    logger.debug(
                        "[DailySelfie] 人格 %s 生成空间配文成功: %s",
                        persona_name,
                        text[:50],
                    )
                    result_text = text
                    break
            except asyncio.TimeoutError:
                logger.warning(
                    "[DailySelfie] 人格 %s 生成空间配文超时(重试%d/2)", persona_name, attempt + 1
                )
                if attempt == 0:
                    logger.debug("[DailySelfie] 人格 %s 将重试一次", persona_name)
                    continue
            except Exception as e:
                logger.warning(
                    "[DailySelfie] 人格 %s 生成空间配文失败(重试%d/2): %s", persona_name, attempt + 1, e
                )
                if attempt == 0:
                    logger.debug("[DailySelfie] 人格 %s 将重试一次", persona_name)
                    continue

        for f in tmp_files:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

        return result_text

    @staticmethod
    def _ensure_qzone_compatible_image(raw: bytes) -> bytes | None:
        try:
            from PIL import Image as PILImage

            img = PILImage.open(io.BytesIO(raw))
            fmt = img.format
            mode = img.mode
            logger.debug(
                "[DailySelfie] PIL 检测图片格式: %s, 模式: %s, 尺寸: %s, 原始大小: %d bytes",
                fmt, mode, img.size, len(raw),
            )
            if mode in ("RGBA", "LA", "P"):
                background = PILImage.new("RGB", img.size, (255, 255, 255))
                if mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
                img = background
            elif mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95, progressive=False)
            result = buf.getvalue()
            logger.debug(
                "[DailySelfie] 图片已重编码为 baseline RGB JPEG: %d -> %d bytes, magic=%s",
                len(raw), len(result),
                result[:8].hex() if len(result) >= 8 else result.hex(),
            )
            return result
        except Exception as e:
            logger.warning("[DailySelfie] 图片格式转换失败: %s", e)
            return None

    @staticmethod
    def _compress_image_for_caption(
        src: Path, dst: Path, max_size: int = 1024, quality: int = 80
    ) -> None:
        from PIL import Image as PILImage

        img = PILImage.open(src)
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
        img.save(dst, format="JPEG", quality=quality)

    def _get_cosplay_weight(self) -> float:
        """cosplay 在 r0 加权抽样中的权重（其余风格恒为 1）。

        读取 features.selfie.daily_selfie_cosplay_weight，默认 3.0。
        留空用默认；非数字、非正数、NaN/Inf 一律回退默认，避免 random.choices 报错。
        """
        default = 3.0
        try:
            raw = self.plugin._get_feature("selfie").get(
                "daily_selfie_cosplay_weight", default
            )
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return default
            weight = float(raw)
        except Exception as e:
            logger.warning("[DailySelfie] cosplay 权重读取失败，回退 %s: %s", default, e)
            return default
        # NaN 会让区间判断整体为 False，一并被拦下
        if not (0 < weight < 1000):
            logger.warning(
                "[DailySelfie] cosplay 权重 %s 非法（须为大于 0 的有限数），回退 %s",
                weight,
                default,
            )
            return default
        return weight

    async def _select_styles_by_algorithm(
        self,
        count: int,
        style_pool: list[str],
        recent_styles: list[str],
    ) -> list[str]:
        """r0: 算法选择风格（近期去重+加权有放回抽样，cosplay 豁免去重）。

        策略：
        1. 从风格池中过滤掉近期已拍过的风格，得"新鲜池"（cosplay 始终保留在新鲜池中）
        2. 若新鲜池非空，从新鲜池中加权有放回抽 count 个
           （cosplay 权重取 features.selfie.daily_selfie_cosplay_weight，默认 3；其余=1）
        3. 若新鲜池为空，从全部风格池中加权有放回抽 count 个
        - 有放回允许同一风格被多次选中（如一次补拍拍多张 cosplay）
        """
        if not style_pool or count <= 0:
            return []

        recent_set = set(recent_styles)
        fresh_pool = [s for s in style_pool if s not in recent_set or s == "cosplay"]

        pool = fresh_pool if fresh_pool else style_pool
        picked = self._weighted_choices(pool, count, self._get_cosplay_weight())

        picked = picked[:count]
        logger.debug(
            "[DailySelfie] r0算法选风格: pool=%d recent=%d fresh=%d picked=%s",
            len(style_pool), len(recent_set), len(fresh_pool), picked,
        )
        return picked

    @staticmethod
    def _weighted_choices(
        pool: list[str], k: int, cosplay_weight: float = 3.0
    ) -> list[str]:
        """从 pool 中加权有放回抽取 k 个元素。cosplay 权重=cosplay_weight，其余=1。"""
        if not pool or k <= 0:
            return []
        weights = [cosplay_weight if s == "cosplay" else 1 for s in pool]
        try:
            return random.choices(pool, weights=weights, k=k)
        except Exception as e:
            logger.warning("[DailySelfie] 加权抽样失败，回退等概率: %s", e)
            return random.choices(pool, k=k)

    @staticmethod
    async def _stagger_start(scheduler: dict) -> None:
        """确保调用之间至少间隔 BATCH_STAGGER_SECONDS 秒。

        锁只在等待期间持有，释放后请求可并发执行——只错开启动，不串行执行。
        """
        async with scheduler["lock"]:
            now = datetime.now()
            last = scheduler["last_start"]
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < BATCH_STAGGER_SECONDS:
                    wait = BATCH_STAGGER_SECONDS - elapsed
                    logger.debug("[DailySelfie] 错开等待 %ds 后启动", int(wait))
                    await asyncio.sleep(wait)
            scheduler["last_start"] = datetime.now()

    def _merge_sub_batch_results(
        self, batch_num: int, total_batches: int, sub_results: list,
    ) -> tuple[int, int, dict[str, list[Path]], list[tuple[str, str, str, str]]]:
        """合并混合批次两条子链路（有图 / 无图）的返回结果。"""
        success = 0
        fail = 0
        provider_success: dict[str, list[Path]] = {}
        failed_items: list[tuple[str, str, str, str]] = []
        for idx, sub in enumerate(sub_results):
            if isinstance(sub, Exception):
                logger.error(
                    "[DailySelfie] 批次 %d/%d 子链路 %d 未捕获异常: %s",
                    batch_num, total_batches, idx, sub,
                )
                continue
            if not isinstance(sub, tuple) or len(sub) != 4:
                logger.error(
                    "[DailySelfie] 批次 %d/%d 子链路 %d 返回结构非法: %r",
                    batch_num, total_batches, idx, sub,
                )
                continue
            sub_success, sub_fail, sub_provider_success, sub_failed_items = sub
            success += sub_success
            fail += sub_fail
            for pid, paths in sub_provider_success.items():
                provider_success.setdefault(pid, []).extend(paths)
            failed_items.extend(sub_failed_items)
        logger.debug(
            "[DailySelfie] 批次 %d/%d 混合批次合并: success=%d fail=%d failed_items=%d",
            batch_num, total_batches, success, fail, len(failed_items),
        )
        return (success, fail, provider_success, failed_items)

    async def _process_design_batch(
        self,
        persona_name: str,
        batch_num: int,
        total_batches: int,
        batch_styles: list[str],
        batch_scenes: list[str],
        batch_refs_desc: list[str],
        batch_refs: list[dict | None],
        designer_provider_id: str,
        reviewer_provider_id: str,
        prompt_engineer_provider_id: str,
        costume_system_prompt: str,
        reviewer_system_prompt: str,
        prompt_engineer_system_prompt: str,
        r3_scheduler: dict,
        r4_scheduler: dict,
        image_scheduler: dict,
        persona: dict,
        only_pid: str,
        persona_ref_count: int = 3,
    ) -> tuple[int, int, dict[str, list[Path]], list[tuple[str, str, str]]]:
        """处理一个批次的 r2设计→r3审核→r4翻译→画图，返回 (success, fail, provider_success, failed_items)。

        流水线模式下每条线独立跑完四步，画图不再等待其他批次的 r2/r3/r4 完成。
        顶层 try/except 保证单批次异常不影响其他批次的结果。
        """
        try:
            # 参考图分流按「逐条」判定，而非整批同进退：旧写法只要同批次里有一组
            # 没搜到参考图，就会把有图的那组也一起退回 r2/r3 重设计——cosplay 这类
            # 场景一旦经过设计师就会被重新臆造一套服装，r4 随后按无图模式把它转写成
            # 文字、参考图被彻底丢弃。
            ref_positions = [i for i, ref in enumerate(batch_refs) if ref is not None]
            nonref_positions = [i for i, ref in enumerate(batch_refs) if ref is None]

            if ref_positions and nonref_positions:
                # 混合批次：有图子集与无图子集各跑一条链路，并发后合并结果
                logger.debug(
                    "[DailySelfie] 人格 %s 批次 %d/%d 混合：有图 %d 组走 r4 看图、无图 %d 组走 r2/r3",
                    persona_name, batch_num, total_batches,
                    len(ref_positions), len(nonref_positions),
                )
                sub_results = await asyncio.gather(
                    self._process_ref_batch(
                        persona_name, batch_num, total_batches,
                        [batch_styles[i] for i in ref_positions],
                        [batch_scenes[i] for i in ref_positions],
                        [batch_refs[i] for i in ref_positions],
                        prompt_engineer_provider_id, prompt_engineer_system_prompt,
                        r4_scheduler, image_scheduler, persona, only_pid, persona_ref_count,
                    ),
                    self._process_design_batch(
                        persona_name, batch_num, total_batches,
                        [batch_styles[i] for i in nonref_positions],
                        [batch_scenes[i] for i in nonref_positions],
                        [batch_refs_desc[i] for i in nonref_positions],
                        [batch_refs[i] for i in nonref_positions],
                        designer_provider_id, reviewer_provider_id, prompt_engineer_provider_id,
                        costume_system_prompt, reviewer_system_prompt, prompt_engineer_system_prompt,
                        r3_scheduler, r4_scheduler, image_scheduler,
                        persona, only_pid, persona_ref_count,
                    ),
                    return_exceptions=True,
                )
                return self._merge_sub_batch_results(batch_num, total_batches, sub_results)

            if ref_positions:
                # 整批都是参考图：跳过 r2/r3，逐条调 r4 有图模式
                return await self._process_ref_batch(
                    persona_name, batch_num, total_batches,
                    batch_styles, batch_scenes, batch_refs,
                    prompt_engineer_provider_id, prompt_engineer_system_prompt,
                    r4_scheduler, image_scheduler, persona, only_pid, persona_ref_count,
                )

            non_empty_refs = [d for d in batch_refs_desc if d]

            # 提取参考图 URI 供 r2/r3 多模态使用。
            # 与 non_empty_refs 严格对齐：任一有效描述对应的图片不可用，
            # 整体回退为 None（纯文本描述），避免图片与描述错位。
            ref_image_uris: list[str] | None = None
            if non_empty_refs:
                uris: list[str] = []
                aligned = True
                for desc, ref in zip(batch_refs_desc, batch_refs):
                    if not desc:
                        continue
                    if not ref:
                        aligned = False
                        break
                    img_path = ref.get("image_path", "")
                    if not img_path:
                        aligned = False
                        break
                    p = Path(img_path)
                    if not p.exists():
                        aligned = False
                        break
                    uris.append(p.as_uri())
                ref_image_uris = uris if (aligned and uris) else None

            logger.debug(
                "[DailySelfie] 人格 %s r2设计 批次 %d/%d：创意设计 %d 组",
                persona_name, batch_num, total_batches, len(batch_styles),
            )
            self._record_debug(
                "INFO",
                f"r2设计 批次 {batch_num}/{total_batches}：创意设计 {len(batch_styles)} 组",
            )

            # r2 设计（第1次带参考图，失败回退纯文本描述）
            designs = await self._llm_round2_design(
                designer_provider_id, batch_styles, batch_scenes,
                ref_descriptions=non_empty_refs if non_empty_refs else None,
                ref_images=ref_image_uris,
                system_prompt=costume_system_prompt,
            )

            # 设计失败后的延迟重试
            retry_attempt = 0
            while designs is None and retry_attempt < DESIGN_MAX_RETRY_ATTEMPTS:
                now_dt = datetime.now()
                estimated_start = now_dt + timedelta(seconds=DESIGN_RETRY_DELAY_SECONDS)
                deadline_dt = now_dt.replace(
                    hour=DESIGN_RETRY_DEADLINE_HOUR,
                    minute=DESIGN_RETRY_DEADLINE_MINUTE,
                    second=0,
                    microsecond=0,
                )
                if estimated_start >= deadline_dt:
                    logger.warning(
                        "[DailySelfie] r2 批次 %d/%d 创意设计失败，预计重试 %s 过截止线 %s，终止重试",
                        batch_num, total_batches,
                        estimated_start.strftime("%H:%M:%S"),
                        deadline_dt.strftime("%H:%M:%S"),
                    )
                    self._record_debug(
                        "WARN",
                        f"r2设计 批次 {batch_num}/{total_batches} 创意设计失败，"
                        f"预计延迟重试开始时间 {estimated_start.strftime('%H:%M:%S')} "
                        f"已到/过当日截止线 {deadline_dt.strftime('%H:%M:%S')}，终止重试",
                    )
                    break

                logger.warning(
                    "[DailySelfie] r2 批次 %d/%d 创意设计失败，%d 分钟后第 %d 次重试（预计 %s）",
                    batch_num, total_batches,
                    DESIGN_RETRY_DELAY_SECONDS // 60, retry_attempt + 1,
                    estimated_start.strftime("%H:%M:%S"),
                )
                self._record_debug(
                    "WARN",
                    f"r2设计 批次 {batch_num}/{total_batches} 创意设计失败，"
                    f"{DESIGN_RETRY_DELAY_SECONDS // 60} 分钟后进行第 {retry_attempt + 1} 次延迟重试"
                    f"（预计开始: {estimated_start.strftime('%H:%M:%S')}）",
                )
                await asyncio.sleep(DESIGN_RETRY_DELAY_SECONDS)
                retry_attempt += 1
                designs = await self._llm_round2_design(
                    designer_provider_id, batch_styles, batch_scenes,
                    ref_descriptions=non_empty_refs if non_empty_refs else None,
                    ref_images=ref_image_uris,
                    system_prompt=costume_system_prompt,
                )

            if designs is None:
                if retry_attempt > 0:
                    logger.warning(
                        "[DailySelfie] r2 批次 %d/%d 重试 %d 次仍失败，跳过",
                        batch_num, total_batches, retry_attempt,
                    )
                    self._record_debug(
                        "WARN",
                        f"r2设计 批次 {batch_num}/{total_batches} 创意设计在 {retry_attempt} 次延迟重试后仍失败，跳过",
                    )
                else:
                    logger.warning(
                        "[DailySelfie] r2 批次 %d/%d 创意设计失败，跳过",
                        batch_num, total_batches,
                    )
                    self._record_debug(
                        "WARN",
                        f"r2设计 批次 {batch_num}/{total_batches} 创意设计失败，跳过",
                    )
                return (0, 0, {}, [])

            # r3 审核（错开启动；审核基于文本设计方案，不看参考图）
            await self._stagger_start(r3_scheduler)
            designs = await self._llm_round3_review(
                reviewer_provider_id, batch_styles, batch_scenes, designs,
                ref_descriptions=non_empty_refs if non_empty_refs else None,
                system_prompt=reviewer_system_prompt,
            )

            # r4 翻译（错开启动；第1次带参考图，失败回退纯文本描述）
            await self._stagger_start(r4_scheduler)
            prompts = await self._llm_round4_prompt(
                designs, prompt_engineer_provider_id,
                ref_images=ref_image_uris,
                system_prompt=prompt_engineer_system_prompt,
                persona_ref_count=persona_ref_count,
            )
            logger.debug(
                "[DailySelfie] 人格 %s r4提示词 批次 %d/%d 返回 %d 条提示词",
                persona_name, batch_num, total_batches, len(prompts),
            )

            # 组装 (prompt, ref) 配对
            result_prompts: list[tuple[str, dict | None]] = []
            actual_count = min(len(prompts), len(batch_refs))
            for i in range(actual_count):
                ref = batch_refs[i] if i < len(batch_refs) else None
                result_prompts.append((prompts[i].strip() if prompts[i] else "", ref))

            # 画图（错开启动，跨批次共享 image_scheduler）
            success = 0
            fail = 0
            provider_success: dict[str, list[Path]] = {}
            failed_items: list[tuple[str, str, str, str]] = []

            image_tasks: list[asyncio.Task] = []
            task_prompts: list[tuple[str, dict | None, str]] = []

            for prompt, ref in result_prompts:
                if not prompt:
                    fail += 1
                    continue

                if ref is not None:
                    ref_image_path = ref.get("image_path", "")
                    ref_strength = ref.get("ref_strength", "style")
                    ref_user_tags = str(ref.get("user_tags", "") or "")
                    if not ref_image_path:
                        logger.warning(
                            "[DailySelfie] 人格 %s 提示词 %d ref_image_path 为空，改为纯文生图",
                            persona_name, len(image_tasks),
                        )
                        ref_image_path = ""
                        ref_strength = ""
                        ref_user_tags = ""
                else:
                    ref_image_path = ""
                    ref_strength = ""
                    ref_user_tags = ""

                selected_pid = await self._reserve_provider(persona, only_pid=only_pid)
                if selected_pid is None:
                    logger.debug("[DailySelfie] 人格 %s 所有提供商额度用完，停止", persona_name)
                    break

                logger.debug(
                    "[DailySelfie] 人格 %s 创建画图任务 %d: provider=%s ref=%s strength=%s",
                    persona_name, len(image_tasks), selected_pid,
                    ref_image_path[:50] if ref_image_path else "纯文生图",
                    ref_strength or "无",
                )

                await self._stagger_start(image_scheduler)
                t = asyncio.create_task(
                    self._generate_one_selfie(
                        persona_name, prompt, ref_image_path, ref_strength, persona,
                        provider_id=selected_pid, ref_user_tags=ref_user_tags,
                    )
                )
                image_tasks.append(t)
                task_prompts.append((prompt, ref, selected_pid))

            if image_tasks:
                img_results = await asyncio.gather(*image_tasks, return_exceptions=True)
                logger.debug(
                    "[DailySelfie] 人格 %s 批次 %d/%d 并发画图完成: tasks=%d results=%d",
                    persona_name, batch_num, total_batches, len(image_tasks), len(img_results),
                )
                self._record_debug(
                    "INFO",
                    f"批次 {batch_num}/{total_batches} 画图完成: tasks={len(image_tasks)} results={len(img_results)}",
                )

                for i, r in enumerate(img_results):
                    if isinstance(r, Path):
                        success += 1
                        if i < len(task_prompts):
                            _pid = task_prompts[i][2]
                            provider_success.setdefault(_pid, []).append(r)
                    else:
                        fail += 1
                        if isinstance(r, Exception):
                            logger.error("[DailySelfie] 人格 %s 生图任务 %d 异常: %s", persona_name, i, r)
                        else:
                            logger.warning("[DailySelfie] 人格 %s 生图任务 %d 返回 None", persona_name, i)
                        if i < len(task_prompts):
                            prompt_text, ref_info, _pid = task_prompts[i]
                            if _pid:
                                await self.counter.release(persona_name, _pid)
                            ref_path = ref_info.get("image_path", "") if ref_info else ""
                            ref_strength = ref_info.get("ref_strength", "style") if ref_info else ""
                            ref_user_tags = str(ref_info.get("user_tags", "") or "") if ref_info else ""
                            failed_items.append((prompt_text, ref_path, ref_strength, ref_user_tags))

            return (success, fail, provider_success, failed_items)

        except Exception as e:
            logger.error(
                "[DailySelfie] 人格 %s r2设计 批次 %d/%d 异常: %s",
                persona_name, batch_num, total_batches, e,
                exc_info=True,
            )
            self._record_debug(
                "ERROR",
                f"批次 {batch_num}/{total_batches} 异常: {e}",
            )
            return (0, 0, {}, [])

    async def _llm_round1_scene(
        self,
        chat_provider_id: str,
        count: int,
    ) -> list[str]:
        system_prompt = _ROUND2_SCENE_SYSTEM_PROMPT.format(count=count)
        user_prompt = _ROUND2_SCENE_USER_PROMPT.format(count=count)

        # 重试一次：超时/异常/返回空/返回条数不足均重试
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=chat_provider_id,
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                    ),
                    timeout=360,
                )
                self._report_llm_tokens(chat_provider_id, resp)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning("[DailySelfie] r1场景返回空文本(重试%d/2)", attempt + 1)
                    if attempt == 0:
                        logger.debug("[DailySelfie] r1场景返回空，重试一次")
                        continue
                    return []

                parsed = _parse_llm_lines(text, count)
                if len(parsed) < count and attempt == 0:
                    logger.warning(
                        "[DailySelfie] r1场景返回 %d 条（期望 %d 条），重试一次",
                        len(parsed), count,
                    )
                    continue
                if len(parsed) < count:
                    logger.debug(
                        "[DailySelfie] r1场景重试后仍返回 %d 条（期望 %d 条），按实际返回处理",
                        len(parsed), count,
                    )
                return parsed
            except asyncio.TimeoutError:
                logger.error("[DailySelfie] r1场景调用超时(360s)(重试%d/2)", attempt + 1)
                self._record_debug("ERROR", f"r1场景调用超时(360s)(重试{attempt + 1}/2)")
                if attempt == 0:
                    logger.debug("[DailySelfie] r1场景超时，重试一次")
                    continue
                return []
            except Exception as e:
                logger.error("[DailySelfie] r1场景调用失败(重试%d/2): %s", attempt + 1, e)
                if attempt == 0:
                    logger.debug("[DailySelfie] r1场景异常，重试一次")
                    continue
                return []
        return []

    async def _llm_round2_design(
        self,
        costume_provider_id: str,
        styles: list[str],
        scenes: list[str],
        ref_descriptions: list[str] | None = None,
        ref_images: list[str] | None = None,
        system_prompt: str = "",
    ) -> list[dict] | None:
        style_list = "\n".join(f"- {s}" for s in styles)
        scene_list = "\n".join(f"- {s}" for s in scenes)

        ref_text = ""
        if ref_descriptions:
            ref_text = "\n".join(ref_descriptions)

        user_prompt = _ROUND3_USER_PROMPT.format(
            style_list=style_list,
            scene_list=scene_list,
            ref_descriptions=ref_text,
            count=len(styles),
        )

        effective_prompt = system_prompt or _COSTUME_DESIGNER_SYSTEM_PROMPT

        for attempt in range(2):
            # 第1次带参考图（多模态），失败时第2次回退为纯文本描述
            current_images = ref_images if (ref_images and attempt == 0) else None
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=costume_provider_id,
                        prompt=user_prompt,
                        image_urls=current_images,
                        system_prompt=effective_prompt,
                    ),
                    timeout=600,
                )
                self._report_llm_tokens(costume_provider_id, resp)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] r2设计返回空文本(重试%d/2)%s",
                        attempt + 1,
                        "，回退为纯文本描述重试" if (ref_images and attempt == 0) else "",
                    )
                    continue

                designs = self._parse_costume_designer_json(text, len(styles))
                if designs is not None:
                    return designs
                logger.warning(
                    "[DailySelfie] r2设计 JSON 解析失败(重试%d/2)，原始文本: %s",
                    attempt + 1, text[:200],
                )
            except asyncio.TimeoutError:
                logger.warning("[DailySelfie] r2设计调用超时(重试%d/2)", attempt + 1)
                self._record_debug("WARN", f"r2设计调用超时(重试{attempt + 1}/2)")
            except Exception as e:
                logger.warning("[DailySelfie] r2设计调用失败(重试%d/2): %s", attempt + 1, e)

        return None

    async def _llm_round3_review(
        self,
        chat_provider_id: str,
        styles: list[str],
        scenes: list[str],
        designs: list[dict],
        ref_descriptions: list[str] | None = None,
        ref_images: list[str] | None = None,
        system_prompt: str = "",
    ) -> list[dict]:
        """r3: 审核师审核设计方案，可能返回改进版。

        对每套设计：
        - approved=true 或 improved_payload 为空 → 保留原设计
        - approved=false 且 improved_payload 存在 → 用改进版替换

        若整体审核调用失败，返回原 designs，不让流程中断。
        """
        if not designs:
            return designs

        input_data = []
        for i, design in enumerate(designs):
            style = styles[i] if i < len(styles) else ""
            scene = scenes[i] if i < len(scenes) else ""
            input_data.append({
                "style": style,
                "scene": scene,
                "design": design,
            })

        ref_section = ""
        if ref_descriptions:
            ref_section = (
                "\n\n参考图描述（用户希望设计方案忠实于参考图的服装款式，"
                "但姿势与构图可重新设计）：\n"
                + "\n".join(ref_descriptions)
            )

        user_prompt = (
            f"请审查以下 {len(input_data)} 套穿搭方案：\n\n"
            f"{json.dumps(input_data, ensure_ascii=False, indent=2)}"
            f"{ref_section}\n\n"
            f"返回 {len(input_data)} 个审核结果的 JSON 数组。"
        )

        effective_prompt = system_prompt or _COSTUME_REVIEWER_SYSTEM_PROMPT

        for attempt in range(2):
            # 第1次带参考图（多模态），失败时第2次回退为纯文本描述
            current_images = ref_images if (ref_images and attempt == 0) else None
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=chat_provider_id,
                        prompt=user_prompt,
                        image_urls=current_images,
                        system_prompt=effective_prompt,
                    ),
                    timeout=600,
                )
                self._report_llm_tokens(chat_provider_id, resp)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] r3审核返回空文本(重试%d/2)%s",
                        attempt + 1,
                        "，回退为纯文本描述重试" if (ref_images and attempt == 0) else "",
                    )
                    continue

                reviews = self._parse_reviewer_json(text, len(input_data))
                if reviews is not None:
                    return self._apply_reviews(designs, reviews)
                logger.warning(
                    "[DailySelfie] r3审核 JSON 解析失败(重试%d/2)，原始文本: %s",
                    attempt + 1, text[:200],
                )
            except asyncio.TimeoutError:
                logger.warning("[DailySelfie] r3审核调用超时(重试%d/2)", attempt + 1)
                self._record_debug("WARN", f"r3审核调用超时(重试{attempt + 1}/2)")
            except Exception as e:
                logger.warning("[DailySelfie] r3审核调用失败(重试%d/2): %s", attempt + 1, e)

        logger.warning("[DailySelfie] r3审核整体失败，返回原始设计方案")
        return designs

    @staticmethod
    def _parse_reviewer_json(text: str, expected_count: int) -> list[dict] | None:
        """解析审核师输出的 JSON 数组。"""
        text = text.strip()
        if text.startswith("```"):
            first_newline = text.index("\n") if "\n" in text else -1
            if first_newline >= 0:
                text = text[first_newline + 1:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if not isinstance(result, list):
            return None

        valid: list[dict] = []
        for item in result:
            if isinstance(item, dict):
                # 兼容 LLM 返回字符串布尔值（如 "false"）的情况
                # bool("false") 在 Python 中为 True，会导致审核结果反转，必须显式解析
                approved_raw = item.get("approved", True)
                if isinstance(approved_raw, str):
                    approved = approved_raw.strip().lower() not in ("false", "0", "no", "null", "none", "")
                else:
                    approved = bool(approved_raw)
                improved = item.get("improved_payload")
                issues = item.get("issues", []) or []
                if not isinstance(issues, list):
                    issues = [str(issues)] if issues else []
                valid.append({
                    "approved": approved,
                    "issues": issues,
                    "improved_payload": improved if isinstance(improved, dict) else None,
                })

        if len(valid) < expected_count:
            logger.warning("[DailySelfie] 审核返回 %d/%d 条", len(valid), expected_count)

        return valid if valid else None

    def _apply_reviews(self, designs: list[dict], reviews: list[dict]) -> list[dict]:
        """根据审核结果生成最终设计方案。"""
        final: list[dict] = []
        for i, design in enumerate(designs):
            review = reviews[i] if i < len(reviews) else None
            if not review:
                final.append(design)
                continue

            approved = review.get("approved", True)
            improved = review.get("improved_payload")
            issues = review.get("issues", [])

            if approved or not improved:
                if not approved and issues:
                    logger.debug(
                        "[DailySelfie] 设计 %d 审核未通过但无改进版，保留原设计。issues: %s",
                        i, issues,
                    )
                    self._record_debug("INFO", f"设计 {i} 审核未通过但无改进版，保留原设计")
                else:
                    logger.debug("[DailySelfie] 设计 %d 审核通过", i)
                    self._record_debug("INFO", f"设计 {i} 审核通过")
                final.append(design)
            else:
                logger.debug(
                    "[DailySelfie] 设计 %d 审核未通过，应用改进版。issues: %s",
                    i, issues,
                )
                self._record_debug("INFO", f"设计 {i} 审核未通过，应用改进版")
                # merge：审核师改进版可能只返回修改过的字段，未修改字段保留原设计。
                # 过滤掉 None 值，防止改进版中的 None 覆盖原设计的有效字段
                # （LLM 可能偏离指令返回部分字段为 null，会导致下游 f-string 渲染成 "None"）
                improved_clean = {k: v for k, v in improved.items() if v is not None}
                merged = {**design, **improved_clean} if isinstance(design, dict) else improved_clean
                final.append(merged)

        return final

    async def _llm_round4_prompt(
        self,
        designs: list[dict],
        chat_provider_id: str,
        ref_images: list[str] | None = None,
        system_prompt: str = "",
        persona_ref_count: int = 3,
    ) -> list[str]:
        designs_text = "\n".join(
            f"- 服装：{d.get('clothing', '')} | 外观：{d.get('appearance', '')} | 动作：{d.get('pose', '')} | 场景：{d.get('scene', '')}"
            for d in designs
        )
        user_prompt = _NO_REF_PROMPT_ENGINEER_USER_PROMPT.format(
            count=len(designs), designs=designs_text,
        )

        effective_prompt = system_prompt or _apply_persona_ref_count(
            _NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT, persona_ref_count
        )

        # 重试一次：超时/异常/返回空/返回条数不足均重试
        # 第1次带参考图（多模态），失败时第2次回退为纯文本描述
        expected = len(designs)
        for attempt in range(2):
            current_images = ref_images if (ref_images and attempt == 0) else None
            # 模式声明跟随本次实际发送的图片，而不是入参：
            # 回退为纯文本那一次必须改口为无衣橱模式，否则模型会去找并不存在的参考图
            current_prompt = (
                user_prompt if current_images
                else "【无衣橱参考图模式】本次未随附任何参考图。\n\n" + user_prompt
            )
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=chat_provider_id,
                        prompt=current_prompt,
                        image_urls=current_images,
                        system_prompt=effective_prompt,
                    ),
                    timeout=600,
                )
                self._report_llm_tokens(chat_provider_id, resp)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] r4提示词返回空文本(重试%d/2)%s",
                        attempt + 1,
                        "，回退为纯文本描述重试" if (ref_images and attempt == 0) else "",
                    )
                    if attempt == 0:
                        logger.debug("[DailySelfie] r4提示词返回空，重试一次")
                        continue
                    return []

                parsed = _parse_llm_lines(text, expected)
                if len(parsed) < expected and attempt == 0:
                    logger.warning(
                        "[DailySelfie] r4提示词返回 %d 条（期望 %d 条），重试一次",
                        len(parsed), expected,
                    )
                    continue
                if len(parsed) < expected:
                    logger.debug(
                        "[DailySelfie] r4提示词重试后仍返回 %d 条（期望 %d 条），按实际返回处理",
                        len(parsed), expected,
                    )
                return parsed
            except asyncio.TimeoutError:
                logger.error("[DailySelfie] r4提示词调用超时(600s)(重试%d/2)%s", attempt + 1, "，回退为纯文本描述重试" if (ref_images and attempt == 0) else "")
                self._record_debug("ERROR", f"r4提示词调用超时(600s)(重试{attempt + 1}/2)")
                if attempt == 0:
                    logger.debug("[DailySelfie] r4提示词超时，重试一次")
                    continue
                return []
            except Exception as e:
                logger.error("[DailySelfie] r4提示词调用失败(重试%d/2): %s", attempt + 1, e)
                if attempt == 0:
                    logger.debug("[DailySelfie] r4提示词异常，重试一次")
                    continue
                return []
        return []

    async def _llm_round4_prompt_with_ref(
        self,
        style: str,
        scene: str,
        ref_strength: str,
        chat_provider_id: str,
        ref_images: list[str] | None = None,
        system_prompt: str = "",
        persona_ref_count: int = 3,
    ) -> str:
        """有衣橱参考图时，r4 逐条构建提示词。

        衣橱图的引用序号 = 人设图张数 + 1，按 persona_ref_count 动态生成——
        人设图由 1 张改为 2 张时序号随之变成参考图3，与系统提示词保持一致。
        有图模式必须带图：拿不到可用图片就放弃该条，不生成没有参考依据的服装描述。
        """
        if not ref_images:
            logger.warning(
                "[DailySelfie] r4有图模式未收到可用参考图，跳过该条"
                "（避免凭空编写一套无人可依的服装描述）"
            )
            self._record_debug("WARN", "r4有图模式无可用参考图，跳过该条")
            return ""

        wardrobe_index = persona_ref_count + 1
        user_prompt = (
            f"【有衣橱参考图模式】随附图片即参考图{wardrobe_index}（衣橱参考图），"
            "请看图后构建1条引用式图像生成提示词，"
            f"参考图{wardrobe_index}中已有的维度一律用“保留参考图{wardrobe_index}的XX”表述：\n"
            f"风格：{style}\n"
            f"场景：{scene}\n"
            f"参考图力度：{ref_strength}\n"
            "（full=完全模仿姿势和构图，style=保留服装重新设计姿势，reimagine=保留服装重新设计姿势和构图；"
            "参考图确是 cosplay／角色扮演照时一律全保留，仅移除遮脸相关元素）"
        )

        effective_prompt = system_prompt or _apply_persona_ref_count(
            _NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT, persona_ref_count
        )

        for attempt in range(2):
            # 全程带图重试：丢掉图片重试等于没有可引用的对象，
            # 模型只能凭空编一套服装，产出与需求无关的画面
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=chat_provider_id,
                        prompt=user_prompt,
                        image_urls=ref_images,
                        system_prompt=effective_prompt,
                    ),
                    timeout=600,
                )
                self._report_llm_tokens(chat_provider_id, resp)
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] r4有图提示词返回空(重试%d/2)，带图重试",
                        attempt + 1,
                    )
                    if attempt == 0:
                        continue
                    return ""
                return text
            except asyncio.TimeoutError:
                logger.error(
                    "[DailySelfie] r4有图提示词超时(600s)(重试%d/2)，带图重试", attempt + 1,
                )
                self._record_debug("ERROR", f"r4有图提示词超时(600s)(重试{attempt + 1}/2)")
                if attempt == 0:
                    continue
                return ""
            except Exception as e:
                logger.error("[DailySelfie] r4有图提示词失败(重试%d/2): %s", attempt + 1, e)
                if attempt == 0:
                    continue
                return ""
        return ""

    async def _process_ref_batch(
        self,
        persona_name: str,
        batch_num: int,
        total_batches: int,
        batch_styles: list[str],
        batch_scenes: list[str],
        batch_refs: list[dict],
        prompt_engineer_provider_id: str,
        prompt_engineer_system_prompt: str,
        r4_scheduler: dict,
        image_scheduler: dict,
        persona: dict,
        only_pid: str,
        persona_ref_count: int = 3,
    ) -> tuple[int, int, dict[str, list[Path]], list[tuple[str, str, str, str]]]:
        """有衣橱参考图的组：跳过 r2/r3，逐条调 r4 有图模式 + 画图。

        入参可能是整批有图，也可能是混合批次里筛出的有图子集。
        persona_ref_count 决定衣橱图的引用序号（= 人设图张数 + 1）。
        """
        try:
            prompts: list[str] = []
            strengths: list[str] = []
            for i, (style, scene, ref) in enumerate(zip(batch_styles, batch_scenes, batch_refs)):
                ref_strength = "full" if style == "cosplay" else "reimagine"
                strengths.append(ref_strength)
                img_path = ref.get("image_path", "")
                p = Path(img_path) if img_path else None
                img_uri = p.as_uri() if (p and p.exists()) else None

                await self._stagger_start(r4_scheduler)
                prompt = await self._llm_round4_prompt_with_ref(
                    style, scene, ref_strength,
                    prompt_engineer_provider_id,
                    ref_images=[img_uri] if img_uri else None,
                    system_prompt=prompt_engineer_system_prompt,
                    persona_ref_count=persona_ref_count,
                )
                prompts.append(prompt)

            logger.debug(
                "[DailySelfie] 人格 %s r4有图 批次 %d/%d 返回 %d 条提示词",
                persona_name, batch_num, total_batches, len(prompts),
            )
            self._record_debug(
                "INFO",
                f"r4有图 批次 {batch_num}/{total_batches}：跳过r2/r3，返回 {len(prompts)} 条提示词",
            )

            # 组装 (prompt, ref, strength) 配对
            result_prompts: list[tuple[str, dict | None, str]] = []
            actual_count = min(len(prompts), len(batch_refs))
            for i in range(actual_count):
                ref = batch_refs[i] if i < len(batch_refs) else None
                strength = strengths[i] if i < len(strengths) else "reimagine"
                result_prompts.append((prompts[i].strip() if prompts[i] else "", ref, strength))

            # 画图
            success = 0
            fail = 0
            provider_success: dict[str, list[Path]] = {}
            failed_items: list[tuple[str, str, str, str]] = []

            image_tasks: list[asyncio.Task] = []
            task_prompts: list[tuple[str, dict | None, str, str]] = []

            for prompt, ref, ref_strength in result_prompts:
                if not prompt:
                    fail += 1
                    continue

                ref_image_path = ref.get("image_path", "") if ref else ""
                ref_user_tags = str(ref.get("user_tags", "") or "") if ref else ""
                if not ref_image_path:
                    ref_image_path = ""
                    ref_strength = ""
                    ref_user_tags = ""

                selected_pid = await self._reserve_provider(persona, only_pid=only_pid)
                if selected_pid is None:
                    logger.debug("[DailySelfie] 人格 %s 所有提供商额度用完，停止", persona_name)
                    break

                await self._stagger_start(image_scheduler)
                t = asyncio.create_task(
                    self._generate_one_selfie(
                        persona_name, prompt, ref_image_path, ref_strength, persona,
                        provider_id=selected_pid, ref_user_tags=ref_user_tags,
                    )
                )
                image_tasks.append(t)
                task_prompts.append((prompt, ref, selected_pid, ref_strength))

            if image_tasks:
                img_results = await asyncio.gather(*image_tasks, return_exceptions=True)
                logger.debug(
                    "[DailySelfie] 人格 %s r4有图 批次 %d/%d 画图完成: tasks=%d results=%d",
                    persona_name, batch_num, total_batches, len(image_tasks), len(img_results),
                )
                for i, r in enumerate(img_results):
                    if isinstance(r, Path):
                        success += 1
                        if i < len(task_prompts):
                            _pid = task_prompts[i][2]
                            provider_success.setdefault(_pid, []).append(r)
                    else:
                        fail += 1
                        if isinstance(r, Exception):
                            logger.error("[DailySelfie] 人格 %s 生图任务 %d 异常: %s", persona_name, i, r)
                        else:
                            logger.warning("[DailySelfie] 人格 %s 生图任务 %d 返回 None", persona_name, i)
                        if i < len(task_prompts):
                            prompt_text, ref_info, _pid, ref_strength = task_prompts[i]
                            if _pid:
                                await self.counter.release(persona_name, _pid)
                            ref_path = ref_info.get("image_path", "") if ref_info else ""
                            ref_user_tags = str(ref_info.get("user_tags", "") or "") if ref_info else ""
                            failed_items.append((prompt_text, ref_path, ref_strength, ref_user_tags))

            return (success, fail, provider_success, failed_items)
        except Exception as e:
            logger.error(
                "[DailySelfie] 人格 %s r4有图 批次 %d/%d 异常: %s",
                persona_name, batch_num, total_batches, e, exc_info=True,
            )
            return (0, 0, {}, [])

    async def _search_reference_images(
        self,
        queries: list[str],
        wardrobe: Any,
        persona_name: str = "",
        min_similarity: float | None = None,
        per_query_min_similarity: list[float | None] | None = None,
    ) -> list[dict]:
        used_ids: set[str] = set()
        results: list[dict | None] = [None] * len(queries)

        async def _search_one(idx: int, query: str) -> None:
            try:
                sim = min_similarity
                if per_query_min_similarity and idx < len(per_query_min_similarity):
                    sim = per_query_min_similarity[idx]
                if hasattr(wardrobe, "get_reference_image"):
                    ref = await wardrobe.get_reference_image(
                        query=query,
                        current_persona=persona_name,
                        min_similarity=sim,
                        daily_selfie_mode=True,
                    )
                    if ref:
                        img_id = str(ref.get("image_id", ""))
                        if img_id and img_id not in used_ids and img_id not in self._today_used_image_ids:
                            used_ids.add(img_id)
                            self._today_used_image_ids.add(img_id)
                            results[idx] = ref
                        elif img_id and img_id in self._today_used_image_ids:
                            logger.debug("[DailySelfie] 参考图今日已用，跳过: id=%s query=%s", img_id, query[:50])
            except Exception as e:
                logger.warning("[DailySelfie] 参考图搜索失败: query=%s error=%s", query[:50], e)

        await asyncio.gather(*[_search_one(i, q) for i, q in enumerate(queries)])
        return results

    async def _get_style_pool(self, wardrobe: Any, persona_name: str = "") -> list[str]:
        try:
            if persona_name and hasattr(wardrobe, "get_style_pool_for_persona"):
                persona_pool = await wardrobe.get_style_pool_for_persona(persona_name)
                if persona_pool:
                    logger.debug(
                        "[DailySelfie] 人格 %s 使用自定义风格池 (%d 项)",
                        persona_name, len(persona_pool),
                    )
                    return persona_pool
            if hasattr(wardrobe, "get_merged_pools"):
                pools = await wardrobe.get_merged_pools()
                return list(pools.get("style", []))
            return []
        except Exception as e:
            logger.warning("[DailySelfie] 获取风格池失败: %s", e)
            return []

    async def _get_recent_styles(self, wardrobe: Any) -> list[str]:
        try:
            db = getattr(wardrobe, "db", None)
            if not db:
                return []
            if not hasattr(db, "list_images_lightweight"):
                return []
            # 按日期级别比较，避免时刻偏差导致3天前当天的图片被过滤掉
            # （原实现用 datetime.now() 时刻 - 3 天，会少算一整天的图片）
            today_date = datetime.now().date()
            three_days_ago_date = today_date - timedelta(days=3)
            images = await db.list_images_lightweight(
                persona="", exclude_persona="",
                sort_by="created_at", limit=50,
            )
            styles: set[str] = set()
            for img in images:
                created_raw = str(img.get("created_at", "") or "")[:10]
                if created_raw:
                    try:
                        created_dt = datetime.strptime(created_raw, _DATE_FMT).date()
                        if created_dt < three_days_ago_date:
                            continue
                    except ValueError:
                        pass
                style_raw = img.get("style", "")
                if not style_raw:
                    continue
                try:
                    tags = json.loads(style_raw) if isinstance(style_raw, str) else style_raw
                except (json.JSONDecodeError, TypeError):
                    tags = [style_raw] if style_raw else []
                if isinstance(tags, str):
                    tags = [tags]
                elif isinstance(tags, dict):
                    tags = list(tags.values()) if tags.values() else list(tags.keys())
                if not isinstance(tags, list):
                    tags = [tags] if tags else []
                for t in tags:
                    t = str(t).strip()
                    if t:
                        styles.add(t)
            return list(styles)
        except Exception as e:
            logger.warning("[DailySelfie] 获取近期风格失败: %s", e)
            return []

    async def get_status(self) -> dict[str, Any]:
        personas = self._get_enabled_personas()
        status = {
            "date": self.counter.get_date(),
            "personas": [],
        }
        for p in personas:
            counts = await self.counter.get_all_counts(p["persona_name"])
            persona_status = {
                "persona_name": p["persona_name"],
                "providers": [],
            }
            for pv in p["providers"]:
                pid = pv["provider_id"]
                used = counts.get(pid, 0)
                limit = pv["daily_limit"]
                schedule_time = self._get_provider_schedule_time(p["persona_name"], pv)
                persona_status["providers"].append({
                    "provider_id": pid,
                    "used": used,
                    "limit": limit,
                    "remaining": max(0, limit - used),
                    "schedule_time": schedule_time,
                })
            status["personas"].append(persona_status)
        return status
