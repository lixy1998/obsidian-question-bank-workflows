# -*- coding: utf-8 -*-
"""fix_table_render.py — 修复 Obsidian 表格不渲染的两类结构问题。

用法：
    python fix_table_render.py                      # 全库 dry-run
    python fix_table_render.py --apply              # 执行（逐文件备份）
    python fix_table_render.py --vault "D:/X/TNote" # 指定库
    python fix_table_render.py --only blank         # 只修第一类
    python fix_table_render.py --only quote         # 只修第二类
⚠ 遍历时跳过一切点开头目录：库根的 .agents/.codebuddy 是指向 skill 本体的 junction
  （2026-11-23 事故：穿透后改写了 skill 自己的 3 个文档）。

━━━ 两类问题与根因（2026-10-02 实测，Obsidian 1.13.7）━━━

【第一类】表格前缺空行（blank）
    md 表格必须与上一行空行隔断，否则整段被当作普通段落吞掉。
    实测对照（阅读模式）：
        题干行\n| a | b |\n| --- | --- |   →  塌成单个 <p>，表不渲染
        题干行\n\n| a | b |\n| --- | --- | →  正常渲染
    修法：在表格首行前补一个空行。
    ⚠ **只管表格前**（2026-11-23 用户定）：表格后紧跟正文/选项不影响渲染，不补。

【第二类】引用块内写 md 表格（quote）
    Obsidian 的 Markdown 渲染器**不支持引用块内渲染表格**——
    `> | a | b |` 在 Live Preview 与阅读模式下都塌成纯文字（表头行 + 分隔行）
    （实测 DOM：该引用块内 children 全是 P，无 table）。
    改用 HTML `<table>` 写在 `> ` 前缀里，两种模式均正常渲染（实测 rows=5）。
    修法：把引用块内的 md 表格整块替换为单行 HTML `<table>`。

    ⚠ 注意与「HTML 表格内嵌 md 图片」不是一回事——后者是 `<td>` 里写 `![]()`
      不渲染，属另一条已知限制（见 workflow/01-input.md）。

退出码：0 正常；1 有阻断性错误。
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

SEP_RE = re.compile(r"^\|[\s:|-]+\|\s*$")
QUOTE_RE = re.compile(r"^\s*>")
SKIP_PARTS = {".trash", "_备份"}


# ───────────────────────── 通用 ─────────────────────────

def load_vault(explicit=None):
    """vault 定位——必须与其它脚本同源（TNOTE_VAULT > skill_config.json）。

    2026-11-23 修：旧实现硬编码 `D:/Documents/TNote`，**绕过 TNOTE_VAULT**——
    测试里指向临时库却实际扫写真库（`--apply` 时尤其危险）。
    """
    if explicit:
        return Path(explicit)
    from qb_schema import get_vault          # 局部导入：其余功能不依赖 qb_schema
    try:
        return Path(get_vault())
    except Exception as e:                   # 未配置 vault → 中文提示后退出
        sys.exit(str(e))


def iter_md(vault):
    """遍历库内 md，**跳过一切点开头目录**。

    ⚠ 2026-11-23 事故：本库根下有 `.agents/skills/…`、`.codebuddy/skills/…` 的目录联接
    （junction）指向 `C:\\Users\\<用户>\\.agents\\skills\\`。`rglob` 会穿透 junction 进入
    skill 本体，把 skill 自己的 `references/fields.md` 等文档当库文件改写（实测 --apply
    动了 3 个 skill 文档）。与 `fix_image_formats.py` 的口径一致：点开头即不扫。
    """
    for p in vault.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(vault).parts[:-1]):
            continue
        if any(s in part for s in SKIP_PARTS for part in p.parts):
            continue
        if p.name.endswith((".bak-20261002", ".bak-quotetable")):
            continue
        yield p


def strip_quote(ln):
    return re.sub(r"^\s*>\s?", "", ln)


def split_cells(ln):
    return [c.strip() for c in ln.strip().strip("|").split("|")]


# ─────────────────── 第一类：表格前补空行 ───────────────────

def find_blank_issues(lines):
    """返回需要在其前面补空行的表格首行索引列表。

    只管**表格前**（2026-11-23 用户定）：表格后紧跟正文/选项不影响渲染，不补空行。
    """
    idx = []
    for i, ln in enumerate(lines):
        if not re.match(r"^\s*\|", ln):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not SEP_RE.match(nxt):
            continue                      # 不是表格首行
        if i == 0:
            continue
        prev = lines[i - 1]
        if prev.strip() and not re.match(r"^\s*\|", prev) and not re.match(r"^#{1,6}\s", prev):
            idx.append(i)
    return idx


# ─────────────────── 第二类：引用块内 md → HTML ───────────────────

def html_table(rows):
    head, body = rows[0], rows[1:]
    out = ["<table>",
           "<thead><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr></thead>"]
    if body:
        out.append("<tbody>")
        for r in body:
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def find_quote_table_spans(lines):
    """返回引用块内 md 表格的 [(start, end_exclusive), ...]。"""
    spans = []
    i = 0
    while i < len(lines):
        if not QUOTE_RE.match(lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and QUOTE_RE.match(lines[j]):
            j += 1
        k = i
        while k < j - 1:
            a, b = strip_quote(lines[k]), strip_quote(lines[k + 1])
            if a.startswith("|") and SEP_RE.match(b):
                t = k + 2
                while t < j and strip_quote(lines[t]).startswith("|"):
                    t += 1
                spans.append((k, t))
                k = t
            else:
                k += 1
        i = j
    return spans


# ───────────────────────── 主流程 ─────────────────────────

def process(path, do_blank, do_quote, apply):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    n_blank = n_quote = 0

    if do_quote:
        for k, t in reversed(find_quote_table_spans(lines)):
            rows = [split_cells(strip_quote(x)) for x in lines[k:t]]
            rows = [r for r in rows
                    if not all(SEP_RE.match("|" + c + "|") for c in r if c)]
            if not rows:
                continue
            lines[k:t] = ["> " + html_table(rows)]
            n_quote += 1

    if do_blank:
        idx = find_blank_issues(lines)
        for i in reversed(idx):
            lines.insert(i, "")
        n_blank = len(idx)

    if n_blank or n_quote:
        if apply:
            shutil.copy2(path, str(path) + ".bak-tblfix")
            path.write_text("\n".join(lines), encoding="utf-8")
    return n_blank, n_quote


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    ap.add_argument("--vault", default=None, help="vault 根目录")
    ap.add_argument("--only", choices=["blank", "quote"], default=None,
                    help="只修某一类")
    args = ap.parse_args()

    vault = load_vault(args.vault)
    do_blank = args.only in (None, "blank")
    do_quote = args.only in (None, "quote")

    tf = tb = tq = 0
    for p in iter_md(vault):
        nb, nq = process(p, do_blank, do_quote, args.apply)
        if nb or nq:
            tf += 1
            tb += nb
            tq += nq
            tag = []
            if nb:
                tag.append(f"补空行 {nb}")
            if nq:
                tag.append(f"引用块转HTML {nq}")
            print(f"[{'；'.join(tag)}] {p.relative_to(vault)}")

    mode = "已执行" if args.apply else "DRY-RUN（未写入）"
    print(f"\n{mode}：{tf} 个文件；补空行 {tb} 处，引用块转 HTML {tq} 处")
    if not args.apply and tf:
        print("确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
