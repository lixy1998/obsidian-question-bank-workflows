# -*- coding: utf-8 -*-
"""fix_arabic_numerals.py — 统一中文数字 → 阿拉伯数字（2026-08-22 用户规范）。

规则：
- 册别：七上/七下/八上/八下/九上/九下 → 7上/7下/8上/8下/9上/9下（纲要/选必 保持）
- 单元：第X单元 → 第X单元（X = 一二三四五六七八九十 → 1-10）
- 课：官方课名已是「第1课」，不处理
覆盖：题库 YAML（vault 由 TNOTE_VAULT 环境变量指定）/ BOOK_INDEX.md（本技能词表）
"""
import os
import re
from pathlib import Path

VAULT = Path(os.environ.get("TNOTE_VAULT") or "").resolve()  # 未设置时 rglob 不会匹配，报错提示见下
SCRIPTS = Path(__file__).parent
# 词表唯一权威 = 本技能内 BOOK_INDEX.md（随 skill 分发，不依赖其他技能/个人路径）
LESSON_TITLES = SCRIPTS / "BOOK_INDEX.md"

CN = {c: str(i) for i, c in enumerate("一二三四五六七八九十", 1)}
BOOK_CN2AR = {"七上": "7上", "七下": "7下", "八上": "8上", "八下": "8下",
              "九上": "9上", "九下": "9下"}


def ar_unit(m):
    """'第X单元' → '第X单元'（中文数字转阿拉伯）。"""
    return "第" + CN[m.group(1)] + "单元"


def fix_md(path: Path, fix_book: bool = True, fix_unit: bool = True) -> int:
    """修正单个 md 文本文件，返回替换次数。"""
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    orig = text
    n = 0
    if fix_book:
        for cn, ar in BOOK_CN2AR.items():
            n += text.count(cn)
            text = text.replace(cn, ar)
    if fix_unit:
        text, k = re.subn(r"第([一二三四五六七八九十])单元", ar_unit, text)
        n += k
    if text != orig:
        path.write_text(text, encoding="utf-8")
    return n


def main():
    if not VAULT.is_dir():
        print(f"⚠ 题库库目录不存在：{VAULT}")
        print("请先设置 TNOTE_VAULT 环境变量指向 Obsidian 题库库根目录（如 TNOTE_VAULT=<题库库路径>）")
        return
    total = 0
    # 1. 题库 YAML（册别 + 单元）
    for md in VAULT.rglob("*.md"):
        if md.name.endswith(("_学生版.md", "_教师版.md")):
            continue
        total += fix_md(md)
    # 2. 词表（仅单元；册别标题已是阿拉伯，如 ### 7上）
    total += fix_md(SCRIPTS / "BOOK_INDEX.md", fix_book=False)
    print(f"完成：共替换 {total} 处（中文数字 → 阿拉伯数字）")


if __name__ == "__main__":
    main()
