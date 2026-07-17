from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

_DATE_FMT = "%Y-%m-%d"

_NUMBER_PREFIX_RE = re.compile(r'^[\d]+[.、)\]】]\s*')
_BULLET_PREFIX_RE = re.compile(r'^[-•*]\s+')


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


_DAILY_SELFIE_REF_HINT = (
    "用户喜欢这张图片的服装款式，但希望姿势与构图完全重新设计。"
    "不要模仿图4（即本描述指向的图片）的构图和姿势。"
    "其中，前3张参考图（系统已内置）是你的人设图，"
    "要使用这张新的参考图，请在提示词中使用参考图4来引用该参考图，"
)


def _build_strength_hint(ref_strength: str) -> str:
    if ref_strength == "full":
        return (
            "完全模仿这张参考图的姿势、构图和氛围。"
            "请使用有图流程，以图4（即本描述指向的图片）为完整模仿对象，"
            "保留其全部视觉细节（不包括图4可能出现的人物面部特征细节，"
            "那不是你，你的人设参考图为前三张）。"
        )
    elif ref_strength == "reimagine":
        return (
            "用户喜欢这张图片的服装款式，但希望姿势与构图完全重新设计。"
            "请使用无图流程 C（衣橱图仅保留服装），仅提取服装描述，"
            "不要模仿图4（即本描述指向的图片）的构图和姿势。"
        )
    else:
        return (
            "用户喜欢这张图片的服装风格和整体氛围，但希望姿势和构图做适当调整。"
            "请使用有图流程，以图4（即本描述指向的图片）为模仿对象，"
            "保留其服装与氛围，微调姿势和构图。"
        )

_ROUND2_SCENE_SYSTEM_PROMPT = (
    "【场景概念生成任务】\n\n"
    "你是一位场景顾问，为生活照拍摄构思场景概念。\n\n"
    "核心任务：\n"
    "生成 {count} 个不同的场景概念。每个场景概念用简短的一句话描述（如\"午后的客厅\"\"清晨的咖啡馆\"\"傍晚的街道\"），不做详细展开，详细设计由后续环节负责。\n\n"
    "多样性要求（必须满足）：\n"
    "- 室内与户外场景尽量分散，避免全部集中在同一种空间类型\n"
    "- 以日常真实生活场景为主（卧室、客厅、厨房、咖啡馆、书店、街道、校园、公园等人们日常会去的地方），最多1个场景可为氛围感非典型地点（如天台、废弃建筑、雨夜小巷等）\n"
    "- 不同时间段尽量分散，避免全部集中在同一时段（如全部是白天或全部是夜晚）\n"
    "- 不同空间尺度尽量分散，避免全部是同类型空间（如全部是狭小室内或全部是开阔户外）\n\n"
    "约束：\n"
    "- 每条一行，不编号，不解释\n"
    "- 只输出场景概念本身，不输出任何类型标签或分类说明\n"
    "- 禁止调用aiimg_generate工具"
)

_ROUND2_SCENE_USER_PROMPT = (
    "请为写真拍摄构思 {count} 个不同的场景概念，需满足系统提示词中的多样性要求。每个场景用一句话简短描述。\n\n"
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

_NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT = (
    "你是一位精通图像生成提示词工程的专家，专长是将抽象的设计方案转化为高质量、高保真的图像生成提示词。你深谙图像生成模型对自然语言提示词的响应规律，知道如何用精准的视觉语言引导模型产出理想画面。\n\n"
    "## 核心任务\n\n"
    "将服装设计师提供的 JSON 设计方案逐条转化为图像生成提示词。每条提示词必须是一段连贯、流畅的中文视觉描述，将服装、外观造型、动作、场景深度融合为统一的画面叙事，而非简单拼接四个字段。\n\n"
    "## 最高优先级约束（覆盖一切其他规则）\n\n"
    "**面部必须完整露出。** 绝对不允许生成挡脸、遮脸、侧脸只露半脸、用手或物品遮挡面部的画面。没有任何例外。\n\n"
    "**必须留有刘海遮住额头。** 不允许生成露出大面积额头的画面。\n\n"
    "**不允许高马尾。** 不得在提示词中描述高马尾发型。\n\n"
    "**不允许佩戴眼镜。** 不得在提示词中描述任何类型的眼镜。\n\n"
    "## 参考图机制\n\n"
    "生图模型会收到三张人设参考图，角色的面部和身体身份特征已由参考图锁定。固定开头已包含身份保持指令和基本体型描述（白皙皮肤、纤细身姿与饱满曲线的对比），因此变量描述只需聚焦\"这次拍摄中她是什么状态\"——服装穿着状态、发型、身体姿态、场景氛围、以及服装对体型的响应。不要在变量部分重复描述角色的固有面部特征或基本体型，这些已由固定开头覆盖。\n\n"
    "## 提示词构建方法\n\n"
    "### 信息筛选与力度分配\n\n"
    "生图模型的注意力是有限的，不可能同时还原所有精细维度。提示词必须有所取舍：\n\n"
    "1. **识别视觉锚点**：每条方案都有1-2个最出彩的视觉特征，这些是画面的\"记忆点\"，必须给予最充分的描述。常见锚点类型：独特的穿着状态（开叉、透视、面料张力）、标志性的动作（持杯、撩发、倚靠姿态）、特殊的光线效果（逆光轮廓、聚光明暗）\n"
    "2. **锚点详写，其余点到**：视觉锚点充分展开描述；其余维度用最简表述覆盖即可，不需要每个细节都充分展开\n"
    "3. **敢于省略**：输入信息非常详细，但提示词不需要保留所有细节。对画面效果影响不大的信息（如被遮挡的内搭、远景中的小物件、配饰的精确尺寸）可以省略，把注意力让给核心元素\n\n"
    "### 位置权重\n\n"
    "图像生成模型对提示词前部的信息赋予更高权重，后部的信息容易被忽略。因此：\n"
    "- 视觉锚点的描述应尽早出现在变量部分的开头区域\n"
    "- 次要的环境和氛围信息自然收尾\n"
    "- 不要把最重要的视觉特征埋在提示词中后段\n\n"
    "### 视觉维度覆盖\n\n"
    "构建提示词时，以下维度都必须被触及（哪怕只用一个短语），但描述力度严格遵循上述\"信息筛选与力度分配\"原则。当某维度非焦点时，可使用括号内的最简表述快速覆盖：\n\n"
    "- **服装与穿着状态**：画面中可见的服装结构、层次、材质质感，以及服装在身体上的实际状态——如何被撑起、贴合、悬垂、褶皱。当服装穿着状态为视觉锚点时，重点关注服装对体型的响应（面料在胸前被撑起的张力、腰臀处贴合的轮廓、裙摆因曲线形成的褶皱）；非锚点时只需简述穿着状态即可。固定开头已描述基本体型，此处聚焦服装如何响应体型，而非重复描述体型本身（最简表述：\"身着[款式+色彩+材质]的[服装名]\"）\n"
    "- **外观造型**：发型（造型、长度、状态）。发型对画面视觉冲击力很大，应自然融入人物描述中，通常应被提及。指甲油信息若出现，用最简表述带过即可（如\"裸粉色甲油\"），无需展开（最简表述：\"[发型描述]\"，如\"黑色长发披散在肩上\"）\n"
    "- **姿态**：完整的身体姿态、手部位置、头部朝向、眼神方向、表情。其中身体朝向、头部角度、手部位置必须给出，但非焦点时只需简单定位（如\"双手自然垂于身侧\"）；眼神方向和表情在非焦点时可用最简表述（最简表述：\"面朝镜头，微笑\"或\"侧身而立，目光投向[方向]\"）\n"
    "- **空间与氛围**：人物在场景中的位置、与环境的互动关系、光线方向与质感、环境色调。景别必须与设计方案的景别意图一致——若方案侧重面部表情与上半身，构建近景或中近景；若方案侧重全身姿态与服装，构建中景或全景。根据每条方案的视觉焦点选择最合适的景别（最简表述：\"在[场景]中，[光线]\"）\n\n"
    "### 叙事流畅性\n\n"
    "提示词应是一段自然的画面描写，而非维度清单的拼接。**叙事的起点就是视觉锚点**——锚点是什么，就从什么开始写。以下是三种可参考的叙事模式：\n\n"
    "1. **人物中心外扩式**：从人物核心状态（服装+姿态）出发，沿视线或动作方向自然延伸到环境。适用于人物为绝对主体的方案。例：\"身着…，侧身倚靠…，目光投向…，身后是…\"\n"
    "2. **场景锚定式**：先用一句场景氛围定调，再引入人物在场景中的状态。适用于场景氛围感强的方案。例：\"暮色中的天台上，她身着…，…\"\n"
    "3. **动作线索串联式**：以一个关键动作为线索，串联服装状态和场景互动。适用于动作感强的方案。例：\"指尖轻捏裙摆边缘，奶白色A字裙随之微微展开，她站在…\"\n\n"
    "选择哪种模式取决于方案的视觉焦点——不要机械套用，让叙事自然服务于画面。\n\n"
    "## 常见生成失败预防\n\n"
    "图像生成模型容易产生以下问题，请在提示词中主动规避：\n\n"
    "- **多人出现**：始终使用单人表述，避免\"她们\"\"人们\"等复数词\n"
    "- **风格偏移**：坚持写实基调，避免\"插画感\"\"海报风\"\"动漫\"等词汇\n"
    "- **手部畸形**：手部描述采取分层策略——手部涉及关键动作（持物、触碰面部、互动）时，必须具体到手指动作和相对位置（如\"右手食指轻抵下唇\"）；手部非焦点时，简单明确地定位即可（如\"双手自然垂于身侧\"\"左手轻搭栏杆\"），避免过度聚焦手指细节反而引发畸形\n"
    "- **文字水印**：不要描述任何文字、标识、水印、Logo\n"
    "- **肢体冗余**：始终明确两只手的位置和动作，避免模糊描述导致多出手臂\n"
    "- **面部遮挡**：避免描述容易导致面部被遮挡的姿态（如\"低头\"\"用手托腮\"\"头发遮住半脸\"），即使意图不是遮挡，生图模型也可能按字面理解生成遮挡画面。使用\"下巴微抬\"\"面部正对镜头\"等明确露出面部的表述\n"
    "- **额头裸露**：避免描述无刘海的发型（如\"大光明\"\"全部后梳\"），生图模型可能按字面生成露额头画面。所有发型描述必须包含刘海覆盖前额的表述\n"
    "- **眼镜出现**：不得在提示词中提及眼镜、墨镜等任何眼部饰品，即使设计方案中未明确禁止也要主动规避\n"
    "- **高马尾**：不得在提示词中描述高马尾发型，改用低马尾、散发、编发等替代\n\n"
    "## 硬性规则\n\n"
    "1. **输出格式**：每条设计方案对应一段完整的提示词，只输出提示词本身，不输出分析、编号、分点或规则解释\n"
    "2. **只描述可见内容**：只描述镜头可以直接捕捉到的视觉信息，不写声音、气味、触感、情绪标签等不可见内容；只描述画面中能看到的服装结构与层次，被完全遮挡的部分不写\n"
    "3. **动作完整性**：姿态描述必须给出足够信息让生图模型理解人物的整体姿态。身体朝向、头部角度、手部位置必须给出，但非焦点时只需简单定位（如\"双手自然垂于身侧\"）；眼神方向和表情在非焦点时可用最简表述（如\"看向镜头\"\"微笑\"），但不可完全缺失\n"
    "4. **物理可行性**：所有姿势必须符合人体工学，人物只有两只手和两条腿，不能出现物理矛盾\n"
    "5. **光影自然**：光线描述应基于物理光源（阳光、灯光、反射光等），避免抽象的光影形容词堆砌\n"
    "6. **中文输出，无文字元素**：提示词必须使用中文；不得描述任何文字、标识、水印或象征性符号\n"
    "7. **年龄一致性**：人物的视觉年龄应符合少女设定\n"
    "8. **魅力呈现方式**：通过服装的穿着状态和服装对体型的响应自然呈现角色魅力。可以描述身体曲线在服装下的视觉呈现，但禁止色情化地聚焦敏感部位的特写描述\n"
    "9. **固定首尾**：每条提示词以\"以参考图中这位少女为基准，完整保留其五官、身材等全部人体身份特征，绝对禁止任何拼图，为她本人生成一张新的写真：她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，\"开头，以\"完全保留少女的面部特征与丰满的身材。\"结尾\n"
    "10. **提示词长度**：建议控制在180-300字之间（含固定首尾约100字，变量部分约80-200字），信息贵精不贵多，过载反而分散生图模型注意力\n\n"
    "## 输入\n\n"
    "你会收到一个 JSON 数组，每个元素包含：\n"
    "- clothing：服装设计描述（含款式、材质、色彩、层次、穿着状态、袜类、鞋类、配饰等详细信息）\n"
    "- appearance：外观造型描述（含发型造型/长度/颜色/状态，以及可选的指甲油等）\n"
    "- pose：动作姿势描述（含身体姿态、四肢位置、手部细节、头部朝向、眼神方向、表情等）\n"
    "- scene：场景环境描述（含具体地点、环境细节、光线氛围、道具、色调、时间段与季节等）\n\n"
    "保留最关键的视觉细节，用自然的语序和节奏重新组织。对画面效果影响大的核心元素充分描述，次要信息简洁带过或省略——见\"信息筛选与力度分配\"。"
)

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
                logger.debug("[DailySelfie] 距离下次执行: %.0f秒", wait_seconds)
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
        logger.info("[DailySelfie] 触发补画: %s", ", ".join(
            f"{p['persona_name']}({', '.join(v['provider_id'] for v in p['providers'])})"
            for p in merged
        ))
        await self._run_personas(merged)

    def _get_enabled_personas(self) -> list[dict[str, Any]]:
        personas = []
        for idx in [1, 2, 3]:
            conf = self.plugin._get_selfie_persona_config(idx)
            if not conf:
                logger.debug("[DailySelfie] selfie_persona_%d 无配置，跳过", idx)
                continue
            if not self.plugin._as_bool(conf.get("daily_selfie_enabled", False), default=False):
                logger.debug("[DailySelfie] selfie_persona_%d daily_selfie_enabled=false，跳过", idx)
                continue

            providers = self._parse_providers_from_conf(conf, idx)

            if not providers:
                logger.debug("[DailySelfie] selfie_persona_%d 无有效提供商，跳过", idx)
                continue

            persona_name = str(conf.get("select_persona", "") or conf.get("persona_name", "")).strip()
            if not persona_name or persona_name == "default":
                logger.debug("[DailySelfie] selfie_persona_%d select_persona 为空或 default，跳过", idx)
                continue

            logger.info(
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
                logger.info(
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
            logger.info("[DailySelfie] 没有启用补画的人格，跳过")
            return

        if persona_name:
            personas = [p for p in personas if p["persona_name"] == persona_name]
            if not personas:
                logger.info("[DailySelfie] 人格 %s 未启用补画，跳过", persona_name)
                return

        await self._run_personas(personas, umo)

    async def _run_personas(self, personas: list[dict], umo: str = ""):
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
                self._execute_daily_selfie([p], wardrobe, umo)
            )
            self._selfie_tasks[pname] = task
            task.add_done_callback(lambda t, n=pname: self._selfie_tasks.pop(n, None))
            launched.append(pname)

        if launched:
            logger.info("[DailySelfie] 已启动补画任务: %s", ", ".join(launched))

    async def _execute_daily_selfie(self, personas: list[dict], wardrobe: Any, umo: str = ""):
        total_success = 0
        total_fail = 0
        request_interval = 30

        debug_mode = self._is_debug()
        selfie_conf = self.plugin._get_feature("selfie")
        logger.info(
            "[DailySelfie] 补画开始: 人格数=%d debug=%s selfie_conf_keys=%s",
            len(personas), debug_mode, list(selfie_conf.keys()),
        )

        try:
            recent_styles = await self._get_recent_styles(wardrobe)

            for p in personas:
                total_remaining = 0
                for pv in p["providers"]:
                    total_remaining += await self.counter.get_remaining(p["persona_name"], pv["provider_id"], pv["daily_limit"])
                if total_remaining <= 0:
                    logger.info("[DailySelfie] 人格 %s 所有提供商额度已用完，跳过", p["persona_name"])
                    continue

                style_pool = await self._get_style_pool(wardrobe, p["persona_name"])

                s, f = await self._process_persona_selfie(
                    p, wardrobe, style_pool, recent_styles, total_remaining, request_interval, umo
                )
                total_success += s
                total_fail += f

        except asyncio.CancelledError:
            logger.info("[DailySelfie] 补画任务被取消")
        except Exception as e:
            logger.error("[DailySelfie] 补画任务异常: %s", e)
        finally:
            logger.info(
                "[DailySelfie] 补画完成: 成功=%d 失败=%d",
                total_success, total_fail,
            )

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

    def _get_prompt_engineer_system_prompt(self, persona: dict) -> str:
        """读取人格级提示词构建系统提示词，留空则回退到内置默认常量。"""
        persona_conf = persona.get("config", {})
        configured = str(persona_conf.get("prompt_engineer_system_prompt", "") or "").strip()
        if configured:
            return configured
        return _NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT

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
            logger.warning(
                "[DailySelfie] 创意设计师返回 %d 条设计，期望 %d 条",
                len(valid), expected_count,
            )

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
    ) -> tuple[int, int]:
        persona_name = persona["persona_name"]
        success = 0
        fail = 0

        logger.info("[DailySelfie] 开始处理人格 %s，总剩余额度 %d（%d个提供商）", persona_name, remaining, len(persona["providers"]))

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
            logger.warning("[DailySelfie] 人格 %s 第1轮(场景)未返回结果", persona_name)
            return 0, 0

        pair_count = min(len(styles), len(scenes))
        styles = styles[:pair_count]
        scenes = scenes[:pair_count]

        logger.info(
            "[DailySelfie] 人格 %s r0算法选风格返回 %d 条，第1轮返回 %d 条场景，配对 %d 组",
            persona_name, len(styles), len(scenes), pair_count,
        )

        search_queries = [f"{s} {c}" for s, c in zip(styles, scenes)]

        selfie_conf = self.plugin._get_feature("selfie")
        daily_ref_min_sim_raw = float(selfie_conf.get("daily_selfie_ref_min_similarity", 0) or 0)
        daily_ref_min_sim = daily_ref_min_sim_raw if daily_ref_min_sim_raw > 0 else None
        if daily_ref_min_sim is not None:
            logger.info("[DailySelfie] 人格 %s 补拍搜图阈值: %s", persona_name, daily_ref_min_sim)

        ref_results = await self._search_reference_images(search_queries, wardrobe, persona_name, min_similarity=daily_ref_min_sim)

        ref_by_pair: dict[int, dict] = {}
        for i, ref in enumerate(ref_results):
            if ref is not None and i < pair_count:
                ref_by_pair[i] = ref

        logger.info("[DailySelfie] 人格 %s 搜图完成，找到 %d 张参考图（共 %d 组配对）", persona_name, len([r for r in ref_results if r is not None]), pair_count)

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
                        f"参考图{search_ref_index}描述：{desc}\n\n{_DAILY_SELFIE_REF_HINT}\n\n"
                        f"这张参考图的序号为{search_ref_index}，请在提示词中使用序号{search_ref_index}来引用该参考图。"
                    )
                else:
                    ref_descriptions.append("")
                ref_by_index.append(ref)
            else:
                ref_descriptions.append("")
                ref_by_index.append(None)

        costume_system_prompt = self._get_costume_designer_system_prompt(persona)
        prompt_engineer_system_prompt = self._get_prompt_engineer_system_prompt(persona)
        reviewer_system_prompt = self._get_reviewer_system_prompt(persona)

        batch_size = 3
        all_designs: list[dict] = []
        all_ref_by_design: list[dict | None] = []

        total_batches = (pair_count + batch_size - 1) // batch_size

        for batch_num, batch_start in enumerate(range(0, pair_count, batch_size), 1):
            batch_styles = styles[batch_start:batch_start + batch_size]
            batch_scenes = scenes[batch_start:batch_start + batch_size]
            batch_refs_desc = ref_descriptions[batch_start:batch_start + batch_size]
            batch_refs = ref_by_index[batch_start:batch_start + batch_size]

            non_empty_refs = [d for d in batch_refs_desc if d]

            logger.info(
                "[DailySelfie] 人格 %s 第2轮批次 %d/%d：创意设计 %d 组",
                persona_name, batch_num, total_batches, len(batch_styles),
            )

            designs = await self._llm_round2_design(
                designer_provider_id, batch_styles, batch_scenes,
                ref_descriptions=non_empty_refs if non_empty_refs else None,
                system_prompt=costume_system_prompt,
            )

            if designs is None:
                logger.warning(
                    "[DailySelfie] 人格 %s 第2轮批次 %d/%d 创意设计失败，跳过",
                    persona_name, batch_num, total_batches,
                )
                continue

            # r3: 审核环节，对设计师输出做美学审核并可能给出改进版
            designs = await self._llm_round3_review(
                reviewer_provider_id, batch_styles, batch_scenes, designs,
                system_prompt=reviewer_system_prompt,
            )

            actual_count = min(len(designs), len(batch_styles))
            for i in range(actual_count):
                all_designs.append(designs[i])
                all_ref_by_design.append(batch_refs[i] if i < len(batch_refs) else None)

        if not all_designs:
            logger.warning("[DailySelfie] 人格 %s 未生成任何设计方案，%d 个额度全部计入失败", persona_name, remaining)
            return 0, remaining

        all_prompts: list[tuple[str, dict | None]] = []

        design_batch_size = 3
        design_total_batches = (len(all_designs) + design_batch_size - 1) // design_batch_size

        for batch_num, batch_start in enumerate(range(0, len(all_designs), design_batch_size), 1):
            batch_designs = all_designs[batch_start:batch_start + design_batch_size]
            batch_refs = all_ref_by_design[batch_start:batch_start + design_batch_size]

            prompts = await self._llm_round4_prompt(batch_designs, prompt_engineer_provider_id, system_prompt=prompt_engineer_system_prompt)
            logger.info(
                "[DailySelfie] 人格 %s 第4轮批次 %d/%d 返回 %d 条提示词",
                persona_name, batch_num, design_total_batches, len(prompts),
            )

            for i, prompt in enumerate(prompts):
                ref = batch_refs[i] if i < len(batch_refs) else None
                all_prompts.append((prompt.strip(), ref))

        if not all_prompts:
            logger.warning("[DailySelfie] 人格 %s 未生成任何提示词，%d 个额度全部计入失败", persona_name, remaining)
            return 0, remaining

        logger.info("[DailySelfie] 人格 %s 生成 %d 条提示词，开始并发画图", persona_name, len(all_prompts))

        tasks: list[asyncio.Task] = []
        task_prompts: list[tuple[str, dict | None, str]] = []

        for prompt, ref in all_prompts:
            if not prompt:
                fail += 1
                continue

            if ref is not None:
                ref_image_path = ref.get("image_path", "")
                ref_strength = ref.get("ref_strength", "style")
                if not ref_image_path:
                    logger.warning("[DailySelfie] 人格 %s 提示词 %d ref_image_path 为空，改为纯文生图", persona_name, len(tasks))
                    ref_image_path = ""
                    ref_strength = ""
            else:
                ref_image_path = ""
                ref_strength = ""

            selected_pid = await self._reserve_provider(persona)
            if selected_pid is None:
                logger.info("[DailySelfie] 人格 %s 所有提供商额度用完，停止", persona_name)
                break

            logger.info("[DailySelfie] 人格 %s 创建画图任务 %d: provider=%s ref=%s strength=%s", persona_name, len(tasks), selected_pid, ref_image_path[:50] if ref_image_path else "纯文生图", ref_strength or "无")

            t = asyncio.create_task(
                self._generate_one_selfie(
                    persona_name, prompt, ref_image_path, ref_strength, persona,
                    provider_id=selected_pid,
                )
            )
            tasks.append(t)
            task_prompts.append((prompt, ref, selected_pid))
            await asyncio.sleep(request_interval)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[DailySelfie] 人格 %s 并发画图完成: tasks=%d results=%d", persona_name, len(tasks), len(results))

        failed_items: list[tuple[str, str, str]] = []
        provider_success: dict[str, list[Path]] = {}

        for i, r in enumerate(results):
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
                    failed_items.append((prompt_text, ref_path, ref_strength))

        if failed_items:
            retry_enabled = self._is_retry_on_fail()
            logger.info(
                "[DailySelfie] 人格 %s 失败 %d 张，重试开关=%s",
                persona_name, len(failed_items), retry_enabled,
            )

            if retry_enabled:
                for prompt_text, ref_path, ref_strength in failed_items:
                    selected_pid = await self._reserve_provider(persona)
                    if selected_pid is None:
                        logger.info("[DailySelfie] 人格 %s 重试时所有提供商额度用完，停止", persona_name)
                        break

                    logger.info(
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
                            logger.info("[DailySelfie] 人格 %s 重试成功: %s provider=%s", persona_name, image_path, selected_pid)
                            await self._save_to_wardrobe(image_path, persona_name)
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
                logger.info("[DailySelfie] 人格 %s 提供商 %s 完成 %d 张，发布空间", persona_name, pid, len(paths))
                await self._publish_to_qzone(persona_name, paths, persona["config"])

        return success, fail

    async def _reserve_provider(self, persona: dict) -> str | None:
        pname = persona["persona_name"]
        for pv in persona["providers"]:
            pid = pv["provider_id"]
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
    ) -> Path | None:
        logger.info("[DailySelfie] 人格 %s 开始画图: provider=%s ref=%s prompt_len=%d", persona_name, provider_id, ref_image_path[:50] if ref_image_path else "空", len(prompt))
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
                await self._save_to_wardrobe(image_path, persona_name)
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

    async def _save_to_wardrobe(self, image_path: Path, persona_name: str) -> None:
        wardrobe = self.plugin._get_wardrobe_instance()
        if not wardrobe or not hasattr(wardrobe, "_save_image_from_bytes"):
            return
        try:
            import aiofiles
            async with aiofiles.open(image_path, "rb") as f:
                image_bytes = await f.read()
            if not image_bytes:
                return
            image_id, attrs, duplicate = await wardrobe._save_image_from_bytes(
                image_bytes, persona=persona_name, created_by="daily_selfie",
            )
            if duplicate:
                logger.debug("[DailySelfie] 补画图片已存在于衣橱，跳过: %s", image_id)
            elif image_id:
                logger.info("[DailySelfie] 补画图片已保存到衣橱: %s", image_id)
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
            logger.info(
                "[DailySelfie] 人格 %s 未启用空间发布或未配置多模态提供商，跳过",
                persona_name,
            )
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
                    logger.info(
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
                text = (getattr(resp, "completion_text", "") or "").strip()
                if text:
                    logger.info(
                        "[DailySelfie] 人格 %s 生成空间配文成功: %s",
                        persona_name,
                        text[:50],
                    )
                    result_text = text
                    break
            except asyncio.TimeoutError:
                logger.warning(
                    "[DailySelfie] 人格 %s 生成空间配文超时(第%d次)", persona_name, attempt + 1
                )
                if attempt == 0:
                    logger.info("[DailySelfie] 人格 %s 将重试一次", persona_name)
                    continue
            except Exception as e:
                logger.warning(
                    "[DailySelfie] 人格 %s 生成空间配文失败(第%d次): %s", persona_name, attempt + 1, e
                )
                if attempt == 0:
                    logger.info("[DailySelfie] 人格 %s 将重试一次", persona_name)
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
            logger.info(
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
            logger.info(
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

    async def _select_styles_by_algorithm(
        self,
        count: int,
        style_pool: list[str],
        recent_styles: list[str],
    ) -> list[str]:
        """r0: 算法选择风格（近期去重+等概率随机）。

        策略：
        1. 从风格池中过滤掉近期已拍过的风格，得"新鲜池"
        2. 若新鲜池 >= count，从新鲜池中等概率随机抽 count 个
           - 当前实现为等概率抽样（random.sample），未来可扩展为按"上次拍摄时间"加权
        3. 若新鲜池 < count 但 >=1，从新鲜池全取，不足部分从近期池中补足
        4. 若新鲜池为空，从全部风格池中随机抽 count 个（允许与近期重复）
        """
        if not style_pool or count <= 0:
            return []

        recent_set = set(recent_styles)
        fresh_pool = [s for s in style_pool if s not in recent_set]
        recent_pool = [s for s in style_pool if s in recent_set]

        if fresh_pool and len(fresh_pool) >= count:
            picked = self._random_sample(fresh_pool, count)
        elif fresh_pool:
            picked = list(fresh_pool)
            need = count - len(picked)
            if need > 0 and recent_pool:
                picked.extend(self._random_sample(recent_pool, min(need, len(recent_pool))))
        else:
            picked = self._random_sample(style_pool, min(count, len(style_pool)))

        picked = picked[:count]
        logger.info(
            "[DailySelfie] r0算法选风格: pool=%d recent=%d fresh=%d picked=%s",
            len(style_pool), len(recent_set), len(fresh_pool), picked,
        )
        return picked

    @staticmethod
    def _random_sample(pool: list[str], k: int) -> list[str]:
        """从 pool 中等概率随机抽取 k 个不重复元素。"""
        if not pool or k <= 0:
            return []
        k = min(k, len(pool))
        try:
            return random.sample(pool, k)
        except Exception as e:
            logger.warning("[DailySelfie] 随机抽样失败，回退: %s", e)
            return random.sample(pool, min(k, len(pool)))

    async def _llm_round1_scene(
        self,
        chat_provider_id: str,
        count: int,
    ) -> list[str]:
        system_prompt = _ROUND2_SCENE_SYSTEM_PROMPT.format(count=count)
        user_prompt = _ROUND2_SCENE_USER_PROMPT.format(count=count)

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round1-Scene] chat_provider_id=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                chat_provider_id, system_prompt, user_prompt,
            )

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
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning("[DailySelfie] LLM第1轮(场景)返回空文本(第%d次)", attempt + 1)
                    if attempt == 0:
                        logger.info("[DailySelfie] LLM第1轮(场景)返回空，重试一次")
                        continue
                    return []

                if self._is_debug():
                    logger.info(
                        "[DailySelfie][DEBUG][Round1-Scene] === LLM response ===\n%s",
                        text,
                    )

                parsed = _parse_llm_lines(text, count)
                if len(parsed) < count and attempt == 0:
                    logger.warning(
                        "[DailySelfie] LLM第1轮(场景)返回 %d 条（期望 %d 条），重试一次",
                        len(parsed), count,
                    )
                    continue
                if len(parsed) < count:
                    logger.warning(
                        "[DailySelfie] LLM第1轮(场景)重试后仍返回 %d 条（期望 %d 条），按实际返回处理",
                        len(parsed), count,
                    )
                return parsed
            except asyncio.TimeoutError:
                logger.error("[DailySelfie] LLM第1轮(场景)调用超时(360s)(第%d次)", attempt + 1)
                if attempt == 0:
                    logger.info("[DailySelfie] LLM第1轮(场景)超时，重试一次")
                    continue
                return []
            except Exception as e:
                logger.error("[DailySelfie] LLM第1轮(场景)调用失败(第%d次): %s", attempt + 1, e)
                if attempt == 0:
                    logger.info("[DailySelfie] LLM第1轮(场景)异常，重试一次")
                    continue
                return []
        return []

    async def _llm_round2_design(
        self,
        costume_provider_id: str,
        styles: list[str],
        scenes: list[str],
        ref_descriptions: list[str] | None = None,
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

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round2-Design] provider=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                costume_provider_id, effective_prompt, user_prompt,
            )

        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=costume_provider_id,
                        prompt=user_prompt,
                        system_prompt=effective_prompt,
                    ),
                    timeout=360,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] 第2轮(创意设计)返回空文本(第%d次)",
                        attempt + 1,
                    )
                    continue

                if self._is_debug():
                    logger.info(
                        "[DailySelfie][DEBUG][Round2-Design] === response ===\n%s",
                        text,
                    )

                designs = self._parse_costume_designer_json(text, len(styles))
                if designs is not None:
                    return designs
                logger.warning(
                    "[DailySelfie] 第2轮(创意设计) JSON 解析失败(第%d次)，原始文本: %s",
                    attempt + 1, text[:200],
                )
            except asyncio.TimeoutError:
                logger.warning("[DailySelfie] 第2轮(创意设计)调用超时(第%d次)", attempt + 1)
            except Exception as e:
                logger.warning("[DailySelfie] 第2轮(创意设计)调用失败(第%d次): %s", attempt + 1, e)

        return None

    async def _llm_round3_review(
        self,
        chat_provider_id: str,
        styles: list[str],
        scenes: list[str],
        designs: list[dict],
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

        user_prompt = (
            f"请审查以下 {len(input_data)} 套穿搭方案：\n\n"
            f"{json.dumps(input_data, ensure_ascii=False, indent=2)}\n\n"
            f"返回 {len(input_data)} 个审核结果的 JSON 数组。"
        )

        effective_prompt = system_prompt or _COSTUME_REVIEWER_SYSTEM_PROMPT

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round3-Review] provider=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                chat_provider_id, effective_prompt, user_prompt,
            )

        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=chat_provider_id,
                        prompt=user_prompt,
                        system_prompt=effective_prompt,
                    ),
                    timeout=360,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] 第3轮(审核)返回空文本(第%d次)",
                        attempt + 1,
                    )
                    continue

                if self._is_debug():
                    logger.info(
                        "[DailySelfie][DEBUG][Round3-Review] === response ===\n%s",
                        text,
                    )

                reviews = self._parse_reviewer_json(text, len(input_data))
                if reviews is not None:
                    return self._apply_reviews(designs, reviews)
                logger.warning(
                    "[DailySelfie] 第3轮(审核) JSON 解析失败(第%d次)，原始文本: %s",
                    attempt + 1, text[:200],
                )
            except asyncio.TimeoutError:
                logger.warning("[DailySelfie] 第3轮(审核)调用超时(第%d次)", attempt + 1)
            except Exception as e:
                logger.warning("[DailySelfie] 第3轮(审核)调用失败(第%d次): %s", attempt + 1, e)

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
            logger.warning(
                "[DailySelfie] 审核师返回 %d 条结果，期望 %d 条",
                len(valid), expected_count,
            )

        return valid if valid else None

    @staticmethod
    def _apply_reviews(designs: list[dict], reviews: list[dict]) -> list[dict]:
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
                    logger.info(
                        "[DailySelfie] 设计 %d 审核未通过但无改进版，保留原设计。issues: %s",
                        i, issues,
                    )
                else:
                    logger.info("[DailySelfie] 设计 %d 审核通过", i)
                final.append(design)
            else:
                logger.info(
                    "[DailySelfie] 设计 %d 审核未通过，应用改进版。issues: %s",
                    i, issues,
                )
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
        system_prompt: str = "",
    ) -> list[str]:
        designs_text = "\n".join(
            f"- 服装：{d.get('clothing', '')} | 外观：{d.get('appearance', '')} | 动作：{d.get('pose', '')} | 场景：{d.get('scene', '')}"
            for d in designs
        )
        user_prompt = _NO_REF_PROMPT_ENGINEER_USER_PROMPT.format(
            count=len(designs), designs=designs_text,
        )

        effective_prompt = system_prompt or _NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round4-Prompt] provider=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                chat_provider_id, effective_prompt, user_prompt,
            )

        # 重试一次：超时/异常/返回空/返回条数不足均重试
        expected = len(designs)
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=chat_provider_id,
                        prompt=user_prompt,
                        system_prompt=effective_prompt,
                    ),
                    timeout=360,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning("[DailySelfie] 第4轮(提示词构建)返回空文本(第%d次)", attempt + 1)
                    if attempt == 0:
                        logger.info("[DailySelfie] 第4轮(提示词构建)返回空，重试一次")
                        continue
                    return []

                parsed = _parse_llm_lines(text, expected)
                if len(parsed) < expected and attempt == 0:
                    logger.warning(
                        "[DailySelfie] 第4轮(提示词构建)返回 %d 条（期望 %d 条），重试一次",
                        len(parsed), expected,
                    )
                    continue
                if len(parsed) < expected:
                    logger.warning(
                        "[DailySelfie] 第4轮(提示词构建)重试后仍返回 %d 条（期望 %d 条），按实际返回处理",
                        len(parsed), expected,
                    )
                return parsed
            except asyncio.TimeoutError:
                logger.error("[DailySelfie] 第4轮(提示词构建)调用超时(360s)(第%d次)", attempt + 1)
                if attempt == 0:
                    logger.info("[DailySelfie] 第4轮(提示词构建)超时，重试一次")
                    continue
                return []
            except Exception as e:
                logger.error("[DailySelfie] 第4轮(提示词构建)调用失败(第%d次): %s", attempt + 1, e)
                if attempt == 0:
                    logger.info("[DailySelfie] 第4轮(提示词构建)异常，重试一次")
                    continue
                return []
        return []

    async def _search_reference_images(
        self,
        queries: list[str],
        wardrobe: Any,
        persona_name: str = "",
        min_similarity: float | None = None,
    ) -> list[dict]:
        used_ids: set[str] = set()
        results: list[dict | None] = [None] * len(queries)

        async def _search_one(idx: int, query: str) -> None:
            try:
                if hasattr(wardrobe, "get_reference_image"):
                    ref = await wardrobe.get_reference_image(
                        query=query,
                        current_persona=persona_name,
                        min_similarity=min_similarity,
                        daily_selfie_mode=True,
                    )
                    if ref:
                        img_id = str(ref.get("image_id", ""))
                        if img_id and img_id not in used_ids:
                            used_ids.add(img_id)
                            results[idx] = ref
            except Exception as e:
                logger.warning("[DailySelfie] 参考图搜索失败: query=%s error=%s", query[:50], e)

        await asyncio.gather(*[_search_one(i, q) for i, q in enumerate(queries)])
        return results

    async def _get_style_pool(self, wardrobe: Any, persona_name: str = "") -> list[str]:
        try:
            if persona_name and hasattr(wardrobe, "get_style_pool_for_persona"):
                persona_pool = await wardrobe.get_style_pool_for_persona(persona_name)
                if persona_pool:
                    logger.info(
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
