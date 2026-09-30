# -*- coding: utf-8 -*-
"""从 BOOK_INDEX.md 生成 TNote 1-MoC-索引 试题索引（初中六册 + 高中七册，共 13 册）。

每个索引 = 1 个册别 md：
  - 单元-课分组标题 手工编写（从 BOOK_INDEX 词表读取，保持词表一致）
  - 每课下 Dataview 按「课」字段自动筛选题目，新题自动出现
  - 不生成 H1 册标题：册名由文件名承载（如 `8下-试题索引.md`、`纲要上-试题索引.md`），
    正文直接以 `## 单元` 起

两套词表结构（`load_books` 同时解析）：
  - **初中六册**：`### {册别}` → `#### 2024/2025/2026版` 版本节 → 单元/课
  - **高中七册**：`### 统编·人教社` → `#### {节标题}`（无版本节）→ 单元/课；
    节标题→册别缩写复用 `qb_schema.GAOZHONG_SECTIONS`（纲要上/下、选必1~3、选修1/2）。
    选修1/2 官方未编写教材，其「单元」为模块名、「课」为课标专题（`专题X.Y 名称`），
    解析器对二者同样适用。

词表路径/册别-版本映射复用 qb_schema（BOOK_INDEX_PATH / BOOK_INDEX_VERSION /
GAOZHONG_SECTIONS），不重复硬编码；「学史方法」栏目由独立索引文档承载
（1-MoC-索引/学史方法-试题索引.md），不在册索引内生成 (一)(二)(三) 空查询块。
"""
import argparse
import re
import sys
from pathlib import Path

import qb_schema
from qb_bootstrap import bootstrap, require_vault

BOOK = qb_schema.BOOK_INDEX_PATH          # 词表路径（随 skill 分发，勿硬编码绝对路径）
TARGET = dict(qb_schema.BOOK_INDEX_VERSION)   # 初中册别 -> 词表版本节（7上~9下）
HS_SECTION = dict(qb_schema.GAOZHONG_SECTIONS)  # 高中学段节标题 -> 册别缩写

# Dataview 数据源（按学段取对应三个类别目录）
DATA_SOURCES_MS = [f"初中-{c}" for c in qb_schema.CATEGORIES["初中"]]
DATA_SOURCES_HS = [f"高中-{c}" for c in qb_schema.CATEGORIES["高中"]]


def data_sources(册) -> list:
    """该册别对应的 Dataview FROM 数据源（初中题不会出现在高中索引里，反之亦然）。"""
    return DATA_SOURCES_MS if 册 in TARGET else DATA_SOURCES_HS


def out_dir() -> Path:
    """索引输出目录（惰性求值：qb_schema.VAULT 改用模块级 __getattr__ 后，
    模块导入期不再触碰 vault，未配置时也能 `--help`）。"""
    return require_vault() / "1-MoC-索引"
# 学史方法栏目组标题：由独立索引文档承载，册索引跳过（避免生成空查询块）
SKIP_GROUPS = {"学史方法"}


def targets() -> list:
    """[(册别, 节标识, 是否初中)]——初中用版本节，高中用节标题。"""
    out = [(b, v, True) for b, v in TARGET.items()]
    out += [(abbr, sec, False) for sec, abbr in HS_SECTION.items()]
    return out


def load_books():
    """解析 BOOK_INDEX.md → {册别: {版本节: [行...]}}（初中） + {高中节标题: [行...]}（高中）。"""
    books = {}
    cur_book = cur_ver = cur_hs = None
    for line in BOOK.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^### (\S+)", line)
        if m:
            # 高中学段的册别由 #### 节标题决定（GAOZHONG_SECTIONS），此处不建 key
            cur_book = None if m.group(1) in ("统编·人教社", "高中") else m.group(1)
            cur_ver = cur_hs = None
            if cur_book:
                books.setdefault(cur_book, {})
            continue
        m = re.match(r"^#### (.+)$", line)
        if m:
            title = m.group(1).strip()
            mv = re.match(r"^(202\d版)", title)
            if title in HS_SECTION:      # 高中：节标题即 key，值是行列表
                cur_hs, cur_ver = title, None
                books.setdefault(cur_hs, [])
            elif cur_book:               # 初中：册别 → 版本节 → 行列表
                cur_ver = mv.group(1) if mv else None
                cur_hs = None
            continue
        if cur_hs:
            books.setdefault(cur_hs, []).append(line)
        elif cur_book and cur_ver:
            books[cur_book].setdefault(cur_ver, []).append(line)
    return books


def section_lines(books, 册, 节, is_ms):
    """取该册别对应词表行：初中 books[册][版本节]，高中 books[节标题]。"""
    if is_ms:
        return books.get(册, {}).get(节)
    return books.get(节)


def parse_groups(lines):
    """返回 [(组标题, [课条目, ...]), ...]

    组标题 = 单元 / 活动课 / 学史方法（缩进0）
    课条目 = 第X课 / 活动课 / 专题X.Y（缩进2）；活动课顶格时自身作为唯一条目
    缩进 ≥4 为课的**子目**（①…），不生成查询块——子目不写在题目的 `课` 字段里
    （题目的课 = 第X课/专题X.Y，子目另有 `小节` 字段承载）。
    """
    groups = []
    for raw in lines:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if not content.startswith("- "):
            continue
        name = content[2:].strip()
        if not name:
            continue
        if indent == 0:
            if name.startswith("附录"):
                # 「附录 朝代表、大事年表」这类非课内容不建索引节（用户裁定：不需要）；
                # 词表里也不应再写这类行，此处是防回归守卫
                continue
            if name.startswith("活动课"):
                groups.append([name, [name]])  # 活动课本身即课条目
            else:
                groups.append([name, []])
        elif indent == 2:
            if groups:
                groups[-1][1].append(name)
        # indent >= 4：子目（①…），忽略
    return groups


def dv_query(册, 课名):
    """Dataview DQL 函数语法：`contains(字段, 值)`，参数必须双引号。
    DQL 里 `CONTAINS` 是函数而非操作符，「字段 CONTAINS 值」写法不被 parser 识别。
    中文引号（“”）在双引号字符串内是普通字符，无害。

    ⚠ id 为多值 list（一题多卷）：题号列直接显示 `id`——多卷收录的题会显示
    「主卷id, 别名卷id」一串，正是所需信息；**排序不能用 `SORT id`（对 list 排序语义不明）**，
    改用 `SORT file.name ASC`（文件名=零填充序号，等价于按题号排序）。
    """
    src = " OR ".join(f'"{s}"' for s in data_sources(册))
    safe = 课名.replace('"', '\\"')  # 兜底 ASCII 引号转义，当前词表无
    return (
        "```dataview\n"
        "TABLE id AS 题号, 课, 知识点, 题型, 素养层次水平, 关键能力\n"
        f"FROM {src}\n"
        f'WHERE contains(册别, "{册}") AND contains(课, "{safe}")\n'
        "SORT file.name ASC\n"
        "```"
    )


def build_index(册, 节, is_ms=True):
    groups = parse_groups(section_lines(load_books(), 册, 节, is_ms))
    L = []
    L.append("---")
    L.append("tags: [MoC, 试题索引]")
    L.append("---")
    L.append("")
    L.append(f"> 题库中 **{册}** 全部试题，按教材「单元-课」分组呈现。")
    L.append("> - 单元-课分组标题为**手工编写**（与 BOOK_INDEX 课名词表一致）；每课下的题目由 **Dataview 按「课」字段自动筛选**——新题录入后自动出现在对应课下，无需改本页。")
    L.append("> - 新增课程：在对应单元末尾补一个 `### 课名` 标题 + 下方 Dataview 查询块（复制邻近课的块，改课名即可）。")
    L.append("> - 跨课/跨册题目会同时出现在其命中的多个课块下。")
    L.append("")
    for gtitle, items in groups:
        if gtitle in SKIP_GROUPS:
            continue  # 学史方法等栏目由独立索引文档承载，册索引不生成空块
        L.append(f"## {gtitle}")
        L.append("")
        if not items:
            L.append("_（无课条目）_")
            L.append("")
        for item in items:
            L.append(f"### {item}")
            L.append("")
            L.append(dv_query(册, item))
            L.append("")
    return "\n".join(L)


def main():
    bootstrap()
    ap = argparse.ArgumentParser(
        description="从 BOOK_INDEX.md 重建 1-MoC-索引 试题索引（初中 6 册 + 高中 7 册，幂等：内容不变则不产生差异）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘")
    ap.add_argument("--force", action="store_true",
                    help="检测到词表外的标题（疑手工新增）时仍强制覆盖；默认中止该册并提示")
    args = ap.parse_args()

    books = load_books()
    OUT = out_dir()
    OUT.mkdir(parents=True, exist_ok=True)
    for 册, 节, is_ms in targets():
        lines = section_lines(books, 册, 节, is_ms)
        if not lines:
            print(f"[跳过] {册} 无 {节} 词表节")
            continue
        # 手工内容保护：现有文件里不在词表内的 ## / ### 标题，可能是手工补的组或课
        groups = parse_groups(lines)
        known = {g for g, _ in groups} | {i for _, items in groups for i in items}
        fp = OUT / f"{册}-试题索引.md"
        old = fp.read_text(encoding="utf-8") if fp.exists() else None
        manual = ([t for t in re.findall(r"^#{2,3} (.+)$", old, re.M) if t not in known]
                  if old else [])
        if manual and not args.force:
            print(f"[中止] {fp.name} 检出 {len(manual)} 个词表外标题（疑手工新增）：{'、'.join(manual[:5])}")
            print("       确认可覆盖后加 --force 重跑；或先把这些课/组并入 BOOK_INDEX 词表。")
            continue
        text = build_index(册, 节, is_ms)
        n_groups = text.count("\n## ")
        n_blocks = text.count("```dataview")
        if args.dry_run:
            # 行尾无关比较：Obsidian 打开过的索引会被规范化成 CRLF，而本脚本写 LF——
            # 逐字节比会把「仅行尾不同」误报成「将有变动」（实测纲要上/纲要下即此）。
            same = (old is not None
                    and old.replace("\r\n", "\n") == text.replace("\r\n", "\n"))
            print(f"[预览] {fp.name} ｜ 分组 {n_groups} ｜ Dataview 块 {n_blocks} ｜ "
                  f"{'与现状一致' if same else '将有变动'}")
            continue
        fp.write_text(text, encoding="utf-8")
        print(f"[生成] {fp.name} ｜ 分组 {n_groups} ｜ Dataview 块 {n_blocks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
