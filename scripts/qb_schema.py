# -*- coding: utf-8 -*-
"""Obsidian历史题库搭建 公共模块：受控词表 + 路径规则 + 通用函数。

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


VAULT = get_vault()

# ---- 受控词表 ----
STAGES = ["初中", "高中"]
# 类别 = 目录序号前缀 + 类别名（如 初中-1中考真题）；原创/改编不占目录，走「来源」标签
CATEGORIES = {
    "初中": ["1中考真题", "2中考模拟题", "3阶段测试"],
    "高中": ["1高考真题", "2高考模拟题", "3阶段测试"],
}
BOOKS = ["7上", "7下", "8上", "8下", "9上", "9下",
         "纲要上", "纲要下", "选必1", "选必2", "选必3"]
TYPES = ["选择题", "材料解析题", "论述题", "综合探究题", "小论文", "填空题"]
DIFFICULTIES = ["易", "中", "难"]
# 来源标签：出处标签自由（卷名/教材课名），类型标签建议受控：
# 真题 / 教材改编 / 中考改编 / 原创题 / 期末卷 / 模拟卷（可扩展）
SOURCE_TAG_HINTS = ["真题", "教材改编", "中考改编", "原创题", "期末卷", "模拟卷"]

# 导出/生成必填字段（小节/知识点/来源标签可空或单值）
REQUIRED_FIELDS = ["id", "册别", "单元", "课", "题型", "难度",
                   "分值", "来源", "年份", "考区", "答案"]


def stage_roots() -> list:
    """vault 根下所有题库目录（初中-* / 高中-*）。"""
    return sorted(p for p in Path(VAULT).iterdir()
                  if p.is_dir() and re.match(r"^(初中|高中)-", p.name))


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
    validate_enum("题型", rec["题型"], TYPES)
    validate_enum("难度", rec["难度"], DIFFICULTIES)


# ---- BOOK_INDEX 课名词表 ----
BOOK_INDEX_PATH = Path(__file__).parent / "BOOK_INDEX.md"
_BOOK_CACHE = None
_METHOD_CACHE = None
# 学史方法（全六册）课名前缀：课名写作 `学史方法-<完整方法名>`，
# 校验只查方法名是否在「## 初中·学史方法（全六册）」15 表内（不按册别，见 load_method_names）
METHOD_PREFIX = "学史方法-"

# 册别 -> 校验用词表版本节（按教材首印定版年份；首用后仅小修订、目录无变化，不建新版词表）
# 7上/7下 2024 首印定版（2024版）；8上/8下 2025 首印定版（2025版）；9上/9下 2026 首印定版（2026版）
# 高中册别（纲要/选必）暂无词表节 -> 不收录 -> 校验跳过（不误报）
BOOK_INDEX_VERSION = {
    "7上": "2024版", "7下": "2024版",
    "8上": "2025版", "8下": "2025版",
    "9上": "2026版", "9下": "2026版",
}


def load_book_index() -> dict:
    """解析 BOOK_INDEX.md 各册别对应版本节 → {册别缩写: [课名全称, ...]}。

    每个册别只收录 `BOOK_INDEX_VERSION` 指定的版本节（如 7上/7下 的 2024版节、
    8上/8下 的 2025版节、9上/9下 的 2026版节）内的「第X课/活动课」行；册别缩写如 9上/9下。
    未在映射内的册别（高中）不参与题库校验。
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
        if re.match(r"^#### ", line):
            # 仅当本册别映射的版本节（如 "#### 2024版…"）才收录，其余版本/高中节跳过
            ver = re.match(r"^#### (20\d\d版)", line)
            in_target = bool(cur_book and ver
                             and ver.group(1) == BOOK_INDEX_VERSION.get(cur_book))
            continue
        if in_target and cur_book:
            m2 = re.match(r"^ {0,2}- (第\d+课 .+|活动课 .+)", line)  # 活动课顶格、正课缩进2格
            if m2:
                index.setdefault(cur_book, []).append(m2.group(1).strip())
    _BOOK_CACHE = index
    return index


def load_method_names() -> set:
    """解析 BOOK_INDEX「初中·学史方法（全六册）」表格 → {方法名, ...}。

    该栏目不属于任何册别词表节，以表格总列 15 个方法（`## 初中·学史方法（全六册）`，
    行形如 `| 1 | 如何从出土文物中获取信息 | 7上 |`）。题库课名规范写作
    `学史方法-<完整方法名>`，校验时只比对方法名（**不按册别**）。
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
                    names.add(m.group(1).strip())
    _METHOD_CACHE = names
    return names


def validate_lesson(rec: dict) -> None:
    """校验课名是否在 BOOK_INDEX 词表中（课/册别支持多值）。

    策略：
    - `学史方法-<方法名>`：只校验方法名是否在「初中·学史方法（全六册）」15 表内（**不按册别**——
      各册分配尚待确认，且中高考题常按「史事 + 方法维度」跨册标注，严格按册会误伤）；
    - 册别列表中某册别收录了词表（7上~9下按 BOOK_INDEX_VERSION 映射版本节）→ 该册别课程名须命中词表；
    - 册别列表中若存在未收录词表的册别（如 纲要上/选必1 等高中）→ 无法校验的课名视为通过（不误报）；
    - 全部册别均未收录词表 → 跳过课名校验（学史方法的方法名校验不受此影响）。
    """
    books = as_list(rec.get("册别"))
    lessons = as_list(rec.get("课"))
    if not books or not lessons:
        return
    methods = load_method_names()
    rest = []
    for lesson in lessons:
        if lesson.startswith(METHOD_PREFIX):
            name = lesson[len(METHOD_PREFIX):].strip()
            if methods and name not in methods:
                raise SchemaError(
                    f"学史方法名不在 BOOK_INDEX「初中·学史方法（全六册）」表中：'{name}'"
                    f"（共 {len(methods)} 个；规范写法 `{METHOD_PREFIX}<完整方法名>`）")
            continue
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
    d = Path(VAULT) / f"{rec['学段']}-{rec['类别']}" / rec["卷名"]
    return d / f"{rec['卷名']}_{rec['题型']}"


def question_path(rec: dict) -> Path:
    """题目文件完整路径：{卷名}_{题型}/{序号}.md"""
    return question_dir(rec) / f"{rec['序号']}.md"


def build_body(rec: dict) -> str:
    """由 题干(+选项) 生成正文。选择题：题干 + (A)(B)(C)(D)；材料题：题干原文。

    图片自动插图（2026-08-26 起）：rec["图片"] = [{"src": 源路径, "name": "21_1.jpg"}, ...]
    正文尾部自动追加 `![](images/名字)` 引用；题干已含同名引用（手写 `![](images/…)`）则不重复追加。
    行为与「图片字段负责复制文件、题干负责引用」的旧约定兼容，且选择题不再依赖题干手写引用。
    """
    stem = str(rec.get("题干", "")).strip()
    opts = rec.get("选项") or []
    if opts:
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
# 重复题以主卷为准唯一存题，别名题归档到 {vault}/_归档/重复题/（详见 workflow/01-input.md「查重与合并」）
ARCHIVE_ROOT = "_归档"            # vault 根下归档目录（不在 初中-*/高中-* 内，scan 天然不扫描）
ARCHIVE_DUP_SUBDIR = "重复题"      # 归档根下子目录
DEDUPE_LOG_DIR = ".dedupe"        # 合并日志目录（vault 根下）

# 权威序（判「以谁为准」，数值越小越权威；同级按年份早者优先）
# 一级：类别目录（中/高考真题 0 > 中/高考模拟 1 > 阶段测试 2）
AUTHORITY_CATEGORY = {"1中考真题": 0, "1高考真题": 0,
                      "2中考模拟题": 1, "2高考模拟题": 1, "3阶段测试": 2}
# 二级：阶段测试卷名细分（期末早于期中/月考）
AUTHORITY_PATTERNS = [(0, r"一模|二模|三模|联考|模拟"), (1, r"期末"), (2, r"期中|月考|单元|阶段")]


def is_question_file(p) -> bool:
    """题库题目文件判定：排除 README、导出卷（_学生版/_教师版）与下划线前缀辅助文件
    （如 `_重复题清单.md`）；归档目录因不在 初中-*/高中-* 下，无需额外排除。"""
    p = Path(p)
    if p.name == "README.md" or p.name.startswith("_"):
        return False
    if p.name.endswith(("_学生版.md", "_教师版.md")):
        return False
    return True


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
