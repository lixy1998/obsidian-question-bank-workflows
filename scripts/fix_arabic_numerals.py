# -*- coding: utf-8 -*-
"""fix_arabic_numerals.py — 统一中文数字 → 阿拉伯数字（2026-08-22 用户规范）。

规则：
- 册别：七上/七下/八上/八下/九上/九下 → 7上/7下/8上/8下/9上/9下（纲要/选必 保持）；全文替换
- 单元：第X单元 → 第X单元（X = 一二三四五六七八九十 → 1-10）；**范围收窄**——题库 md 仅改
  YAML frontmatter 内的 `单元:` 字段，词表仅改行首 `- 第X单元` 行；正文一律不动
  （正文里的「第一单元」多为普通序数表达，如「世界近代史的第一单元」，全文替换会改写文义）
- 课：官方课名已是「第1课」，不处理
覆盖：题库 YAML（vault 由 TNOTE_VAULT 环境变量或 skill_config.json 指定）/ BOOK_INDEX.md（本技能词表）

⚠ 安全约定（2026-09-20 定，实测事故后补）：
- vault 一律走 `qb_schema.get_vault()`（环境变量 > skill_config.json > 报错），**禁止 cwd 兜底**。
  旧版写 `Path(os.environ.get("TNOTE_VAULT") or "").resolve()`——未设环境变量时解析成当前工作目录，
  若 cwd 恰为技能目录，会把技能自身的 SKILL.md / workflow / references 当题库改写（实测踩过）。
- **默认只预览（dry-run），`--apply` 才落盘**；写入前逐文件列出改动处数。
"""
import argparse
import re
import sys
from pathlib import Path

import qb_schema
from qb_bootstrap import bootstrap

SCRIPTS = Path(__file__).parent
# 词表唯一权威 = 本技能内 BOOK_INDEX.md（随 skill 分发，不依赖其他技能/个人路径）
LESSON_TITLES = SCRIPTS / "BOOK_INDEX.md"

CN = {c: str(i) for i, c in enumerate("一二三四五六七八九十", 1)}
BOOK_CN2AR = {"七上": "7上", "七下": "7下", "八上": "8上", "八下": "8下",
              "九上": "9上", "九下": "9下"}

# 全库遍历跳过**一切点开头的顶层目录**（2026-09-27 白名单起，2026-11-23 改为「点开头即跳过」）：
#   .trash      库内 Obsidian 回收站——一题多卷归档件落此，属留痕，不应被日常批处理改写
#   .obsidian   配置与插件 / .dedupe 合并日志 / .linkfix 修复日志
#   .Z-Archive  同日弃用的过渡落点，留作防御（万一残留或重建）
#   .agents / .codebuddy  指向 skill 本体的目录联接——rglob 穿透会改写 skill 自己的文档
# ⚠ 不再用显式白名单：新增点目录（如上述两种联接）会绕过白名单，故改为前缀判据。


def iter_library_md(vault: Path):
    """库内 md（跳过点开头的顶层目录，其余递归）。

    ⚠ 用「点开头」而非白名单：库根下的 `.agents`/`.codebuddy` 是**指向 skill 本体的
    目录联接**，rglob 会穿透进去把 skill 自己的文档当库文件改写（2026-11-23 实测）。
    """
    vault = Path(vault)
    for md in sorted(vault.rglob("*.md")):
        if md.relative_to(vault).parts[0].startswith("."):
            continue
        yield md


def ar_unit(m):
    """'第X单元' → '第X单元'（中文数字转阿拉伯）。"""
    return "第" + CN[m.group(1)] + "单元"


def _fix_units(text: str, scope: str):
    """按上下文收窄「第X单元」替换范围，返回 (新文本, 处数)。

    scope='frontmatter'：只改 YAML frontmatter 区（题库题的 `单元:` 字段所在），
        正文一律不动——正文里的「第一单元」多为普通序数表达（如「世界近代史的第一单元」），
        全文替换会改写文义（实测教训）。
    scope='bookline'：只改行首 `- 第X单元` 行（BOOK_INDEX 词表的单元行），同样不碰正文。
    """
    if scope == "frontmatter":
        m = re.match(r"^---\s*\n(.*?\n)---\s*\n", text, re.S)
        if not m:
            return text, 0
        fm2, k = re.subn(r"第([一二三四五六七八九十])单元", ar_unit, m.group(1))
        if not k:
            return text, 0
        return text[:m.start(1)] + fm2 + text[m.end(1):], k
    if scope == "bookline":
        out, k = [], 0
        for line in text.splitlines(keepends=True):
            if re.match(r"^\s*-\s*第[一二三四五六七八九十]单元", line):
                line, n1 = re.subn(r"第([一二三四五六七八九十])单元", ar_unit, line)
                k += n1
            out.append(line)
        return "".join(out), k
    return text, 0


def scan_md(path: Path, fix_book: bool = True, unit_scope: str = "frontmatter"):
    """扫描单个 md 文本文件，返回 (替换处数, 新文本或 None（无需改动）)。不写盘。"""
    if not path.exists():
        return 0, None
    text = path.read_text(encoding="utf-8")
    orig = text
    n = 0
    if fix_book:
        for cn, ar in BOOK_CN2AR.items():
            n += text.count(cn)
            text = text.replace(cn, ar)
    text, k = _fix_units(text, unit_scope)
    n += k
    return n, (text if text != orig else None)


def main():
    bootstrap()
    ap = argparse.ArgumentParser(
        description="统一中文数字 → 阿拉伯数字（册别 / 第X单元）；vault 取 TNOTE_VAULT 或 skill_config.json")
    ap.add_argument("--vault", default=None, help="覆盖题库库根目录（默认取环境变量或 skill_config.json）")
    ap.add_argument("--apply", action="store_true", help="实际写入文件（默认只预览，不写盘）")
    args = ap.parse_args()

    try:
        vault = Path(args.vault or qb_schema.get_vault())
    except RuntimeError as e:
        print(f"⚠ {e}")
        return 1
    if not vault.is_dir():
        print(f"⚠ 题库库目录不存在：{vault}")
        return 1

    total = 0
    changed = []
    # 1. 题库 YAML（册别 + 单元）
    for md in iter_library_md(vault):
        if md.name.endswith(("_学生版.md", "_教师版.md", "_命题说明.md")):
            continue
        n, new = scan_md(md)
        if n:
            changed.append((md, n))
            if args.apply:
                md.write_text(new, encoding="utf-8")
            total += n
    # 2. 词表（仅单元；册别标题已是阿拉伯，如 ### 7上）；范围 = 行首 `- 第X单元` 行
    n, new = scan_md(SCRIPTS / "BOOK_INDEX.md", fix_book=False, unit_scope="bookline")
    if n:
        changed.append((SCRIPTS / "BOOK_INDEX.md", n))
        if args.apply:
            (SCRIPTS / "BOOK_INDEX.md").write_text(new, encoding="utf-8")
        total += n

    for p, c in changed:
        print(f"  {c:3d} 处  {p}")
    print(f"{'已写入' if args.apply else '预览（未写盘）'}：共 {total} 处，涉及 {len(changed)} 个文件")
    if not args.apply and total:
        print("→ 确认无误后加 --apply 执行写入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
