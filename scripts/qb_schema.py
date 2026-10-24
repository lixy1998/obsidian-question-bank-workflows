# -*- coding: utf-8 -*-
"""Obsidian历史题库搭建与组卷 公共模块：受控词表 + 路径规则 + 通用函数。

题库方案（"每道题，一个文件"）：
- 正文只放题干/选项/材料，答案与解析藏 YAML frontmatter
- 目录即数据库：{vault}/{学段}-{序号类别}/{卷名}/{卷名}_{题型}/{序号}.md
  （如 TNote/初中-1中考真题/2026河南中考/2026河南中考_选择题/01.md）
- 原创题/改编题不建独立文件夹，靠「来源」标签区分（教材改编/中考改编/原创题等）
- 导出组卷 = 剥离 frontmatter，答案永不外泄
"""
import json
import os
import re
from pathlib import Path

# 本技能内配置文件：首次使用引导写入（setup.py --vault 或 AI 询问后写入）；已加入 .gitignore，不随仓库发布
CONFIG_FILE = Path(__file__).parent / "skill_config.json"


def load_config() -> dict:
    """读取本技能配置文件（不存在返回空 dict）。"""
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_vault() -> str:
    """题库库根目录（题库目录直接位于 vault 根下）。

    优先级：环境变量 TNOTE_VAULT > 本技能 scripts/skill_config.json 的 vault 字段 > 引导报错。
    - 首次使用：用 `python scripts/setup.py --vault <路径> [--init]` 写入配置（或直接告知 AI 询问配置）
    - 环境变量可省略：配置文件方案对不熟悉环境变量的用户更友好
    """
    v = os.environ.get("TNOTE_VAULT") or load_config().get("vault")
    if not v:
        raise RuntimeError(
            "未配置题库库（vault）路径。两种方式任选：\n"
            "  ① python scripts/setup.py --vault <路径> --init   （命令行引导，--init 可自动建库）\n"
            "  ② 告知 AI 你的题库库位置（或让 AI 新建），由 AI 写入 scripts/skill_config.json\n"
            "设置后重跑即可。"
        )
    return v.rstrip("/\\")


def __getattr__(name):
    """惰性导出 VAULT（2026-09-30 改）。

    此前 `VAULT = get_vault()` 写在模块顶层：未配置题库库时**导入即抛异常**，
    导致 `--help`、单测、纯工具函数都无法使用（实测 `scan_lesson.py --help` 直接
    RuntimeError）。改为模块级 __getattr__ 惰性求值——`from qb_schema import VAULT`
    的既有写法与语义保持不变，但只有真正访问时才要求配置。
    """
    if name == "VAULT":
        return get_vault()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ---- 受控词表 ----
STAGES = ["初中", "高中"]
# 类别 = 目录序号前缀 + 类别名（如 初中-1中考真题）；原创/改编不占目录，走「来源」标签
CATEGORIES = {
    "初中": ["1中考真题", "2中考模拟题", "3阶段测试"],
    "高中": ["1高考真题", "2高考模拟题", "3阶段测试"],
}
BOOKS = ["7上", "7下", "8上", "8下", "9上", "9下",
         "纲要上", "纲要下", "选必1", "选必2", "选必3",
         # 高中选修两模块（官方未编写教材，依课标选修课程的内容要求建表，2026-09-28 起）
         "选修1", "选修2"]
# 题型受控枚举（2026-09-26 起「小论文」统一为「论述题」；2026-10-20 实测库内
# `*_小论文` 目录 0 个、`题型: 小论文` 0 条，迁移已完成）
TYPES = ["选择题", "材料解析题", "论述题", "综合探究题", "填空题"]
# 旧值兼容：录入/扫描遇旧值自动归一为论述题，不再作为合法枚举（保留仅为容错手工输入旧称，
# 目录名层面不再做兼容——见下方说明）
TYPE_ALIASES = {"小论文": "论述题"}
DIFFICULTIES = ["易", "中", "难"]
# 来源标签：出处标签自由（卷名/教材课名），类型标签建议受控：
# 真题 / 教材改编 / 中考改编 / 原创题 / 期末卷 / 期中卷 / 月考卷 / 模拟卷（可扩展）
# 阶段测试卷按实际考试类型标注（期中卷→期中卷、月考卷→月考卷、期末卷→期末卷）
SOURCE_TAG_HINTS = ["真题", "教材改编", "中考改编", "原创题", "期末卷", "期中卷", "月考卷",
                    "练习卷", "周测卷", "单元卷", "专题卷", "模拟卷", "案例题"]

# 导出/生成必填字段（小节/知识点/来源标签可空或单值）
REQUIRED_FIELDS = ["id", "册别", "单元", "课", "题型", "难度",
                   "分值", "来源", "年份", "考区", "答案"]


def stage_roots() -> list:
    """vault 根下所有题库目录（初中-* / 高中-*）。

    未配置 / 目录不存在时经 `qb_bootstrap.require_vault()` 给出中文引导并退出码 2
    （而不是裸抛 RuntimeError）——本函数是绝大多数脚本第一个真正需要 vault 的动作，
    故把友好报错收敛在这里，调用方不必各写一套 try/except。
    """
    from qb_bootstrap import require_vault  # 延迟导入避免与 qb_bootstrap 形成环

    return sorted(p for p in require_vault().iterdir()
                  if p.is_dir() and re.match(r"^(初中|高中)-", p.name))


# 题库目录名（学段-序号类别），题目文件**必须**位于其下：
#   {vault}/{学段-类别}/{卷名}/{卷名}_{题型}/{序号}.md
# `is_question_file` 据此判定，避免把 vault 内非题库目录（0-Template-模板、1-MoC-索引、
# 2-命题与评价、3-试题研究 等）下的同名结构文件误判为题目。
STAGE_DIR_RE = re.compile(r"^(初中|高中)-")


class SchemaError(ValueError):
    pass


def validate_enum(field: str, value, allowed: list) -> None:
    if value not in allowed:
        raise SchemaError(f"字段 [{field}] 取值 '{value}' 不在受控枚举内：{allowed}")


def as_list(v) -> list:
    """把单值/多值统一为 list（兼容旧单值数据）。

    - None/空 -> []
    - str（含分号分隔） -> 拆分后的 [项, ...]
    - list/tuple -> 原样
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.split(";") if x.strip()]
    return [v]


def validate_record(rec: dict) -> None:
    """校验单条题目记录的字段完整性与枚举合法性。"""
    for f in REQUIRED_FIELDS:
        v = rec.get(f)
        if v is None:
            raise SchemaError(f"缺少必填字段 [{f}]")
        if isinstance(v, str) and not v.strip():
            raise SchemaError(f"缺少必填字段 [{f}]")
        if isinstance(v, list) and not v:
            raise SchemaError(f"缺少必填字段 [{f}]")
    validate_enum("学段", rec["学段"], STAGES)
    validate_enum("类别", rec["类别"], CATEGORIES[rec["学段"]])
    for b in as_list(rec["册别"]):  # 册别支持多值（跨册别综合题）
        validate_enum("册别", b, BOOKS)
    if rec["题型"] in TYPE_ALIASES:  # 旧值原地归一（后续建目录/写 YAML 均用新值）
        rec["题型"] = TYPE_ALIASES[rec["题型"]]
    validate_enum("题型", rec["题型"], TYPES)
    validate_enum("难度", rec["难度"], DIFFICULTIES)


# ---- BOOK_INDEX 课名词表 ----
BOOK_INDEX_PATH = Path(__file__).parent / "BOOK_INDEX.md"
_BOOK_CACHE = None
_METHOD_CACHE = None
_METHOD_UNIT_CACHE = None
# 学史方法（全六册）：**单元层级**（2026-10-06 改）——单元名固定 `学史方法`，
# 课名＝方法条目本身（如 `如何从出土文物中获取信息`，**不带任何前缀**）。
# 权威清单＝BOOK_INDEX「## 初中·学史方法（全六册）」总表；
# 各册版本节内亦按单元体例列出（`- 学史方法` + 缩进方法条目），两处逐字一致。
METHOD_UNIT = "学史方法"
# 活动课（方案A，2026-10-06 定）：词表写作顶格 `第N课 活动课 名称`（教材目录体例，N 接该册
# 常规课末号顺延）；题目的 **单元** 字段统一写 `活动课`（活动课不属任何常规单元），**课** 写整行。
ACTIVITY_UNIT = "活动课"
# 匹配活动课词表行（顶格）：`第N课 活动课 名称`；兼容历史无编号写法 `活动课 名称`
_ACTIVITY_LINE_RE = re.compile(r"^ {0,2}- ((?:第[\d一二三四五六七八九十]+课 活动课 .+)|(?:活动课 .+))$")
# 待核实标记：词表里方法名/子目不确定时尾随 `[?]`（如「如何编写历史剧本[?]」），
# 解析时一律剥除，**不进入课名白名单**（否则写进题库的课名会带标记）。
_UNVERIFIED_RE = re.compile(r"\s*\[\?\]\s*$")


def _strip_unverified(name: str) -> str:
    """剥除词表条目尾随的「待核实」标记 `[?]`。"""
    return _UNVERIFIED_RE.sub("", str(name).strip()).strip()

# 册别 -> 校验用词表版本节（按教材首印定版年份；首用后仅小修订、目录无变化，不建新版词表）
# 7上/7下 2024 首印定版（2024版）；8上/8下 2025 首印定版（2025版）；9上/9下 2026 首印定版（2026版）
# 高中册别（纲要/选必/选修）不走版本节，由 GAOZHONG_SECTIONS 整节映射（见下）
BOOK_INDEX_VERSION = {
    "7上": "2024版", "7下": "2024版",
    "8上": "2025版", "8下": "2025版",
    "9上": "2026版", "9下": "2026版",
}


def load_book_index() -> dict:
    """解析 BOOK_INDEX.md 各册别对应版本节 → {册别缩写: [课名全称, ...]}。

    - 初中六册：只收录 `BOOK_INDEX_VERSION` 指定的版本节（如 7上/7下 的 2024版节、
      8上/8下 的 2025版节、9上/9下 的 2026版节）内的「第X课/活动课」行。
    - 高中各册：收录「### 统编·人教社」下的 `####` 节（无版本节概念），册别缩写由
      `GAOZHONG_SECTIONS` 映射——纲要上/纲要下/选必1~3 依教材目录；选修1/选修2
      官方未编写教材，依课标选修模块的 7 个专题建表，课名形如 `专题1.1 历史与历史学`。

    2026-09-17 起高中各册纳入课名校验（此前仅初中六册参与，高中题的课名长期
    躲过机器校验，只能靠 load_lesson_units 的单元归属间接印证）；
    2026-09-28 增补选修1/选修2 两模块（用户依课标选修部分拟定）。
    """
    global _BOOK_CACHE
    if _BOOK_CACHE is not None:
        return _BOOK_CACHE
    index = {}
    if not BOOK_INDEX_PATH.exists():
        print(f"[警告] BOOK_INDEX 不存在：{BOOK_INDEX_PATH}")
        _BOOK_CACHE = index
        return index
    cur_book, in_target = None, False
    for line in BOOK_INDEX_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^### (\S+)", line)
        if m:
            cur_book, in_target = m.group(1), False
            continue
        if line.startswith("#### "):
            title = line[5:].strip()
            ver = re.match(r"^(20\d\d版)", title)
            if title in GAOZHONG_SECTIONS:
                cur_book, in_target = GAOZHONG_SECTIONS[title], True
            else:
                # 仅当本册别映射的版本节（如 "#### 2024版…"）才收录，其余版本节跳过
                in_target = bool(cur_book and ver
                                 and ver.group(1) == BOOK_INDEX_VERSION.get(cur_book))
            continue
        if in_target and cur_book:
            # 体例：单元行顶格 `- 第X单元 …` / `- 学史方法`；课行缩进 2 格。
            # 但「活动课」「专题X.Y」在词表里既可顶格亦曾缩进，故两种缩进都收。
            # 学史方法改为单元层级（2026-10-06）：`- 学史方法` 顶格为**单元**（不收录），
            # 其下缩进 2 格的**方法条目**按课名收录（条目名尾随 `[?]` 剥除）。
            m2 = re.match(r"^ {2,}- (.+)", line)
            if m2:
                name = _strip_unverified(m2.group(1).strip())
                if re.match(r"^(第\d+课 .+|活动课 .+|专题\d+\.\d+ .+)", name):
                    index.setdefault(cur_book, []).append(name)
                elif name in load_method_names():
                    index.setdefault(cur_book, []).append(name)  # 学史方法条目按课名收录
                continue
            m2o = re.match(r"^ {0,2}- ((?:第\d+课 .+)|(?:活动课 .+)|(?:专题\d+\.\d+ .+))", line)
            if m2o:
                index.setdefault(cur_book, []).append(m2o.group(1).strip())
    _BOOK_CACHE = index
    return index


# 高中各册节标题 -> 册别缩写（BOOK_INDEX「### 统编·人教社」下的 #### 节，无版本节概念）
# 选修两模块官方未编写教材，节标题按课标模块名命名（「选修1 史学入门」/「选修2 史料研读」）
GAOZHONG_SECTIONS = {
    "中外历史纲要（上）": "纲要上",
    "中外历史纲要（下）": "纲要下",
    "选择性必修1 国家制度与社会治理": "选必1",
    "选择性必修2 经济与社会生活": "选必2",
    "选择性必修3 文化交流与传播": "选必3",
    "选修1 史学入门": "选修1",
    "选修2 史料研读": "选修2",
}
_UNIT_RE = re.compile(r"^- ((?:第[0-9一二三四五六七八九十]+单元 .+)|(?:选修[0-9] .+)|(?:学史方法)|(?:活动课))$")  # 单元行；选修以模块名为「单元」，学史方法/活动课为固定单元
_LESSON_RE = re.compile(r"^ {2,}- ((?:第\d+课 .+?)|(?:专题\d+\.\d+ .+?)|(?:如何.+?))$")  # 课行；选修以课标专题为「课」，学史方法以「如何…」方法条目为「课」
_UNIT_CACHE = None


def load_lesson_units() -> dict:
    """解析 BOOK_INDEX.md → {(册别, 课名): 单元全称}。

    - 初中六册：只收 `BOOK_INDEX_VERSION` 指定的版本节（与 load_book_index 同口径）
    - 高中各册：收「### 统编·人教社」下的 `####` 节（无版本节概念；含选修1/选修2，
      其「单元」为模块名、「课」为课标专题）
    用途：校验时提示「课的所属单元/册别是否已写进该题的 单元/册别 字段」（多值录入兜底）。
    """
    global _UNIT_CACHE
    if _UNIT_CACHE is not None:
        return _UNIT_CACHE
    out = {}
    if not BOOK_INDEX_PATH.exists():
        _UNIT_CACHE = out
        return out
    cur_book, in_target, cur_unit = None, False, None
    for line in BOOK_INDEX_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^### (\S+)", line)
        if m:
            cur_book, in_target, cur_unit = m.group(1), False, None
            continue
        if line.startswith("#### "):
            title = line[5:].strip()
            ver = re.match(r"^(20\d\d版)", title)
            if title in GAOZHONG_SECTIONS:
                cur_book, in_target = GAOZHONG_SECTIONS[title], True
            else:
                # 仅本册别映射的版本节（如 "#### 2024版…"）才收录，其余版本节跳过
                in_target = bool(cur_book and ver
                                 and ver.group(1) == BOOK_INDEX_VERSION.get(cur_book))
            cur_unit = None
            continue
        if not in_target or not cur_book:
            continue
        mu = _UNIT_RE.match(line)
        if mu:
            cur_unit = mu.group(1).strip()
            continue
        # 活动课（方案A）：顶格 `第N课 活动课 名称`——**单元固定为 `活动课`**、课名为整行
        # （活动课不属任何常规单元；此映射供 scan 提示「单元应写 活动课」）
        if _ACTIVITY_LINE_RE.match(line):
            name = _strip_unverified(_ACTIVITY_LINE_RE.match(line).group(1).strip())
            out[(cur_book, name)] = ACTIVITY_UNIT
            cur_unit = ACTIVITY_UNIT
            continue
        ml = _LESSON_RE.match(line)
        if ml and cur_unit:
            out[(cur_book, _strip_unverified(ml.group(1).strip()))] = cur_unit
    _UNIT_CACHE = out
    return out


def load_method_names() -> set:
    """解析 BOOK_INDEX「初中·学史方法（全六册）」表格 → {方法名, ...}（**权威清单**）。

    该栏目不属于任何册别词表节，以表格总列 15 个方法（`## 初中·学史方法（全六册）`，
    行形如 `| 1 | 如何从出土文物中获取信息 | 7上 |`）。题库**单元**写 `学史方法`、
    **课**写方法条目本身（如 `如何从出土文物中获取信息`），校验时只比对方法名
    （**不按册别**）。条目尾随的待核实标记 `[?]` 一律剥除，不进白名单。
    """
    global _METHOD_CACHE
    if _METHOD_CACHE is not None:
        return _METHOD_CACHE
    names = set()
    if BOOK_INDEX_PATH.exists():
        in_sec = False
        for line in BOOK_INDEX_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("## ") and "学史方法" in s:
                in_sec = True
                continue
            if in_sec and s.startswith("## "):
                break  # 下一节（如 ## 高中）
            if in_sec:
                m = re.match(r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|", s)
                if m:
                    names.add(_strip_unverified(m.group(1)))
    _METHOD_CACHE = names
    return names


def load_method_units() -> set:
    """(册别, 方法条目) 集合——由各册版本节内「- 学史方法」单元下的课行解析。

    与 `load_method_names()`（总表白名单）互为印证：前者是权威清单，本函数反映
    **各册实际挂载**（未核实册别的方法名亦会被收录，同样剥除 `[?]`）。
    用途：`scan_questions.py` 定位字段提示——学史方法题的「单元」应写 `学史方法`。
    """
    global _METHOD_UNIT_CACHE
    if _METHOD_UNIT_CACHE is not None:
        return _METHOD_UNIT_CACHE
    out = set()
    if BOOK_INDEX_PATH.exists():
        cur_book = cur_ver = None
        in_target = False
        in_method = False
        for line in BOOK_INDEX_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^### (\S+)", line)
            if m:
                cur_book, cur_ver, in_target, in_method = m.group(1), None, False, False
                continue
            if line.startswith("#### "):
                title = line[5:].strip()
                ver = re.match(r"^(20\d\d版)", title)
                if title in GAOZHONG_SECTIONS:
                    cur_book, in_target = GAOZHONG_SECTIONS[title], True
                else:
                    in_target = bool(cur_book and ver
                                     and ver.group(1) == BOOK_INDEX_VERSION.get(cur_book))
                in_method = False
                continue
            if not (in_target and cur_book):
                continue
            raw = line.rstrip()
            indent = len(raw) - len(raw.lstrip(" "))
            content = raw.strip()
            if not content.startswith("- "):
                continue
            name = content[2:].strip()
            if indent == 0:
                in_method = (name == METHOD_UNIT)
                continue
            if indent == 2 and in_method and name:
                out.add((cur_book, _strip_unverified(name)))
    _METHOD_UNIT_CACHE = out
    return out


def validate_lesson(rec: dict) -> None:
    """校验课名是否在 BOOK_INDEX 词表中（课/册别支持多值）。

    策略：
    - **学史方法**（单元层级，2026-10-06 起）：`单元` 写 `学史方法`、`课` 写**方法条目本身**
      （如 `如何从出土文物中获取信息`），只校验方法名是否在「初中·学史方法（全六册）」总表内
      （**不按册别**——各册分配尚待确认，且中高考题常按「史事 + 方法维度」跨册标注，严格按册会误伤）；
      兼容旧前缀写法 `学史方法-<方法名>`（存量迁移期）；
    - 册别列表中某册别收录了词表（7上~9下按 BOOK_INDEX_VERSION 映射版本节）→ 该册别课程名须命中词表；
    - 册别列表中若存在未收录词表的册别（高中各册与选修现均已收录；新增册别尚未建表时）→ 无法校验的课名视为通过（不误报）；
    - 全部课名都是学史方法 → 方法名校验已完成，跳过册别词表校验。
    """
    books = as_list(rec.get("册别"))
    lessons = as_list(rec.get("课"))
    if not books or not lessons:
        return
    methods = load_method_names()
    rest = []
    for lesson in lessons:
        # 学史方法（单元层级，2026-10-06 起）：课名＝方法条目本身（无前缀），
        # 只校验方法名是否在「初中·学史方法（全六册）」总表内，**不按册别**。
        # 兼容旧写法 `学史方法-<方法名>`（存量迁移期）：剥前缀后同样按方法名比对。
        name = lesson[len("学史方法-"):].strip() if lesson.startswith("学史方法-") else lesson
        if name in methods:
            continue
        if lesson.startswith("学史方法-"):
            raise SchemaError(
                f"学史方法名不在 BOOK_INDEX「初中·学史方法（全六册）」表中：'{name}'"
                f"（共 {len(methods)} 个；现行为单元层级，课名直接写方法条目如 `如何从出土文物中获取信息`，"
                f"不再带 `学史方法-` 前缀）")
        rest.append(lesson)
    if not rest:
        return  # 全部课名都是学史方法 → 方法名校验已完成，跳过册别词表校验
    index = load_book_index()
    checked = {b: set(index.get(b, [])) for b in books if index.get(b)}
    if not checked:
        return  # 所有册别均无词表（高中等）→ 跳过
    has_free = any(b not in checked for b in books)  # 存在未收录词表的册别
    for lesson in rest:
        if any(lesson in s for s in checked.values()):
            continue
        if has_free:
            continue  # 可能属于未收录词表的册别，无法校验，不误报
        raise SchemaError(
            f"课名不在 BOOK_INDEX 词表中（册别{books}）：'{lesson}'，"
            f"请检查课名书写或补充对应册别")


def question_dir(rec: dict) -> Path:
    """按记录生成题目所在目录（vault 根：{学段}-{类别}/{卷名}/{卷名}_{题型}）。"""
    d = Path(get_vault()) / f"{rec['学段']}-{rec['类别']}" / rec["卷名"]
    return d / f"{rec['卷名']}_{rec['题型']}"


def question_path(rec: dict) -> Path:
    """题目文件完整路径：{卷名}_{题型}/{序号}.md"""
    return question_dir(rec) / f"{rec['序号']}.md"


def _render_image_options(stem: str, opts: list) -> str:
    """图片选项题的正文：md 表格横排（第一行图片、第二行「字母. 选项文字」）。

    与「多图题统一写法」同源（见 references/试卷排版规范.md、references/参考-图片与图注修复.md）：
    选项里的图片若沿用默认的行内拼接（`(A) ![](…) 文字  (B) ![](…)`），Obsidian 会把数张大图挤进
    一段、图注被拆到各图下方，预览极乱；改出表格后列与选项一一对齐。
    """
    letters = ["A", "B", "C", "D", "E", "F"]
    imgs, labels = [], []
    for i, raw in enumerate(opts):
        item = str(raw).strip()
        m = re.match(r"^(!\[[^\]]*\]\([^)]*\))\s*(.*)$", item)
        if m:
            imgs.append(m.group(1))
            text = m.group(2).strip()
        else:
            imgs.append("")
            text = item
        labels.append(f"{letters[i]}. {text}" if text else f"{letters[i]}.")
    head = "| " + " | ".join(imgs) + " |"
    sep = "|" + "|".join([":---:"] * len(opts)) + "|"
    lab = "| " + " | ".join(labels) + " |"
    return f"{stem}\n\n{head}\n{sep}\n{lab}"


def build_body(rec: dict) -> str:
    """由 题干(+选项) 生成正文。选择题：题干 + (A)(B)(C)(D)；材料题：题干原文。

    图片自动插图（2026-08-26 起）：rec["图片"] = [{"src": 源路径, "name": "21_1.jpg"}, ...]
    正文尾部自动追加 `![](images/名字)` 引用；题干已含同名引用（手写 `![](images/…)`）则不重复追加。
    行为与「图片字段负责复制文件、题干负责引用」的旧约定兼容，且选择题不再依赖题干手写引用。

    图片选项题（2026-10-06 起）：选项项内含图片引用时改出 md 表格（见 _render_image_options）。
    """
    stem = str(rec.get("题干", "")).strip()
    opts = rec.get("选项") or []
    if opts:
        if any("![" in str(o) for o in opts):
            body = _render_image_options(stem, opts)
        else:
            letters = ["A", "B", "C", "D", "E", "F"]
            line = "  ".join(f"({letters[i]}) {o.strip()}" for i, o in enumerate(opts))
            body = f"{stem}\n{line}".strip()
    else:
        body = stem
    imgs = rec.get("图片") or []
    if imgs:
        refs = []
        for it in imgs:
            name = (it.get("name") or "").strip()
            if not name or f"images/{name}" in body:
                continue  # 题干已含该图引用（手写）→ 去重
            refs.append(f"![](images/{name})")
        if refs:
            body = f"{body}\n\n" + "\n\n".join(refs)
    return body


def copy_images(rec: dict, target_dir: Path) -> None:
    """复制图片到题目目录 images/ 下（正文用 ![](images/xxx) 引用）。

    图片字段格式：[{"src": 源文件绝对路径, "name": 正文引用的文件名}]
    """
    imgs = rec.get("图片") or []
    img_dir = target_dir / "images"
    for it in imgs:
        src = Path(it["src"])
        name = it.get("name") or src.name
        dst = img_dir / name
        if not src.exists():
            print(f"[警告] 图片源不存在：{src}")
            continue
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        img_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        try:
            shutil.copy2(src, dst)
            print(f"[图片] {src.name} -> {dst}")
        except (PermissionError, OSError) as e:
            # Obsidian 打开库时图片可能被锁（WinError 32）：跳过不中断整批，重试即可
            print(f"[警告] 图片复制失败（可能被占用），已跳过：{dst}（{e}）")


def _yaml_scalar(v) -> str:
    """将值安全渲染为 YAML 标量（含特殊字符时用单引号包裹并双写单引号）。"""
    s = str(v)
    if s == "":
        return "''"
    if "\n" in s:
        return None  # 含换行交由调用方用 | 块
    # 含 YAML 特殊字符、首尾空白、或为 bool/null 关键字时加引号
    if re.search(r"[:#\"'\[\]{}&*!|>%@`,]", s) or s != s.strip() \
            or s.lower() in ("true", "false", "null", "yes", "no", "on", "off"):
        return "'" + s.replace("'", "''") + "'"
    return s


def dump_yaml(rec: dict) -> str:
    """渲染 YAML 块（保持字段顺序，列表用流式，多行/特殊字符安全转义）。"""
    order = ["id", "册别", "单元", "课", "小节", "知识点", "核心素养",
             "素养层次水平", "关键能力", "题型", "难度", "分值", "来源", "年份", "考区",
             "答案", "解析"]
    lines = ["---"]
    for k in order:
        if k not in rec or rec[k] is None or rec[k] == "":
            continue
        v = rec[k]
        if isinstance(v, (list, tuple)):
            items = []
            for x in v:
                s = _yaml_scalar(x)
                if s is None:  # 含换行的列表项：用双引号包裹并转义（流式内联）
                    s = '"' + str(x).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'
                items.append(s)
            lines.append(f"{k}: [{', '.join(items)}]")
        elif isinstance(v, str) and "\n" in v:
            lines.append(f"{k}: |")
            for ln in v.splitlines():
                lines.append(f"  {ln}")
        else:
            scalar = _yaml_scalar(v)
            if scalar is None:
                lines.append(f"{k}: |")
                for ln in str(v).splitlines():
                    lines.append(f"  {ln}")
            else:
                lines.append(f"{k}: {scalar}")
    lines.append("---")
    return "\n".join(lines)


def split_frontmatter(text: str):
    """拆分 md：返回 (frontmatter_dict, body_text, err)。

    err 为 None 表示正常；无 frontmatter 时 ( {}, text, None )；
    YAML 语法非法时返回 ( {}, text, 错误信息 )。
    """
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text, None
    import yaml
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        return fm, m.group(2), None
    except Exception as e:
        return {}, m.group(2), f"YAML解析失败：{e}"


def strip_comments(body: str) -> str:
    """移除正文中的 HTML 注释块（模板使用说明）。"""
    return re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()


# ---- 一题多卷与去重（v1.3.0 起）----
# id 为多值 list：**首项 = 主卷 id**（本题文件所在卷），其余 = 收录本题的其他卷「卷名-题号」。
#   例：id: [2024河南中考-08, 河南省实验中学2025-2026学年8上期末-03]
#   首项之后的别名有**两种来历**（2026-10-24 起，勿混）：
#     ① 他卷收录——dedupe_questions.py --merge 合并，别名题归档到 .trash；
#     ② 本校考试卷选用——record_usage.py 登记，**无题目文件、无归档件**，卷目录只放 `_组卷清单.md`；
#        故 find_inverted（--check-authority）遇「归档区找不到别名」会按设计跳过，非错误。
# 重复题以主卷为准唯一存题，别名题归档到 {vault}/.trash/<原相对路径>（详见 workflow/01-input.md「查重与合并」）
# ⚠ 归档落点 = **库内 Obsidian 回收站**（2026-09-27 定案）：
#   · 依赖 Obsidian 设置 `trashOption: local`（本库 `trashedFilesFolder` = `.trash`），两键在 .obsidian/app.json；
#     改设置须走运行时 API `app.vault.setConfig('trashOption','local')`——直接改文件会被运行中实例的内存值写回
#   · **不加中间层**：`.trash/重复题/…` 会让回收站面板的「还原」落回错误路径（还原依 `.trash/<原相对路径>` 回原位）
#   · 点开头目录不被索引 → 归档件不出现在库内面板/搜索/图谱；其 wikilink 亦不可解析，引用方一律改指主卷
#   · ⚠ 归档件是「可被清空的留痕」：清空回收站即失去还原原卷的凭据（**还原原卷的正源仍是原卷 PDF 存档**）
ARCHIVE_ROOT = ".trash"            # 归档落点＝库内回收站目录（Obsidian trashedFilesFolder，默认 .trash）
LEGACY_ARCHIVE_ROOTS = ("_归档", ".Z-Archive")  # 历史日志（.dedupe/*.json）记录过的旧归档根名，回滚时自动映射
DEDUPE_LOG_DIR = ".dedupe"        # 合并日志目录（vault 根下）

# 权威序（判「以谁为准」，数值越小越权威；同级按年份早者优先）
# 一级：类别目录（中/高考真题 0 > 中/高考模拟 1 > 阶段测试 2）
AUTHORITY_CATEGORY = {"1中考真题": 0, "1高考真题": 0,
                      "2中考模拟题": 1, "2高考模拟题": 1, "3阶段测试": 2}
# 二级：阶段测试卷名细分（期末早于期中/月考）
AUTHORITY_PATTERNS = [(0, r"一模|二模|三模|联考|模拟"), (1, r"期末"), (2, r"期中|月考|单元|阶段")]


def is_question_file(p) -> bool:
    """题库题目文件判定。

    判据（2026-09-30 收紧）：文件名合法 **且** 路径位于题库目录（`{vault}/{学段-类别}/…`）之下。

    此前只判文件名，把 vault 内 `0-Template-模板/历史试题模板.md` 等 3 个模板文件也
    算成题目，全库统计虚增（实测 2629 vs 真实 2626），且模板示例 id `2026河南中考-01`
    与真题 id 相撞（实测「同一 id 出现在多个文件」1 例即由此产生）。
    文件名判据保持原样（排除 README、`_` 前缀辅助文件、导出卷）。
    ⚠ **库内回收站 `.trash/` 下的归档件仍返回 True**（它们保持着题库内相对路径）——
    `check_note_links.py` 正是靠这个判定扫描归档件、反推旧 id。是否排除归档区由调用方决定：
    遍历用 `stage_roots()`（只含 初中-*/高中-*，天然不含 `.trash`）。
    """
    p = Path(p)
    if p.name == "README.md" or p.name.startswith("_"):
        return False
    if p.name.endswith(("_学生版.md", "_教师版.md", "_命题说明.md")):
        return False
    # 路径判据：任一层目录名命中「初中-…/高中-…」即视为题库内文件
    return any(STAGE_DIR_RE.match(part) for part in p.parts)


def authority_key(rec: dict, path=None) -> tuple:
    """一题多卷的权威序比较键（元组越小越优先，用于挑选主卷）。

    序：中/高考真题(0) > 中/高考模拟(1) > 阶段测试(2)；同级按考试类型细分
    （模拟：一模/二模/联考 0 > 期末 1 > 期中/月考 2）；再按**年份早者优先**；最后按卷名、题号稳定排序。
    `path` 传入题目文件路径时按路径推断类别，否则回退 rec["类别"]。
    """
    cat = None
    if path is not None:
        for part in Path(path).parts:
            for c, lvl in AUTHORITY_CATEGORY.items():
                if part.endswith(c):
                    cat = lvl
                    break
            if cat is not None:
                break
    if cat is None:
        cat = AUTHORITY_CATEGORY.get(str(rec.get("类别", "")), 9)
    vol = str(rec.get("卷名", ""))
    sub = 9
    for lvl, pat in AUTHORITY_PATTERNS:
        if re.search(pat, vol):
            sub = lvl
            break
    try:
        year = int(rec.get("年份"))
    except (TypeError, ValueError):
        year = 9999
    return (cat, sub, year, vol, str(rec.get("序号", "")))


def norm_for_dedupe(s: str) -> str:
    """查重归一化：去图片引用/分值括注/题号、全角转半角、去全部标点空白、英文小写。

    只保留汉字与 ASCII 字母数字——用于「一字之差」级别的同题比对（L1 精确指纹）。
    """
    import unicodedata
    s = re.sub(r"!\[\]\([^)]*\)", "", s or "")
    s = re.sub(r"[（(]\s*\d+\s*分\s*[)）]", "", s)
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)


def source_pairs(rec: dict) -> list:
    """把「来源」多值列表解析为 [(卷名, 类型标签), ...]（成对平铺约定）。

    `来源: [主卷名, 主类型, 别名卷名, 别名类型]` → [("主卷名","主类型"), ("别名卷名","别名类型")]。
    奇数长度时末项单独成对（类型留空），保证不丢信息。
    """
    src = as_list(rec.get("来源"))
    return [(src[i], src[i + 1] if i + 1 < len(src) else "") for i in range(0, len(src), 2)]
