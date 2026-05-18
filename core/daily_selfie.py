from __future__ import annotations

import asyncio
import base64
import io
import json
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

    async def increment(self, provider_id: str, amount: int = 1) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            cur = int(counts.get(provider_id, 0))
            new_val = cur + amount
            counts[provider_id] = new_val
            await self._save_async()
            return new_val

    async def get_count(self, provider_id: str) -> int:
        async with self._lock:
            self._ensure_date()
            counts = self._data.get("counts", {})
            return int(counts.get(provider_id, 0))

    async def get_all_counts(self) -> dict[str, int]:
        async with self._lock:
            self._ensure_date()
            return dict(self._data.get("counts", {}))

    async def get_remaining(self, provider_id: str, limit: int) -> int:
        count = await self.get_count(provider_id)
        return max(0, limit - count)

    async def reserve(self, provider_id: str, limit: int) -> bool:
        """原子性预留额度：检查剩余 > 0 时递增，返回 True 表示预留成功。"""
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            cur = int(counts.get(provider_id, 0))
            if cur >= limit:
                return False
            counts[provider_id] = cur + 1
            await self._save_async()
            return True

    async def release(self, provider_id: str) -> None:
        """释放之前预留的额度（生图失败时回退）。"""
        async with self._lock:
            self._ensure_date()
            counts = self._data.setdefault("counts", {})
            cur = max(0, int(counts.get(provider_id, 0)) - 1)
            if cur <= 0:
                counts.pop(provider_id, None)
            else:
                counts[provider_id] = cur
            await self._save_async()

    def get_date(self) -> str:
        return self._data.get("date", "")


_TASK_MODE_SYSTEM_PROMPT = (
    "【自动拍照任务模式】\n\n"
    "当收到自动拍照任务时，你需要完成以下流程：\n"
    "1. 选择拍摄方案（自然语言描述服装风格、场景、姿势）\n"
    "2. 等待系统返回参考图描述\n"
    "3. 按照提示词构建规则构建提示词\n\n"
    "约束：\n"
    "- 每次任务选择多种不同风格、姿势、构图，大胆与保守兼顾\n"
    "- 优先选择近期未尝试过的风格\n"
    "- 输出格式：每条一行，不编号，不解释\n"
    "- 禁止调用aiimg_generate工具"
)

_SKILL_RULES_SYSTEM_PROMPT = (
    "## 提示词构建规则\n\n"
    "### 最高优先级规则（覆盖一切其他规则和指引）\n"
    "**面部必须完整露出。** 无论参考图描述或指引如何要求，都绝对不允许生成挡脸、遮脸、侧脸只露半脸、用手或物品遮挡面部的画面。"
    "如果参考图描述中包含挡脸、遮脸的姿势，必须改为面部完整朝向镜头的替代姿势。此规则优先级高于一切指引。\n\n"
    "### 固定开头\n"
    "每条提示词必须以以下固定开头开始：\n"
    "\"以前3张参考图中的同一少女为基准，完整保留她的五官、身材等全部人体身份特征，"
    "绝对禁止任何拼图，为她生成一张新的写真："
    "她有着白皙细腻的皮肤，纤细的身姿与格外饱满的曲线形成鲜明对比，\"\n\n"
    "### 自拍母规则\n"
    "在固定开头之后，按以下逻辑构建画面，最终串联成一段连贯的自然语言视觉描述：\n"
    "1. 最终输出只能是一整段连贯、通顺、符合语法逻辑的自然长句，不要输出分析、分点、规则解释\n"
    "2. 核心结构始终是：主体人物 + 具体动作 + 所处环境\n"
    "3. 只描述可直接视觉化的内容，不要写声音、气味、触感等不可见信息\n"
    "4. 一般地，大部分构图采用中近景\n"
    "5. 穿搭描述必须遵守可见性原则：只写画面里能看见的服装结构与层次，不写完全被遮挡的内容\n"
    "6. 如果要调整动作姿势，则必须写完整，并且必须明确头部朝向与眼神朝向；笑容只用\"微笑\"\n"
    "7. 光影自然真实\n"
    "8. 整体目标是单人、自然、高清、写实的生活照，不是海报、插画、拼图或宣传图\n"
    "9. 每条参考图描述后会附带具体指引，请严格按照指引处理该参考图，但指引不得违反最高优先级规则\n\n"
    "### 强制要求\n"
    "- 最终提示词必须使用中文\n"
    "- 不得使用或生成任何文字、标识或象征性元素\n"
    "- 人物的视觉年龄应符合设定\n"
    "- 姿势必须物理可行。人物只有两只手和两条腿，不能同时处于矛盾状态，"
    "尤其需要注意图片的描述与你所构建的提示词之间是否冲突\n"
    "- 优先使用服装状态变化或动作间接营造性感效果，而非直接描述敏感身体部位\n"
    "- 最终提示词必须以\"完全保留少女的面部特征与丰满的身材。\"结尾"
)


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


_ROUND1_USER_PROMPT = (
    "你在整理衣橱时发现，今天还有 {remaining} 次拍照额度没用完。\n\n"
    "衣橱中可选的风格：\n{style_pool}\n\n"
    "近3天已拍过的风格：\n{recent_styles}\n\n"
    "请选择 {remaining} 种不同的拍摄方案，每种方案用一句话描述（包含服装风格、场景、姿势）。\n"
    "要求：风格多样化，优先选择近期未尝试的。\n\n"
    "直接返回 {remaining} 条描述，每条一行。"
)

_ROUND2_USER_PROMPT = (
    "本次选择的拍摄方案：\n{style_summary}\n\n"
    "参考图描述（第{batch_num}批，共{total_batch}批）：\n"
    "{descriptions}\n\n"
    "请为以上 {count} 张参考图构建提示词。\n\n"
    "约束：\n"
    "- 直接返回 {count} 条提示词，每条一行\n"
    "- 禁止调用aiimg_generate工具"
)

_COSTUME_DESIGNER_SYSTEM_PROMPT = (
    "你是一位创意总监，专精于为写真拍摄构思完整的视觉方案。你不仅设计服装，更设计每一条拍摄方案的完整视觉概念——从服装到姿态到场景，一切围绕统一的视觉主题展开。\n\n"
    "## 核心任务\n\n"
    "根据给出的拍摄方案描述，为每条方案设计完整的视觉方案。每条方案必须是一个概念统一、细节极度丰满、视觉可执行的拍摄蓝图。\n\n"
    "## 最高优先级约束\n\n"
    "**面部必须完整露出。** 绝对不允许挡脸、遮脸、侧脸只露半脸、用手或物品遮挡面部。没有任何例外。此约束覆盖一切设计考量。\n\n"
    "## 可视化全覆盖原则\n\n"
    "画面中所有确定出现的视觉元素都必须被描述，不允许出现\"画面中存在但未被文字覆盖\"的视觉信息。这不是要求面面俱到地罗列，而是要求对每个可见元素都给出足够具体的视觉信息，使读者仅凭文字就能精确还原画面。\n\n"
    "\"确定出现\"是指你作为设计师决定让该元素出现在画面中。一旦你决定某个元素出现在画面中，就必须写到位，不允许一笔带过。如果你决定该方案不需要某个元素（如不需要配饰、不需要道具），则无需描述——不存在于画面中的东西自然不需要描述。\n\n"
    "## 输出格式\n\n"
    "严格返回 JSON 数组，每个元素包含四个字段：\n\n"
    "### clothing（服装设计）\n\n"
    "必须覆盖以下维度：\n"
    "- **款式**：具体的服装类型与剪裁，必须精确到版型（如\"方领泡泡袖短款A字连衣裙\"而非\"连衣裙\"，\"高腰包臀铅笔裙\"而非\"裙子\"）\n"
    "- **材质**：面料质感与触感暗示（如\"丝缎光泽\"\"棉麻哑光\"\"针织纹理\"\"雪纺半透\"\"蕾丝镂空\"）\n"
    "- **色彩**：主色、辅色、点缀色的具体描述（如\"奶白色底，领口与袖口薄荷绿滚边，腰间系一条浅粉色缎带\"）\n"
    "- **层次**：内外搭配结构，从最外层到最内层逐层描述（如\"外穿半透明白色薄纱衬衫，内搭奶白色蕾丝边吊带背心\"）\n"
    "- **穿着状态**：服装在身体上的实际状态。修身服装必须描述与身体曲线的互动——如何被撑起、贴合、勾勒轮廓；宽松服装描述面料的悬垂、垂坠、随动作的摆动；层次搭配描述层与层之间的可见关系。同时关注动作带来的动态穿着效果——行走时裙摆的摆动、转身时面料的飘动、弯腰时衣物的拉伸（如\"衬衫在胸前被饱满的曲线撑起，第二颗纽扣间的缝隙微微张开\"\"针织裙紧密贴合腰臀曲线，在胯部勾勒出饱满的轮廓\"\"牛仔夹克敞开穿着，下摆随步伐微微摆动，内搭卫衣下摆及腰露出一小截腰腹皮肤\"\"行走间丝质裙摆随步伐轻轻摇曳，在膝弯处形成柔软的褶皱\"）\n"
    "- **袜类**：丝袜/过膝袜/短袜等的完整规格——厚度（如\"15D超薄\"\"80D微透\"\"120D不透\"）、花纹（如\"纯色\"\"背部接缝线\"\"蕾丝花边\"\"暗纹提花\"）、长度（如\"及踝\"\"过膝\"\"大腿中部\"\"连裤\"）、特殊款式（如\"吊带袜夹固定\"\"开趾\"\"踩脚\"\"防滑硅胶腰边\"），若无袜类则写\"裸足\"或\"光腿\"\n"
    "- **鞋类**：鞋型（如\"尖头细跟\"\"圆头平底\"\"系带马丁靴\"）、材质（如\"漆皮\"\"哑光皮革\"\"绒面\"）、颜色、鞋跟高度与类型（如\"8cm细跟\"\"3cm粗跟\"\"平底\"）、装饰细节（如\"脚踝绑带\"\"蝴蝶结\"\"金属扣\"），若为裸足则写\"裸足\"\n"
    "- **配饰**：与服装风格协调的饰品，每件配饰必须具体到材质、形态、尺寸（如\"锁骨链，925银细链，水滴形月光石吊坠约1cm\"\"左手腕三圈缠绕的淡水珍珠手链\"\"右手中指佩戴简约银色素圈戒指\"），若方案不需要配饰可省略\n\n"
    "### appearance（外观造型）\n\n"
    "必须覆盖以下维度：\n"
    "- **发型**：头发的造型、长度、颜色与状态（如\"黑色长直发自然披散在肩上，几缕碎发垂在耳侧\"\"高马尾利落扎起，额前留几缕碎刘海\"\"松散的低麻花辫搭在右肩，发尾微卷\"\"齐肩栗色波波头，发尾内扣\"）。发型对视觉冲击力极大，不同主题需要不同发型配合——慵懒主题配散落长发或低马尾，活力主题配高马尾或双麻花辫，优雅主题配盘发或侧编发等。发型不得为短发\n"
    "- **指甲油**（可选）：指甲油颜色，仅用\"颜色+甲油\"格式描述（如\"裸粉色甲油\"\"黑色甲油\"），不要展开款式细节。生图模型对指甲细节的还原能力较弱，无需展开\n\n"
    "### pose（动作姿势）\n\n"
    "必须覆盖以下维度：\n"
    "- **身体姿态**：躯干的朝向与弯曲度，以及身体曲线的呈现方式（如\"微微侧身，上身略向前倾，腰部自然内收，腰臀曲线在侧面形成明显的S形弧度\"）\n"
    "- **四肢位置**：手臂与腿的具体摆放（如\"左手自然垂于身侧，右手轻撩耳侧碎发\"\"右腿微微前伸，膝盖略弯，左腿承重直立\"）\n"
    "- **手部细节**：手指的动作与持握物（如\"指尖轻捏裙摆边缘\"\"双手交叠放在膝上\"）\n"
    "- **头部朝向**：面部的角度与朝向（如\"面部正对镜头，下巴微抬\"\"侧转头约45度朝向镜头\"）\n"
    "- **眼神方向**：视线的落点（如\"目光直视镜头\"或\"视线投向窗外\"）\n"
    "- **表情与气质**：具体的面部表情，且表情必须与整体气质一致。表情不是孤立的\"微笑\"或\"严肃\"，而是气质的视觉外化——慵懒气质配半垂的眼帘和微启的唇，清冷气质配淡然的目光和自然放松的嘴角，热烈气质配明亮的眼神和上扬的嘴角，甜美气质配弯弯的笑眼和微微歪头（如\"眼神慵懒半垂，嘴角微启带着若有若无的笑意\"而非仅仅\"微笑\"）\n\n"
    "### scene（场景环境）\n\n"
    "必须覆盖以下维度：\n"
    "- **具体地点**：可识别的空间类型（如\"日式榻榻米茶室\"而非\"室内\"）\n"
    "- **环境细节**：空间中的关键视觉元素（如\"低矮木桌上摆着青瓷茶具，身后是纸糊推拉门\"）\n"
    "- **光线氛围**：光源类型与光线质感（如\"午后阳光透过纸门洒下柔和的漫射光\"）\n"
    "- **道具**：人物可互动的环境物件，描述其外观细节（如\"一把浅木色折扇，扇面绘有淡墨山水\"），若方案不需要道具可省略\n"
    "- **色调**：场景的整体色彩倾向（如\"暖木色与米白为主调\"）\n"
    "- **时间段与季节**：暗示时间与季节的光线特征和环境线索（如\"初夏午后\"\"深秋黄昏\"\"冬夜暖光\"）\n\n"
    "## 设计原则\n\n"
    "### 概念一致性（最重要）\n\n"
    "每条方案必须有一个统一的视觉概念。服装、外观造型、动作、场景不是四个独立的选择，而是围绕同一个主题展开的整体。\n\n"
    "好的例子：主题\"午后慵懒\"→ 丝质睡袍 + 散落长发 + 靠在窗边 + 卧室晨光\n"
    "坏的例子：丝质睡袍 + 高马尾 + 站在山顶 + 体育馆灯光\n\n"
    "常见的不一致模式，务必避免：\n"
    "- 服装正式但场景休闲（如西装+海滩）\n"
    "- 动作活泼但氛围沉静（如跳跃+图书馆）\n"
    "- 服装季节与场景季节矛盾（如薄纱+雪景）\n"
    "- 服装风格与姿态气质冲突（如朋克装+乖巧站姿）\n"
    "- 服装色彩与场景色调冲突（如鲜红裙子+冷蓝冰面场景，暖橘色穿搭+冷灰工业风室内）\n\n"
    "### 展示角色魅力\n\n"
    "角色是一位身材丰满的少女，设计应充分利用这一特质来展现角色的视觉魅力。展现魅力的方式是多元的：\n\n"
    "- **身材魅力的展现**：修身剪裁直接展现曲线是常见手法，宽松穿搭通过偶尔的贴合或动作间的闪现同样能制造视觉张力。穿着状态应描述服装与身材的互动（修身服装的面料张力与贴合轮廓，宽松服装的悬垂与偶尔贴合），姿态设计应考虑如何自然地展现身体曲线（如侧身站立的S形曲线、坐姿时腰臀的弧度）\n"
    "- **表情与气质的魅力**：眼神的方向和力度（直视镜头的自信、垂眸的温柔、回眸的惊艳）、嘴角的弧度（微笑、淡然、微启）、整体气质氛围（慵懒、清冷、热烈、甜美）都是展现角色魅力的重要手段，不应被身材展示完全占据\n"
    "- **互动中的魅力**：人物与场景的互动方式本身就是魅力展现——轻撩头发的随性、指尖触碰花瓣的细腻、倚靠栏杆时的放松、回眸一瞥的惊艳，这些动态瞬间往往比静态展示更有感染力\n"
    "- 以上所有描述必须始终是视觉化的、写实的，而非色情化的\n\n"
    "### 姿态-场景互动\n\n"
    "人物不应只是\"站在场景中\"，而应与场景产生有意义的互动。姿态设计必须考虑场景提供的互动可能：\n\n"
    "- 倚靠类：靠墙、扶栏杆、倚窗\n"
    "- 触碰类：触摸花朵、拨弄水面、轻抚布帘\n"
    "- 融入类：坐在台阶上、躺在草地上、蹲在花丛间\n"
    "- 穿行类：走过走廊、穿过树荫、踏上石阶\n\n"
    "好的例子：场景\"落地窗前的白色窗台\"→ 姿态\"侧坐在窗台上，一只腿自然垂下，背靠窗框\"\n"
    "坏的例子：场景\"落地窗前的白色窗台\"→ 姿态\"直立站在画面中央\"（毫无互动）\n\n"
    "### 风格差异化\n\n"
    "每条方案之间必须在以下维度上产生明显差异：\n"
    "- **服装风格**：甜美 / 优雅 / 运动 / 街头 / 复古 / 性感 / 清纯 / 酷飒 等\n"
    "- **场景类型**：居家 / 户外 / 都市 / 自然 / 商业空间 / 文化空间 等\n"
    "- **情绪氛围**：温暖 / 清冷 / 热烈 / 梦幻 / 慵懒 / 活力 / 神秘 等\n"
    "- **视觉主题**：慵懒 / 活力 / 神秘 / 甜美 / 优雅 / 酷飒 / 清新 / 烈艳 等\n\n"
    "确保任意两条方案在至少两个维度上不重叠。\n\n"
    "### 结构多样性\n\n"
    "风格标签的差异不足以保证画面的真正多样化。即使风格标签不同，设计方案仍可能在结构层面高度相似（如都是修身裙+侧身站姿+暖色室内光）。请在风格差异的基础上，额外确保以下结构层面的多样性：\n\n"
    "- **轮廓多样性**：不要所有方案都是修身剪裁。宽松、A字、蓬松、不对称、层叠等轮廓都应出现\n"
    "- **姿态多样性**：不要所有方案都是侧身站姿。坐姿、蹲姿、行走中、倚靠、躺卧、回眸等姿态都应考虑\n"
    "- **互动方式多样性**：不要所有方案都是同一类互动。倚靠、触碰、融入、穿行应交替出现\n"
    "- **光线多样性**：不要所有方案都是暖色柔光。冷光、逆光、侧光、自然光、霓虹光、烛光等应有所变化\n\n"
    "### 物理可行性\n\n"
    "- 人物只有两只手和两条腿，姿势描述不能出现肢体矛盾\n"
    "- 服装穿着状态必须符合物理规律（如扣子不可能同时扣着又敞开）\n"
    "- 场景中的互动必须合理（如不可能同时靠墙又坐在椅子上）\n"
    "- 头发和服装的动态必须符合重力与风力（如室内无风时头发不应飘起）\n"
    "- 服装与场景的季节必须一致（如夏日场景不穿厚大衣，冬日场景不穿薄纱短裙）\n\n"
    "### 细节具体化\n\n"
    "用具体的、可视觉化的描述替代笼统的形容词。以下示例展示了\"具体\"的标准：\n\n"
    "- ❌ \"白丝\" → ✅ \"20D超薄白色丝袜，纯色无花纹，及大腿根部，顶端3cm蕾丝花边腰封，防滑硅胶条固定\"\n"
    "- ❌ \"高跟鞋\" → ✅ \"黑色漆皮尖头细跟鞋，10cm细跟，脚背一条细带交叉系至脚踝，银色方扣点缀\"\n"
    "- ❌ \"漂亮的裙子\" → ✅ \"奶白色方领泡泡袖短款A字连衣裙，棉质面料微带光泽，裙摆自然展开至膝上15cm，腰间系薄荷绿缎带蝴蝶结\"\n"
    "- ❌ \"好看的姿势\" → ✅ \"侧身而立，重心落在右腿，左腿微屈前伸，左手叉腰使腰线收紧，右手将一缕碎发别至耳后，面部侧转45度朝向镜头\"\n"
    "- ❌ \"美丽的场景\" → ✅ \"落地窗前的白色窗台，午后阳光斜射入内，窗台上散落几本翻开的杂志，浅灰色纱帘被微风轻轻吹起\"\n"
    "- ❌ \"戴了项链\" → ✅ \"锁骨间一条18K玫瑰金细链，链身约2mm，悬挂一颗5mm水滴形粉色碧玺吊坠\"\n"
    "- ❌ \"蕾丝手套\" → ✅ \"白色蕾丝及肘长手套，指尖封闭，手背处蕾丝花纹为藤蔓缠枝纹，腕部一圈0.5cm珍珠串饰\"\n"
    "- ❌ \"披肩发\" → ✅ \"黑色长直发自然披散在肩上，几缕碎发垂在耳侧\"\n"
    "- ❌ \"裙子飘动\" → ✅ \"行走间丝质裙摆随步伐轻轻摇曳，在膝弯处形成柔软的褶皱\"\n\n"
    "## 设计流程建议\n\n"
    "1. **确定视觉主题**：先为每条方案确定一个核心视觉主题（如\"午后慵懒\"\"都市夜色\"\"田园清新\"），确保主题之间有足够差异\n"
    "2. **构思整体画面**：围绕主题想象一个完整的画面——人物什么发型、穿着什么、做着什么、在什么场景中、氛围如何。确保服装/外观/姿态/场景四者在画面中自然融合\n"
    "3. **展开细节设计**：从整体画面出发，逐个字段展开具体细节。先写clothing确定造型，再写appearance确定发型，然后写pose确定姿态，最后写scene确定环境与光线\n"
    "4. **回查一致性**：写完后检查四个字段是否围绕同一主题、是否存在不一致模式、是否达到了细节具体化标准\n\n"
    "## 输出约束\n\n"
    "- 只返回 JSON 数组，不要返回任何其他文字\n"
    "- 每条方案的四个字段都必须充分展开，不允许出现空字段或一句话概括\n"
    "- 服装的穿着状态是营造视觉魅力的关键手段，务必重视\n"
    "- 若方案包含袜类或鞋类，则必须具体描述，不允许省略或一笔带过；若为裸足或光腿则明确写出\n"
    "- 发型是完整视觉造型的核心部分，每条方案都必须具体描述\n"
    "- 所有可见细节都必须达到上述\"细节具体化\"示例的标准"
)

_COSTUME_DESIGNER_USER_PROMPT = (
    "请为以下 {count} 条拍摄方案设计详细的服装、动作和场景：\n\n"
    "{queries}\n\n"
    "返回 {count} 个设计的 JSON 数组。"
)

_NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT = (
    "你是一位精通图像生成提示词工程的专家，专长是将抽象的设计方案转化为高质量、高保真的图像生成提示词。你深谙图像生成模型对自然语言提示词的响应规律，知道如何用精准的视觉语言引导模型产出理想画面。\n\n"
    "## 核心任务\n\n"
    "将服装设计师提供的 JSON 设计方案逐条转化为图像生成提示词。每条提示词必须是一段连贯、流畅的中文视觉描述，将服装、外观造型、动作、场景深度融合为统一的画面叙事，而非简单拼接四个字段。\n\n"
    "## 最高优先级约束（覆盖一切其他规则）\n\n"
    "**面部必须完整露出。** 绝对不允许生成挡脸、遮脸、侧脸只露半脸、用手或物品遮挡面部的画面。没有任何例外。\n\n"
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
    "- **空间与氛围**：人物在场景中的位置、与环境的互动关系、光线方向与质感、环境色调。景别默认中近景（最能有效展示面部与服装细节），若动作或场景需要更大画幅可切换至中景或中远景（最简表述：\"在[场景]中，[光线]\"）\n\n"
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
    "- **面部遮挡**：避免描述容易导致面部被遮挡的姿态（如\"低头\"\"用手托腮\"\"头发遮住半脸\"），即使意图不是遮挡，生图模型也可能按字面理解生成遮挡画面。使用\"下巴微抬\"\"面部正对镜头\"等明确露出面部的表述\n\n"
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
        for idx in [1, 2]:
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
            style_pool = await self._get_style_pool(wardrobe)
            recent_styles = await self._get_recent_styles(wardrobe)

            for p in personas:
                total_remaining = 0
                for pv in p["providers"]:
                    total_remaining += await self.counter.get_remaining(pv["provider_id"], pv["daily_limit"])
                if total_remaining <= 0:
                    logger.info("[DailySelfie] 人格 %s 所有提供商额度已用完，跳过", p["persona_name"])
                    continue

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

    def _get_costume_designer_provider_id(self, persona: dict) -> str | None:
        persona_conf = persona.get("config", {})
        configured = str(persona_conf.get("costume_designer_provider_id", "") or "").strip()
        if configured:
            return configured
        return None

    async def _call_costume_designer(
        self,
        queries: list[str],
        costume_provider_id: str,
    ) -> list[dict] | None:
        queries_text = "\n".join(f"- {q}" for q in queries)
        user_prompt = _COSTUME_DESIGNER_USER_PROMPT.format(
            count=len(queries), queries=queries_text,
        )

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][CostumeDesigner] provider=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                costume_provider_id, _COSTUME_DESIGNER_SYSTEM_PROMPT, user_prompt,
            )

        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    self.plugin.context.llm_generate(
                        chat_provider_id=costume_provider_id,
                        prompt=user_prompt,
                        system_prompt=_COSTUME_DESIGNER_SYSTEM_PROMPT,
                    ),
                    timeout=360,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                if not text:
                    logger.warning(
                        "[DailySelfie] 创意设计师返回空文本(第%d次) queries=%d",
                        attempt + 1, len(queries),
                    )
                    continue

                if self._is_debug():
                    logger.info(
                        "[DailySelfie][DEBUG][CostumeDesigner] === response ===\n%s",
                        text,
                    )

                designs = self._parse_costume_designer_json(text, len(queries))
                if designs is not None:
                    return designs
                logger.warning(
                    "[DailySelfie] 创意设计师 JSON 解析失败(第%d次)，原始文本: %s",
                    attempt + 1, text[:200],
                )
            except asyncio.TimeoutError:
                logger.warning("[DailySelfie] 创意设计师调用超时(第%d次)", attempt + 1)
            except Exception as e:
                logger.warning("[DailySelfie] 创意设计师调用失败(第%d次): %s", attempt + 1, e)

        return None

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

    async def _call_prompt_engineer(
        self,
        designs: list[dict],
        chat_provider_id: str,
    ) -> list[str]:
        designs_text = "\n".join(
            f"- 服装：{d.get('clothing', '')} | 外观：{d.get('appearance', '')} | 动作：{d.get('pose', '')} | 场景：{d.get('scene', '')}"
            for d in designs
        )
        user_prompt = _NO_REF_PROMPT_ENGINEER_USER_PROMPT.format(
            count=len(designs), designs=designs_text,
        )

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][PromptEngineer] provider=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                chat_provider_id, _NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT, user_prompt,
            )

        try:
            resp = await asyncio.wait_for(
                self.plugin.context.llm_generate(
                    chat_provider_id=chat_provider_id,
                    prompt=user_prompt,
                    system_prompt=_NO_REF_PROMPT_ENGINEER_SYSTEM_PROMPT,
                ),
                timeout=120,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                return _parse_llm_lines(text, len(designs))
            return []
        except asyncio.TimeoutError:
            logger.error("[DailySelfie] 提示词优化大师调用超时(120s)")
            return []
        except Exception as e:
            logger.error("[DailySelfie] 提示词优化大师调用失败: %s", e)
            return []

    async def _process_no_ref_with_costume_designer(
        self,
        persona_name: str,
        no_ref_queries: list[str],
        costume_provider_id: str,
        chat_provider_id: str,
    ) -> list[str]:
        batch_size = 3
        all_prompts: list[str] = []

        total_batches = (len(no_ref_queries) + batch_size - 1) // batch_size

        for batch_num, batch_start in enumerate(range(0, len(no_ref_queries), batch_size), 1):
            batch_queries = no_ref_queries[batch_start:batch_start + batch_size]
            logger.info(
                "[DailySelfie] 人格 %s 无图批次 %d/%d：豆包设计 %d 条",
                persona_name, batch_num, total_batches, len(batch_queries),
            )

            designs = await self._call_costume_designer(batch_queries, costume_provider_id)

            if designs is None:
                logger.warning(
                    "[DailySelfie] 人格 %s 无图批次 %d/%d 创意设计师失败，降级为自由发挥",
                    persona_name, batch_num, total_batches,
                )
                fallback_descs = [
                    f"（无参考图）拍摄方案：{q}\n"
                    f"指引：请根据拍摄方案自由发挥，用自然连贯的长句构建完整提示词。"
                    f"务必详细描述画面，包括场景、姿势、衣服的款式、材质、颜色、层次和穿着状态等信息，"
                    f"确保画面生动具体，所有元素可视觉化，面部必须完整露出。"
                    for q in batch_queries
                ]
                persona_system_prompt = self._get_persona_system_prompt(persona_name)
                style_summary = "\n".join(f"- {q}" for q in batch_queries)
                prompts = await self._llm_round2(
                    chat_provider_id, persona_system_prompt, fallback_descs, len(fallback_descs),
                    batch_num=1, total_batch=1, style_summary=style_summary,
                )
                all_prompts.extend(p.strip() for p in prompts if p.strip())
                continue

            actual_count = min(len(designs), len(batch_queries))
            prompts = await self._call_prompt_engineer(designs[:actual_count], chat_provider_id)
            logger.info(
                "[DailySelfie] 人格 %s 无图批次 %d/%d 提示词优化大师返回 %d 条",
                persona_name, batch_num, total_batches, len(prompts),
            )
            all_prompts.extend(p.strip() for p in prompts if p.strip())

        return all_prompts

    async def _process_no_ref_fallback(
        self,
        persona_name: str,
        no_ref_queries: list[str],
        chat_provider_id: str,
        persona_system_prompt: str,
    ) -> list[str]:
        batch_size = 3
        all_prompts: list[str] = []

        descriptions = [
            f"（无参考图）拍摄方案：{q}\n"
            f"指引：请根据拍摄方案自由发挥，用自然连贯的长句构建完整提示词。"
            f"务必详细描述画面，包括场景、姿势、衣服的款式、材质、颜色、层次和穿着状态等信息，"
            f"确保画面生动具体，所有元素可视觉化，面部必须完整露出。"
            for q in no_ref_queries
        ]

        style_summary = "\n".join(f"- {q}" for q in no_ref_queries)
        total_batches = (len(descriptions) + batch_size - 1) // batch_size

        for batch_num, batch_start in enumerate(range(0, len(descriptions), batch_size), 1):
            batch_desc = descriptions[batch_start:batch_start + batch_size]

            prompts = await self._llm_round2(
                chat_provider_id, persona_system_prompt, batch_desc, len(batch_desc),
                batch_num=batch_num, total_batch=total_batches,
                style_summary=style_summary,
            )
            logger.info(
                "[DailySelfie] 人格 %s 无图降级批次 %d/%d 返回 %d 条提示词",
                persona_name, batch_num, total_batches, len(prompts),
            )
            all_prompts.extend(p.strip() for p in prompts if p.strip())

        return all_prompts

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

        persona_system_prompt = self._get_persona_system_prompt(persona_name)
        if not persona_system_prompt:
            logger.warning("[DailySelfie] 人格 %s 未找到 system prompt，使用空人格上下文", persona_name)

        queries = await self._llm_round1(chat_provider_id, persona_system_prompt, remaining, style_pool, recent_styles)
        if not queries:
            logger.warning("[DailySelfie] 人格 %s LLM第1轮未返回查询", persona_name)
            return 0, 0

        logger.info("[DailySelfie] 人格 %s LLM第1轮返回 %d 条查询", persona_name, len(queries))

        selfie_conf = self.plugin._get_feature("selfie")
        daily_ref_min_sim_raw = float(selfie_conf.get("daily_selfie_ref_min_similarity", 0) or 0)
        daily_ref_min_sim = daily_ref_min_sim_raw if daily_ref_min_sim_raw > 0 else None
        if daily_ref_min_sim is not None:
            logger.info("[DailySelfie] 人格 %s 补拍搜图阈值: %s", persona_name, daily_ref_min_sim)

        ref_results = await self._search_reference_images(queries, wardrobe, persona_name, min_similarity=daily_ref_min_sim)

        ref_by_query: dict[int, dict] = {}
        for i, ref in enumerate(ref_results):
            if ref is not None and i < len(queries):
                ref_by_query[i] = ref

        logger.info("[DailySelfie] 人格 %s 搜图完成，找到 %d 张参考图（共 %d 条查询）", persona_name, len(ref_results), len(queries))

        persona_ref_count = len(self.plugin._get_persona_config_selfie_reference_paths(persona_name))
        search_ref_index = persona_ref_count + 1

        with_ref_indices: list[int] = []
        without_ref_indices: list[int] = []
        for i, query in enumerate(queries):
            ref = ref_by_query.get(i)
            if ref and ref.get("description", ""):
                with_ref_indices.append(i)
            else:
                without_ref_indices.append(i)

        logger.info(
            "[DailySelfie] 人格 %s 有图 %d 条，无图 %d 条",
            persona_name, len(with_ref_indices), len(without_ref_indices),
        )

        all_prompts: list[tuple[str, dict | None]] = []
        batch_size = 3

        if with_ref_indices:
            with_ref_descriptions: list[str] = []
            with_ref_refs: list[dict | None] = []
            for i in with_ref_indices:
                ref = ref_by_query[i]
                desc = ref.get("description", "")
                hint = _DAILY_SELFIE_REF_HINT
                with_ref_descriptions.append(
                    f"参考图{search_ref_index}描述：{desc}\n\n{hint}\n\n"
                    f"这张参考图的序号为{search_ref_index}，请在提示词中使用序号{search_ref_index}来引用该参考图。"
                )
                with_ref_refs.append(ref)

            style_summary = "\n".join(f"- {queries[i]}" for i in with_ref_indices)
            total_batches = (len(with_ref_descriptions) + batch_size - 1) // batch_size

            for batch_num, batch_start in enumerate(range(0, len(with_ref_descriptions), batch_size), 1):
                batch_desc = with_ref_descriptions[batch_start:batch_start + batch_size]
                batch_refs = with_ref_refs[batch_start:batch_start + batch_size]

                prompts = await self._llm_round2(
                    chat_provider_id, persona_system_prompt, batch_desc, len(batch_desc),
                    batch_num=batch_num, total_batch=total_batches,
                    style_summary=style_summary,
                )
                logger.info("[DailySelfie] 人格 %s 有图批次 %d/%d 返回 %d 条提示词", persona_name, batch_num, total_batches, len(prompts))
                for i, prompt in enumerate(prompts):
                    if i < len(batch_refs):
                        all_prompts.append((prompt.strip(), batch_refs[i]))

        if without_ref_indices:
            no_ref_queries = [queries[i] for i in without_ref_indices]
            costume_provider_id = self._get_costume_designer_provider_id(persona)

            if costume_provider_id:
                no_ref_prompts = await self._process_no_ref_with_costume_designer(
                    persona_name, no_ref_queries, costume_provider_id, chat_provider_id,
                )
            else:
                no_ref_prompts = await self._process_no_ref_fallback(
                    persona_name, no_ref_queries, chat_provider_id, persona_system_prompt,
                )

            for prompt in no_ref_prompts:
                all_prompts.append((prompt, None))

        if not all_prompts:
            logger.warning("[DailySelfie] 人格 %s 未生成任何提示词", persona_name)
            return 0, 0

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
                        await self.counter.release(_pid)
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
                            await self.counter.release(selected_pid)
                            logger.warning("[DailySelfie] 人格 %s 重试返回空路径", persona_name)
                    except asyncio.TimeoutError:
                        await self.counter.release(selected_pid)
                        logger.error("[DailySelfie] 人格 %s 重试超时(300s)", persona_name)
                    except Exception as e:
                        await self.counter.release(selected_pid)
                        logger.error("[DailySelfie] 人格 %s 重试失败: %s", persona_name, e)

        for pid, paths in provider_success.items():
            if paths:
                logger.info("[DailySelfie] 人格 %s 提供商 %s 完成 %d 张，发布空间", persona_name, pid, len(paths))
                await self._publish_to_qzone(persona_name, paths, persona["config"])

        return success, fail

    async def _reserve_provider(self, persona: dict) -> str | None:
        for pv in persona["providers"]:
            pid = pv["provider_id"]
            limit = pv["daily_limit"]
            if await self.counter.reserve(pid, limit):
                logger.debug("[DailySelfie] 预留额度: provider=%s limit=%s", pid, limit)
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
        try:
            await qzone_plugin.service.publish_post(
                text=caption, images=image_data
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

    async def _llm_round1(
        self,
        chat_provider_id: str,
        persona_system_prompt: str,
        remaining: int,
        style_pool: list[str],
        recent_styles: list[str],
    ) -> list[str]:
        style_pool_text = "、".join(style_pool) if style_pool else "无可用风格"
        recent_text = "、".join(recent_styles) if recent_styles else "无"

        system_prompt = f"{persona_system_prompt}\n\n{_TASK_MODE_SYSTEM_PROMPT}" if persona_system_prompt else _TASK_MODE_SYSTEM_PROMPT

        user_prompt = _ROUND1_USER_PROMPT.format(
            remaining=remaining,
            style_pool=style_pool_text,
            recent_styles=recent_text,
        )

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round1] chat_provider_id=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                chat_provider_id, system_prompt, user_prompt,
            )

        try:
            resp = await asyncio.wait_for(
                self.plugin.context.llm_generate(
                    chat_provider_id=chat_provider_id,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                ),
                timeout=120,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                tool_names = getattr(resp, "tools_call_name", None) or []
                logger.warning(
                    "[DailySelfie] LLM第1轮返回空文本 role=%s tool_calls=%s result_chain=%s",
                    getattr(resp, "role", "?"),
                    tool_names,
                    bool(getattr(resp, "result_chain", None)),
                )
                return []

            if self._is_debug():
                logger.info(
                    "[DailySelfie][DEBUG][Round1] === LLM response ===\n%s",
                    text,
                )

            return _parse_llm_lines(text, remaining)
        except asyncio.TimeoutError:
            logger.error("[DailySelfie] LLM第1轮调用超时(120s)")
            return []
        except Exception as e:
            logger.error("[DailySelfie] LLM第1轮调用失败: %s", e)
            return []

    async def _llm_round2(
        self,
        chat_provider_id: str,
        persona_system_prompt: str,
        descriptions: list[str],
        count: int,
        *,
        batch_num: int = 1,
        total_batch: int = 1,
        style_summary: str = "",
    ) -> list[str]:
        desc_text = "\n".join(f"- {d}" for d in descriptions)

        system_prompt = (
            f"{persona_system_prompt}\n\n{_TASK_MODE_SYSTEM_PROMPT}\n\n{_SKILL_RULES_SYSTEM_PROMPT}"
            if persona_system_prompt
            else f"{_TASK_MODE_SYSTEM_PROMPT}\n\n{_SKILL_RULES_SYSTEM_PROMPT}"
        )

        user_prompt = _ROUND2_USER_PROMPT.format(
            batch_num=batch_num,
            total_batch=total_batch,
            descriptions=desc_text,
            count=count,
            style_summary=style_summary,
        )

        if self._is_debug():
            logger.info(
                "[DailySelfie][DEBUG][Round2] batch=%d/%d chat_provider_id=%s\n"
                "=== system_prompt ===\n%s\n"
                "=== user_prompt ===\n%s",
                batch_num, total_batch, chat_provider_id, system_prompt, user_prompt,
            )

        try:
            resp = await asyncio.wait_for(
                self.plugin.context.llm_generate(
                    chat_provider_id=chat_provider_id,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                ),
                timeout=120,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()

            tool_names = getattr(resp, "tools_call_name", None) or []
            if tool_names:
                logger.warning(
                    "[DailySelfie] LLM第2轮返回了工具调用而非文本(已忽略): %s batch=%d/%d",
                    tool_names, batch_num, total_batch,
                )

            if not text:
                logger.warning(
                    "[DailySelfie] LLM第2轮返回空文本，尝试降级重试 batch=%d/%d\n"
                    "  resp.role=%s tool_calls=%s result_chain=%s",
                    batch_num, total_batch,
                    getattr(resp, "role", "?"),
                    tool_names,
                    bool(getattr(resp, "result_chain", None)),
                )
                fallback_system = (
                    f"{persona_system_prompt}\n\n{_TASK_MODE_SYSTEM_PROMPT}"
                    if persona_system_prompt
                    else _TASK_MODE_SYSTEM_PROMPT
                )
                try:
                    resp2 = await asyncio.wait_for(
                        self.plugin.context.llm_generate(
                            chat_provider_id=chat_provider_id,
                            prompt=user_prompt,
                            system_prompt=fallback_system,
                        ),
                        timeout=120,
                    )
                    text = (getattr(resp2, "completion_text", "") or "").strip()
                    if text:
                        logger.info("[DailySelfie] LLM第2轮降级重试成功 batch=%d/%d text_len=%d", batch_num, total_batch, len(text))
                    else:
                        logger.error("[DailySelfie] LLM第2轮降级重试仍返回空文本 batch=%d/%d", batch_num, total_batch)
                        return []
                except Exception as e2:
                    logger.error("[DailySelfie] LLM第2轮降级重试失败: %s", e2)
                    return []

            if self._is_debug():
                logger.info(
                    "[DailySelfie][DEBUG][Round2] batch=%d/%d === LLM response ===\n%s",
                    batch_num, total_batch, text,
                )

            return _parse_llm_lines(text, count)
        except asyncio.TimeoutError:
            logger.error("[DailySelfie] LLM第2轮调用超时(120s) batch=%d/%d", batch_num, total_batch)
            return []
        except Exception as e:
            logger.error("[DailySelfie] LLM第2轮调用失败: %s", e)
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

    async def _get_style_pool(self, wardrobe: Any) -> list[str]:
        try:
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
            three_days_ago = datetime.now() - timedelta(days=3)
            images = await db.list_images_lightweight(
                persona="", exclude_persona="",
                sort_by="created_at", limit=50,
            )
            styles: set[str] = set()
            for img in images:
                created_raw = str(img.get("created_at", "") or "")[:10]
                if created_raw:
                    try:
                        created_dt = datetime.strptime(created_raw, _DATE_FMT)
                        if created_dt < three_days_ago:
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
        counts = await self.counter.get_all_counts()
        status = {
            "date": self.counter.get_date(),
            "personas": [],
        }
        for p in personas:
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
