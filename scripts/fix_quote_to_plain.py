# -*- coding: utf-8 -*-
"""fix_quote_to_plain.py — 报告真题引用块降为普通段落（去 `>` 前缀）。

背景（2026-10-02）：
    报告真题引用采用如下形态——

        **【中考真题】[[...|某卷 第 24 题]]（论述 · 6 分）**
        <空行>
        > 阅读下表……
        >
        > | 国家 | 法律文献 |
        > | --- | --- |
        > | 英国 | 《权利法案》 |
        >
        > (A) ……

    上方加粗行已给出题目来源，下方引用块纯属冗余；且引用块内**不能渲染 md 表格**
    （Obsidian 限制），迫使表格必须写 HTML，单元格内又不能用 wikilink / 图片。
    因此改为：去掉 `>` 前缀，真题内容降为普通段落，表格还原为 md 表格。

识别与转换规则（两种形态）：
    【形态甲】标题在引用块外：
        **【中考真题】[[...|某卷 第 24 题]]（论述 · 6 分）**
        <空行>
        > 阅读下表……
    【形态乙】来源标题在引用块内（河南中考/解析与检测两份设问类型报告）：
        > **【中考真题】[[...|2022河南中考 第 23 题]]（节选）】**
        >
        > 材料一……

    转换：
    1. 引用块整段去 `>` 前缀，`>` 空行 → 真空行。
    2. 块内 HTML `<table>…</table>` 还原为 md 表格（含表格前后补空行）。
    3. 文档扉页元信息块（`> 数据来源：…`）与其他非真题引用块**不动**。

用法：
    python fix_quote_to_plain.py --scan            # 勘察：列出所有真题引用块
    python fix_quote_to_plain.py                   # dry-run
    python fix_quote_to_plain.py --apply           # 执行（逐文件备份 .bak-unquote）
    python fix_quote_to_plain.py --apply --file X  # 只处理单个文件

退出码：0 正常；1 有阻断性错误。
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

QUOTE_RE = re.compile(r"^\s*>")
# 真题引用标题行：**【{类型}】…** / **【真题 N】…** / **【补充】…** / **【变式 2】…**
# （类型＝库类别短名：中考真题/中考模拟题/阶段测试/高考真题/高考模拟题；旧写法一并兼容）
# ⚠ 与 fix_item_spacing.py 的同名正则**必须保持一致**，改一处即改两处。
ITEM_RE = re.compile(r"^\*\*【(?:真题(?:\s*\d+|[··][^】]*)?|补充(?:\s*\d+)?|变式\s*\d+|拓展\s*\d+|中考真题|中考模拟题|高考真题|高考模拟题|阶段测试)】")
# 形态乙：引用块首行本身就是来源标题 `> **【[[...]]（节选）】**`
QUOTE_ITEM_RE = re.compile(r"^\s*>\s*\*\*【")
SEP_RE = re.compile(r"^\|[\s:|-]+\|\s*$")
SKIP_PARTS = {".trash", "_备份"}
SKIP_SUFFIX = (".bak-tblfix", ".bak-unquote", ".bak-20261002", ".bak-quotetable")


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


def strip_quote(ln):
    return re.sub(r"^\s*>\s?", "", ln)


def find_item_quotes(lines):
    """返回 [(标题行索引, 引用块始, 引用块终_exclusive), ...]

    形态甲：`**【真题N】...**` 标题行 → 空行 → 引用块；标题行索引 = 标题所在行。
    形态乙：引用块首行即 `> **【...】**`；标题行索引 = 引用块首行。
    """
    out = []
    i = 0
    while i < len(lines):
        # 形态甲
        if ITEM_RE.match(lines[i]):
            j = i + 1
            blanks = 0
            while j < len(lines) and not lines[j].strip():
                j += 1
                blanks += 1
                if blanks > 1:
                    break
            if j < len(lines) and QUOTE_RE.match(lines[j]):
                k = j
                while k < len(lines) and QUOTE_RE.match(lines[k]):
                    k += 1
                out.append((i, j, k))
                i = k
                continue
            i += 1
            continue
        # 形态乙
        if QUOTE_ITEM_RE.match(lines[i]):
            k = i
            while k < len(lines) and QUOTE_RE.match(lines[k]):
                k += 1
            out.append((i, i, k))
            i = k
            continue
        i += 1
    return out


def html_to_md(block):
    """把引用块（已去 `>`）内的 <table>…</table> 还原为 md 表格。"""
    out = []
    i = 0
    while i < len(block):
        ln = block[i]
        if "<table>" in ln:
            html = ln
            j = i
            while "</table>" not in html and j + 1 < len(block):
                j += 1
                html += "\n" + block[j]
            rows = parse_html_rows(html)
            if rows:
                # 前后确保空行
                if out and out[-1].strip():
                    out.append("")
                md = md_table(rows)
                out.extend(md)
                out.append("")
            i = j + 1
        else:
            out.append(ln)
            i += 1
    # 折叠连续空行
    res = []
    for ln in out:
        if not ln.strip() and res and not res[-1].strip():
            continue
        res.append(ln)
    return res


def parse_html_rows(html):
    rows = []
    for m in re.finditer(r"<tr>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<t[hd]>(.*?)</t[hd]>", m.group(1), re.S)
        if cells:
            rows.append([c.strip() for c in cells])
    return rows


def md_table(rows):
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join("---" for _ in head) + " |"]
    for r in body:
        r = (r + [""] * len(head))[:len(head)]
        out.append("| " + " | ".join(r) + " |")
    return out


def process(path, apply, scan=False):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    spans = find_item_quotes(lines)
    if not spans:
        return 0, 0, []

    if scan:
        info = []
        for (t, a, b) in spans:
            has_html = any("<table>" in x for x in lines[a:b])
            info.append((t + 1, lines[t][:80], has_html, b - a))
        return len(spans), sum(1 for x in info if x[2]), info

    n_html = 0
    for (t, a, b) in reversed(spans):
        block = [strip_quote(x) for x in lines[a:b]]
        # 去尾空格行
        while block and not block[-1].strip():
            block.pop()
        if any("<table>" in x for x in block):
            n_html += 1
        new = html_to_md(block)
        lines[a:b] = new

    # 折叠块内外相接处产生的连续空行（保留 1 个）
    cleaned = []
    for ln in lines:
        if not ln.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(ln)
    lines = cleaned

    if apply:
        shutil.copy2(path, str(path) + ".bak-unquote")
        path.write_text("\n".join(lines), encoding="utf-8")
    return len(spans), n_html, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--vault", default=None)
    ap.add_argument("--file", default=None, help="只处理单个文件")
    ap.add_argument("--dir", default=None, help="只处理某子目录（如 3-试题研究）")
    args = ap.parse_args()

    vault = load_vault(args.vault)
    if args.file:
        files = [Path(args.file)]
        base = Path(args.file).parent
    else:
        only_dirs = [args.dir] if args.dir else None
        files = list(iter_md(vault, only_dirs))
        base = vault

    tf = tq = th = 0
    for p in files:
        n, nh, info = process(p, args.apply, args.scan)
        if not n:
            continue
        tf += 1
        tq += n
        th += nh
        try:
            rel = p.relative_to(base)
        except ValueError:
            rel = p
        if args.scan:
            print(f"\n=== {rel}（{n} 块，含表格 {nh}）===")
            for (ln, head, h, size) in info:
                print(f"  L{ln:<5} {'[表]' if h else '    '} {size:>3} 行  {head}")
        else:
            print(f"[{n} 块" + (f"，还原表格 {nh}" if nh else "") + f"] {rel}")

    mode = "SCAN" if args.scan else ("已执行" if args.apply else "DRY-RUN（未写入）")
    print(f"\n{mode}：{tf} 个文件；真题引用块 {tq} 块，其中含表格 {th} 块")
    if not args.apply and not args.scan and tf:
        print("确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
