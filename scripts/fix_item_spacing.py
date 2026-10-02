# -*- coding: utf-8 -*-
"""fix_item_spacing.py — 报告真题引用块内的段落间距压缩。

规则（2026-10-02 用户定，见 workflow/04 §6c）：
    真题标题行（`**【{类型}】[[…]]（题型 · 分值）**`）与题干**紧连，不加空行**；
    内容内部（题干、图片占位、选项、小问、答案/点拨）**紧连，不加空行**；
    真题与真题之间（即块尾与下一个标题行/后续段落之间）**保留空行**。

目标形态：

    **【中考真题】[[…|2026开封一模 第 16 题]]（选择 · 1 分）**
    1649 年，英国议会下令拆解王权礼器；…这一过程表明英国资产阶级革命
    （A）恢复了封建专制统治　（B）具有曲折性与妥协性特点　（C）…
    ✅ 答案：B　💡 点拨：…
    <空行>
    **【阶段测试】…**

⚠ 只压缩**真题引用块内部**；正文其他段落（怎么考 / 识别口诀 / 教学落点等）的间距不动。

⚠ **表格保护（硬约束）**：md 表格必须与上一行空行隔断，否则整段塌成文字
   （见 workflow/01 陷阱 21 第一类）。故凡「空行的下一行是 md 表格首行
   （`| … |` 且再下一行是 `| --- |` 分隔行）」者，该空行**必须保留**。

块边界判定：
    块起 = 真题标题行（类型＝ 中考真题/中考模拟题/阶段测试/高考真题/高考模拟题，
           兼容旧写法的 【真题 N】【真题】/【真题·xxx】【补充】【变式N】【拓展N】）
    块止 = 标题行后，遇「空行 + 下一非空行为块外内容」处
           块外内容 = 下一个真题标题 / `**加粗段落**` / `#` 章节标题 / `---` 分隔线 / 引用块

用法：
    python fix_item_spacing.py --scan            # 勘察
    python fix_item_spacing.py                   # dry-run
    python fix_item_spacing.py --apply           # 执行（逐文件备份 .bak-space）
    python fix_item_spacing.py --file X          # 单文件

退出码：0 正常；1 有阻断性错误。
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

# 真题标题行：**【{类型}】…**（类型＝库类别短名）＋ 旧写法兼容（【真题 N】【补充】【变式N】【拓展N】）
# ⚠ 与 fix_quote_to_plain.py 的同名正则**必须保持一致**，改一处即改两处。
ITEM_RE = re.compile(r"^\*\*【(?:真题(?:\s*\d+|[··][^】]*)?|补充(?:\s*\d+)?|变式\s*\d+|拓展\s*\d+|中考真题|中考模拟题|高考真题|高考模拟题|阶段测试)】")
# 块外内容标志（下一非空行属这些即认为本块结束）
OUTSIDE_RE = re.compile(r"^(?:\*\*【|#{1,6}\s|---|\*{2}[^*]|>)")
# md 表格首行 / 分隔行（表格前的空行必须保留）
ROW_RE = re.compile(r"^\s*\|")
SEP_RE = re.compile(r"^\|[\s:|-]+\|\s*$")
SKIP_PARTS = {".trash", "_备份"}
SKIP_SUFFIX = (".bak-tblfix", ".bak-unquote", ".bak-space", ".bak-20261002", ".bak-quotetable")


def is_table_start(lines, i):
    """lines[i] 是否为 md 表格首行（即下一行是分隔行）。"""
    if i >= len(lines) or not ROW_RE.match(lines[i]):
        return False
    return i + 1 < len(lines) and bool(SEP_RE.match(lines[i + 1]))



def load_vault(explicit=None):
    if explicit:
        return Path(explicit)
    for cand in (Path(r"D:/Documents/TNote"),):
        if cand.exists():
            return cand
    sys.exit("未找到 vault，请用 --vault 指定")


def iter_md(vault, only_dirs=None):
    for p in vault.rglob("*.md"):
        if any(s in part for s in SKIP_PARTS for part in p.parts):
            continue
        if p.name.endswith(SKIP_SUFFIX):
            continue
        if only_dirs and not any(d in p.parts for d in only_dirs):
            continue
        yield p


def find_blocks(lines):
    """返回 [(title_idx, content_start, content_end_exclusive), ...]

    content_start = 标题行之后第一个非空行（题干首行）
    content_end   = 块内容末尾（不含其后空行）
    """
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        if not ITEM_RE.match(lines[i]):
            i += 1
            continue
        # 内容首行 = 标题行之后（允许标题后有空行，它们也属块内、待删）
        j = i + 1
        # 找到第一个非空行（用于判块是否为空块）
        m0 = j
        while m0 < n and not lines[m0].strip():
            m0 += 1
        if m0 >= n:
            break
        # 若标题后紧接的是块外内容（无题干），跳过
        if OUTSIDE_RE.match(lines[m0]):
            i += 1
            continue
        # 从 j 扫到块尾（含标题后空行）
        k = j
        last_content = i
        while k < n:
            if not lines[k].strip():
                m = k + 1
                while m < n and not lines[m].strip():
                    m += 1
                if m >= n:
                    break
                if OUTSIDE_RE.match(lines[m]):
                    break            # 块结束（空行 + 块外内容）
                k = m                 # 仍是块内内容：空行待删
                continue
            last_content = k
            k += 1
        blocks.append((i, j, last_content + 1))
        i = last_content + 1
    return blocks


def compact(lines, cs, ce):
    """压缩 [cs, ce) 内空行；表格前空行保留。返回新片段。"""
    inner = lines[cs:ce]
    out = []
    for idx, ln in enumerate(inner):
        if ln.strip():
            out.append(ln)
            continue
        # 空行：判断下一个非空行是否表格首行
        m = idx + 1
        while m < len(inner) and not inner[m].strip():
            m += 1
        if m < len(inner) and is_table_start(inner, m):
            out.append(ln)        # 保护：表格前的空行必须留
    return out


def process(path, apply, scan=False):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    blocks = find_blocks(lines)
    if not blocks:
        return 0, 0, []

    detail = []
    total_removed = 0
    for (t, cs, ce) in reversed(blocks):
        inner = lines[cs:ce]
        kept = compact(lines, cs, ce)
        removed = len(inner) - len(kept)
        if scan:
            detail.append((t + 1, lines[t][:60], len(inner), len(kept), removed))
            total_removed += removed
            continue
        if removed:
            lines[cs:ce] = kept
        total_removed += removed

    if scan:
        detail.reverse()
        return len(blocks), total_removed, detail

    if total_removed and apply:
        shutil.copy2(path, str(path) + ".bak-space")
        path.write_text("\n".join(lines), encoding="utf-8")
    return len(blocks), total_removed, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--vault", default=None)
    ap.add_argument("--file", default=None)
    ap.add_argument("--dir", default=None)
    args = ap.parse_args()

    vault = load_vault(args.vault)
    if args.file:
        files = [Path(args.file)]
        base = Path(args.file).parent
    else:
        only_dirs = [args.dir] if args.dir else None
        files = list(iter_md(vault, only_dirs))
        base = vault

    tf = tb = tr = 0
    for p in files:
        nb, rem, detail = process(p, args.apply, args.scan)
        if not nb:
            continue
        tf += 1
        tb += nb
        tr += rem
        try:
            rel = p.relative_to(base)
        except ValueError:
            rel = p
        if args.scan:
            print(f"\n=== {rel}（{nb} 块，可删空行 {rem}）===")
            for (ln, head, before, after, r) in detail:
                flag = "*" if r else " "
                print(f"  L{ln:<5}{flag} {before:>3}→{after:<3} 行  删{r:<2}  {head}")
        else:
            print(f"[{nb} 块，删空行 {rem}] {rel}")

    mode = "SCAN" if args.scan else ("已执行" if args.apply else "DRY-RUN（未写入）")
    print(f"\n{mode}：{tf} 个文件；真题块 {tb} 个，可删/已删空行 {tr} 行")
    if not args.apply and not args.scan and tr:
        print("确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
