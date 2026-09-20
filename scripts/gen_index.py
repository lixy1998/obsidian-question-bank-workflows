# -*- coding: utf-8 -*-
"""从 BOOK_INDEX.md 生成 TNote 1-MoC-索引 六册试题索引。

每个索引 = 1 个册别 md：
  - 单元-课分组标题 手工编写（从 BOOK_INDEX 词表读取，保持词表一致）
  - 每课下 Dataview 按「课」字段自动筛选题目，新题自动出现
  - 不生成 H1 册标题：册名由文件名承载（如 `8下-试题索引.md`），正文直接以 `## 单元` 起

词表路径/册别-版本映射复用 qb_schema（BOOK_INDEX_PATH / BOOK_INDEX_VERSION），
不重复硬编码；「学史方法」栏目由独立索引文档承载（1-MoC-索引/学史方法-试题索引.md），
不在册索引内生成 (一)(二)(三) 空查询块。
"""
import argparse
import re
import sys
from pathlib import Path

import qb_schema

BOOK = qb_schema.BOOK_INDEX_PATH          # 词表路径（随 skill 分发，勿硬编码绝对路径）
OUT = Path(qb_schema.VAULT) / "1-MoC-索引"  # TNote 索引目录
TARGET = dict(qb_schema.BOOK_INDEX_VERSION)  # 册别 -> 词表版本节（7上~9下 初中六册）
DATA_SOURCES = [f"初中-{c}" for c in qb_schema.CATEGORIES["初中"]]
# 学史方法栏目组标题：由独立索引文档承载，册索引跳过（避免生成空查询块）
SKIP_GROUPS = {"学史方法"}


def load_books():
    """解析 BOOK_INDEX.md → {册别: {版本: [行...]}}"""
    books = {}
    cur_book = cur_ver = None
    for line in BOOK.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^### (\S+)", line)
        if m:
            cur_book, cur_ver = m.group(1), None
            books.setdefault(cur_book, {})
            continue
        m = re.match(r"^#### (202\d版)", line)
        if m:
            cur_ver = m.group(1)
            continue
        if cur_book and cur_ver:
            books[cur_book].setdefault(cur_ver, []).append(line)
    return books


def parse_groups(lines):
    """返回 [(组标题, [课条目, ...]), ...]

    组标题 = 单元 / 活动课 / 学史方法（缩进0）
    课条目 = 第X课 / 活动课 / (一)等方法（缩进2）；活动课顶格时自身作为唯一条目
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
            if name.startswith("活动课"):
                groups.append([name, [name]])  # 活动课本身即课条目
            else:
                groups.append([name, []])
        elif indent == 2:
            if groups:
                groups[-1][1].append(name)
    return groups


def dv_query(册, 课名):
    """Dataview DQL 函数语法：`contains(字段, 值)`，参数必须双引号。
    纠正先前错误理解：CONTAINS 不是 DQL 操作符而是函数，技能示例的「字段 CONTAINS 值」写法不被 Dataview parser 识别。
    中文引号（“”）在双引号字符串内是普通字符，无害。

    ⚠ id 为多值 list（一题多卷，v1.3.0 起）：题号列直接显示 `id`——多卷收录的题会显示
    「主卷id, 别名卷id」一串，正是所需信息；**排序不能用 `SORT id`（对 list 排序语义不明）**，
    改用 `SORT file.name ASC`（文件名=零填充序号，等价于按题号排序）。
    """
    src = " OR ".join(f'"{s}"' for s in DATA_SOURCES)
    safe = 课名.replace('"', '\\"')  # 兜底 ASCII 引号转义，当前词表无
    return (
        "```dataview\n"
        "TABLE id AS 题号, 课, 知识点, 题型, 素养层次水平, 关键能力\n"
        f"FROM {src}\n"
        f'WHERE contains(册别, "{册}") AND contains(课, "{safe}")\n'
        "SORT file.name ASC\n"
        "```"
    )


def build_index(册, 版本):
    groups = parse_groups(load_books()[册][版本])
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
    ap = argparse.ArgumentParser(
        description="从 BOOK_INDEX.md 重建 TNote 1-MoC-索引 六册试题索引（幂等：内容不变则不产生差异）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘")
    ap.add_argument("--force", action="store_true",
                    help="检测到词表外的标题（疑手工新增）时仍强制覆盖；默认中止该册并提示")
    args = ap.parse_args()

    books = load_books()
    OUT.mkdir(parents=True, exist_ok=True)
    for 册, 版本 in TARGET.items():
        if 册 not in books or 版本 not in books[册]:
            print(f"[跳过] {册} 无 {版本} 词表节")
            continue
        # 手工内容保护：现有文件里不在词表内的 ## / ### 标题，可能是手工补的组或课
        groups = parse_groups(books[册][版本])
        known = {g for g, _ in groups} | {i for _, items in groups for i in items}
        fp = OUT / f"{册}-试题索引.md"
        old = fp.read_text(encoding="utf-8") if fp.exists() else None
        manual = ([t for t in re.findall(r"^#{2,3} (.+)$", old, re.M) if t not in known]
                  if old else [])
        if manual and not args.force:
            print(f"[中止] {fp.name} 检出 {len(manual)} 个词表外标题（疑手工新增）：{'、'.join(manual[:5])}")
            print("       确认可覆盖后加 --force 重跑；或先把这些课/组并入 BOOK_INDEX 词表。")
            continue
        text = build_index(册, 版本)
        n_groups = text.count("\n## ")
        n_blocks = text.count("```dataview")
        if args.dry_run:
            print(f"[预览] {fp.name} ｜ 分组 {n_groups} ｜ Dataview 块 {n_blocks} ｜ "
                  f"{'与现状一致' if old == text else '将有变动'}")
            continue
        fp.write_text(text, encoding="utf-8")
        print(f"[生成] {fp.name} ｜ 分组 {n_groups} ｜ Dataview 块 {n_blocks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
