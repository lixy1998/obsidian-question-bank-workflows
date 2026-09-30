# -*- coding: utf-8 -*-
"""paperkit.markup — 卷面文本清理、图片/表格摘除与选项拆分。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import re

CJK = r"\u4e00-\u9fff"


def clean(t: str) -> str:
    """卷面文本清理：中文之间的空格→全角（并列词/缩排）、中文字符后的半角逗号/分号→全角。

    ⚠ 两处必须保护，否则会毁版面（2026-09-25 实测）：
    ① **作答括号** `（　　）` 内部是全角空格、两侧是括号（非 CJK），会被「删多余空格」清成
       `（）` → 与后补的括号叠成 `（）（　　）`；先整段替换为 PUA 占位 `\\ue002`，末尾还原。
    ② **连续多个空格**要按原数量换回全角（`100分　　建议用时` 是刻意的两字宽间隔），
       只压成一个会丢版式（`分  建` 两个空格一个都不匹配单空格正则，最后被整段删掉）。
    """
    t = re.sub(r"[（(][\u3000 \t]*[）)]", "\ue002", t)          # 保护作答括号
    t = re.sub(r"^\s*>\s?", "", t)                              # md 引用符（题库正文偶带）
    t = re.sub(r"^\s*[-*+]\s+", "", t)                          # md 列表符
    t = t.replace("**", "").replace("__", "")                   # md 强调符
    t = t.replace("\u3000", " ")
    t = re.sub(rf"(?<=[{CJK}])(?P<sp> +)(?=[{CJK}])",
               lambda m: "\u3000" * len(m.group("sp")), t)
    t = re.sub(r"([A-Za-z0-9]) (?=[A-Za-z0-9])", lambda m: m.group(1) + "\ue000", t)
    t = t.replace(" ", "").replace("\ue000", " ")
    t = re.sub(rf"(?<=[{CJK}])[,;]", lambda m: "，" if m.group(0) == "," else "；", t)
    return t.replace("\ue002", "（　　）").strip()


OPT_SPLIT = re.compile(r"\((?:A|Ａ)\)")


def extract_images(body: str) -> tuple:
    """把图片行替换为 `\\x00IMGn\\x00` 占位，并摘出其后的图注行。

    返回 (去图文本, [(图片文件名, [图注行, …]), …])。
    图注判定：图片行后紧跟的非空行；遇到「（1）」「(A)」「——」「材料X」开头即停。
    """
    lines, out, imgs = body.split("\n"), [], []
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*!\[\]\(images/(.+?)\)\s*$", lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        cap, j = [], i + 1
        while j < len(lines):
            s = lines[j].strip()
            if not s:
                j += 1
                continue
            # ⚠ 下一张图片行也必须停 —— 否则会把后续图与图注一并吃成第一张的图注
            if (re.match(r"^!\[", s) or re.match(r"^[（(]\d+[）)]", s)
                    or re.match(r"^[（(][A-DＡ-Ｄ][）)]", s)
                    or s.startswith("——") or re.match(r"^材料[一二三四五六]", s)):
                break
            cap.append(clean(s))
            j += 1
        out.append(f"\ue001IMG{len(imgs)}\ue001")
        imgs.append((m.group(1), cap))
        i = j
    return "\n".join(out), imgs


TBL_SLOT_RE = re.compile(r"\ue003TBL(\d+)\ue003")
HTML_TABLE_RE = re.compile(r"<table>.*?</table>", re.S)
MD_TABLE_RE = re.compile(r"(?m)(?:^\|.*\|\s*$\n?){2,}")


def is_tbl_slot(ln: str) -> bool:
    """是否为表格占位行（`\\ue003TBLn\\ue003`）。"""
    return bool(TBL_SLOT_RE.fullmatch(ln.strip()))


def _html_table_rows(html: str) -> list:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if any(cells):
            rows.append(cells)
    return rows


def _md_table_rows(block: str) -> list:
    rows = []
    for line in block.strip().split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                                  # 分隔行 `|---|---|`
        rows.append(cells)
    return rows


def extract_tables(body: str) -> tuple:
    """摘出表格（HTML `<table>` 与 markdown 管道表）→ (占位文本, [表, …])，表 = [[单元格…]…]。

    ⚠ **必须摘出后另行渲染**：这两种语法都不能直接进 Word。原实现是把它们
    `re.sub` 删掉（只想着别让 md 语法漏到卷面），结果**所有表格题的表格在 Word 卷上凭空消失**——
    2026-09-25 用户实测发现第 1 题漏表，清查题库共 19 题 HTML 表 ＋ 181 题 md 表受影响。
    占位符自带换行，保证独立成行、能被逐行渲染识别。
    """
    tables = []

    def repl(m, is_html):
        rows = _html_table_rows(m.group(0)) if is_html else _md_table_rows(m.group(0))
        if not rows:
            return ""
        tables.append(rows)
        return f"\n\ue003TBL{len(tables) - 1}\ue003\n"

    body = HTML_TABLE_RE.sub(lambda m: repl(m, True), body)
    body = MD_TABLE_RE.sub(lambda m: repl(m, False), body)
    return body, tables


def split_stem_opts(body: str) -> tuple:
    """拆分题干与选项 → (题干行列表, 选项文本列表)。

    表格与图片已由 extract_tables / extract_images 摘成占位符，此处不再处理。
    """
    # 摘掉图片行与空表格行（docx 版图片另行插入）
    b = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)
    m = re.search(r"\((?:A|Ａ)\).*", b, re.S)
    if m:
        stem_src, opt_src = b[:m.start()], m.group(0)
        opts = [x.strip() for x in re.split(r"\((?:A|Ｂ|B|Ｃ|C|Ｄ|D)\)", opt_src)[1:]]
    else:
        stem_src, opts = b, []
    lines = [clean(s) for s in stem_src.split("\n") if s.strip()]
    return lines, opts
