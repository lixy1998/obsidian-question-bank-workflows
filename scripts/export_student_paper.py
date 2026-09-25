# -*- coding: utf-8 -*-
"""export_student_paper.py — 组卷导出（清单驱动，v3）

主用法：吃一份「组卷清单」→ 出学生卷/教师卷（md 或 Word）。
清单是**有序的题目引用**（可跨卷），每项可带分值/来源括注/考察角度；
题目正文实时从题库读取，清单只管「选哪些、什么顺序、怎么标注」。

用法：
  # ① 清单驱动（主用法）
  python export_student_paper.py --清单 组卷清单.yaml --模式 学生 --输出 <out.md>
  python export_student_paper.py --清单 组卷清单.yaml --模式 教师 --输出 <out.md>
  python export_student_paper.py --清单 组卷清单.yaml --docx <out.docx>
  python export_student_paper.py --清单 组卷清单.yaml --体检
  python export_student_paper.py --清单 组卷清单.yaml --模式 教师 --docx out.docx

  # ② 按卷导出（兼容原用法）
  python export_student_paper.py --卷 2024河南中考 --only 01,03
  python export_student_paper.py --卷 "A,B" --only "A:01;B:07" --输出 out.md

清单格式（.yaml/.yml/.json；.txt 则一行一个 id）：
  卷名: 9年级上历史第1-4单元月考        # 必填，用于输出命名与图片命名空间
  标题: 2026—2027学年第一学期　9年级历史上册　第1—4单元阶段测试
  副标题: 姓名：__________　班级：__________　得分：__________
  说明:
    - 满分：100分　　建议用时：45分钟
    - 每小题只有一个选项符合题意，请将所选答案填入卷末答题卡内。
  题目:
    - 2024河南中考-13                    # 纯 id：元数据自动从题库读
    - id: 2026湖北中考-27                # 带组卷元数据（可覆盖题库值）
      分值: 2
      来源: (2026·湖北中考·27)           # 学生卷按 --标来源 决定是否出现在卷面
      角度: 第1课·文明起源——知识卡片式材料判断农业文明起源的多源性

  兼容 9/21 式 selected.json：顶层为数组、元素带 id/文件 快照，自动识别。

输出：
  md   —— 卷头 + 题号重编（1．2．…）+ 题干原文；教师卷附答案/解析/来源/角度
  docx —— 按《试卷排版规范》成卷：A4、宋体 12pt、固定行距 24pt、选项两项一行、
          图片居中（图注紧随）、教师卷答案分页另起、页脚页码域
  图片 —— 统一放 {输出目录}/images/{卷名}/（跨卷同名图不冲突），md 引用同步改写
"""
import argparse
import json
import re
import shutil
import sys

from pathlib import Path

from qb_schema import (VAULT, is_question_file, as_list, split_frontmatter,
                       stage_roots, strip_comments)

try:
    import yaml
except ImportError:
    yaml = None


# ============================================================================
# 一、题库索引（id → 题目文件）
# ============================================================================

def build_index(verbose: bool = True) -> dict:
    """扫库建索引：`id → 题目文件路径`（id 多值时各项都指向同一文件）。"""
    idx = {}
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            fm, _, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            for qid in as_list(fm.get("id")):
                idx.setdefault(str(qid).strip(), p)
    if verbose:
        print(f"[索引] {len(idx)} 个 id")
    return idx


def vol_name_of(path: Path) -> str:
    """从题目文件路径取卷名：{vault}/{学段-类别}/{卷名}/…"""
    try:
        rel = path.relative_to(Path(VAULT)).parts
        return rel[1] if len(rel) > 1 else path.parent.name
    except ValueError:
        return path.parent.name


def norm_no(s) -> str:
    """题号归一化：1 与 01 视为同一题号。"""
    s = str(s).strip()
    return str(int(s)) if s.isdigit() else s


def img_ns(vol: str) -> str:
    """卷名 → 图片子目录名（去掉路径非法字符与空白）。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", vol)


# ============================================================================
# 二、清单加载
# ============================================================================

def load_manifest(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"[失败] 清单文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("[失败] 读 YAML 清单需要 pyyaml（venv 已内置）")
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:  # 纯文本：一行一个 id，`#` 起注释
        items = [l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        data = {"卷名": path.stem, "题目": items}
    if data is None:
        raise SystemExit(f"[失败] 清单为空：{path}")
    if isinstance(data, list):                      # 兼容旧 selected.json
        data = {"卷名": path.stem, "题目": data}
    if not isinstance(data, dict):
        raise SystemExit(f"[失败] 清单顶层应为映射或数组，实为 {type(data).__name__}")
    return data


def norm_items(man: dict) -> list:
    """清单条目统一为 dict（str → {'id': …}）。"""
    raw = man.get("题目") or man.get("questions") or []
    if not isinstance(raw, list):
        raise SystemExit("[失败] 清单的「题目」应为列表")
    out = []
    for x in raw:
        if isinstance(x, str):
            out.append({"id": x.strip()})
        elif isinstance(x, dict):
            out.append(dict(x))
        else:
            raise SystemExit(f"[失败] 清单条目格式不支持：{x!r}")
    return out


def resolve_items(items: list, index: dict, strict: bool = True) -> tuple:
    """逐条定位题目文件并读出元数据 → (记录列表, 缺失列表)。

    定位优先级：条目自带「文件」→ id 索引 → 「卷名-题号」拆分兜底。
    """
    ok, miss = [], []
    for i, it in enumerate(items, 1):
        path, why = None, ""
        f = it.get("文件") or it.get("file")
        if f and Path(f).exists():
            path = Path(f)
        else:
            qid = str(it.get("id") or "").strip()
            if not qid:
                miss.append((i, "(缺 id)", "条目既无「id」也无有效「文件」"))
                continue
            path = index.get(qid)
            if path is None and "-" in qid:          # 兜底：卷名-题号
                vol, no = qid.rsplit("-", 1)
                for root in stage_roots():
                    for cand in root.glob(f"{vol}/*/*.md"):
                        if norm_no(cand.stem) == norm_no(no):
                            path = cand
                            break
                    if path:
                        break
                why = "（按 卷名-题号 兜底命中）" if path else ""
        if path is None or not path.exists():
            miss.append((i, it.get("id") or it.get("文件"), "题库中未找到该 id"))
            continue
        fm, body, err = split_frontmatter(path.read_text(encoding="utf-8"))
        ok.append({
            "序号": i, "id": it.get("id") or fm.get("id"),
            "path": path, "vol": vol_name_of(path),
            "fm": fm, "body": strip_comments(body),
            "分值": it.get("分值", it.get("score", fm.get("分值"))),
            "来源括注": it.get("来源", ""), "角度": it.get("角度", ""),
            "选项序": it.get("选项序") or it.get("order"),
            "括号": it.get("括号"),
            "备注": it.get("备注", ""), "yaml_err": err, "定位提示": why,
        })
    if miss and strict:
        print(f"[警告] {len(miss)} 条未定位：")
        for i, qid, why in miss:
            print(f"   第{i}条 {qid} —— {why}")
    return ok, miss


# ============================================================================
# 三、图片打包（跨卷命名空间）
# ============================================================================

IMG_REF_RE = re.compile(r"!\[\]\(images/([^)\s]+)\)")
IMG_PREFIX_RE = re.compile(r"!\[\]\(images/")


def bundle_images(recs: list, out_dir: Path) -> int:
    """把选中题目引用的图片复制到 {输出目录}/images/{卷名}/。"""
    copied = 0
    for r in recs:
        img_dir = out_dir / "images" / img_ns(r["vol"])
        for name in IMG_REF_RE.findall(
                r["path"].read_text(encoding="utf-8")):
            src, dst = r["path"].parent / "images" / name, img_dir / name
            if not src.exists():
                print(f"[警告] 图片缺失：{src}")
                continue
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            img_dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                copied += 1
            except PermissionError:
                print(f"[警告] 图片被占用跳过：{dst}")
    return copied


def rewrite_img(body: str, vol: str) -> str:
    return IMG_PREFIX_RE.sub(f"![]({'images/' + img_ns(vol) + '/'}", body)


# ============================================================================
# 四、卷面文本（md / docx 共用）
# ============================================================================

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
        m = re.match(r"^\s*!\[\]\(images/([^)\s]+)\)\s*$", lines[i])
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


def split_stem_opts(body: str) -> tuple:
    """拆分题干与选项 → (题干行列表, 选项文本列表)。"""
    # 摘掉图片行与空表格行（docx 版图片另行插入）
    b = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)
    b = re.sub(r"<table>.*?</table>", "", b, flags=re.S)
    b = re.sub(r"(?m)^\|.*\|$", "", b)
    m = re.search(r"\((?:A|Ａ)\).*", b, re.S)
    if m:
        stem_src, opt_src = b[:m.start()], m.group(0)
        opts = [x.strip() for x in re.split(r"\((?:A|Ｂ|B|Ｃ|C|Ｄ|D)\)", opt_src)[1:]]
    else:
        stem_src, opts = b, []
    lines = [clean(s) for s in stem_src.split("\n") if s.strip()]
    return lines, opts


Q_TAIL = ("具有", "是", "有", "为", "表明", "反映", "体现", "说明", "可知", "源于",
          "在于", "得益于", "属于", "指", "目的是", "原因是", "标志是", "可以看出")


IMG_SLOT_RE = re.compile(r"\ue001IMG(\d+)\ue001")


def is_img_slot(ln: str) -> bool:
    """是否为 docx 图片占位行（`\\ue001IMGn\\ue001`）。"""
    return bool(IMG_SLOT_RE.fullmatch(ln.strip()))


def find_answer_slot(lines: list, mode: str = None) -> int:
    """返回应补「（　　）」的行号；-1＝已有无需补，-2＝按清单要求不补。

    启发的顺序：清单显式「括号: 首行|末行|无」→ 否则找**短且以设问词结尾**的行
    （卡片式/条文式材料的设问常在首行，而非末行）→ 兜底最后一个文本行。
    ⚠ **图片占位行不参与判定**，否则括号会补到图片的位置上（实测踩坑）。
    """
    if any("（　　）" in l for l in lines):
        return -1
    if mode == "无":
        return -2
    rows = [i for i, l in enumerate(lines) if not is_img_slot(l)]
    if not rows:
        return -1
    if mode == "首行":
        return rows[0]
    if mode == "末行":
        return rows[-1]
    for i in rows:
        ln = lines[i]
        if len(ln) <= 42 and not ln.endswith("。") and ln.endswith(Q_TAIL):
            return i
    return rows[-1]


def reorder_opts(opts: list, rec: dict) -> tuple:
    """按清单「选项序」重排选项 → (新选项列表, 新答案字母 或 None)。

    「选项序: [3,1,2,4]」＝ 新卷面 A/B/C/D 位依次放**原第** 3、1、2、4 项；
    答案字母同步换算（题库原题不动，仅卷内重排，对应 §3.2 答案分布均衡）。
    """
    seq = rec.get("选项序")
    if not seq or not opts:
        return opts, None
    perm = [int(x) - 1 for x in seq]
    if sorted(perm) != list(range(len(opts))):
        raise SystemExit(f"[失败] 第 {rec['序号']} 条「选项序」不合法：{seq}"
                         f"（应为 1..{len(opts)} 的排列）")
    new = [opts[i] for i in perm]
    old = str(rec["fm"].get("答案", "")).strip()
    if len(old) == 1 and old in "ABCD":
        new_ans = "ABCD"[perm.index("ABCD".index(old))]
    else:
        new_ans = None
    return new, new_ans


def opts_md(opts: list) -> str:
    """选项卷面文本：两项一行、`A．` 全角顿点、项间全角空格×2（《试卷排版规范》§三/§12.4）。"""
    rows = []
    for i in range(0, len(opts) - 1, 2):
        rows.append(f"{'ABCD'[i]}．{opts[i]}\u3000\u3000{'ABCD'[i + 1]}．{opts[i + 1]}")
    if len(opts) % 2:
        rows.append(f"{'ABCD'[len(opts) - 1]}．{opts[-1]}")
    return "\n".join(rows)


def rtype_of(r: dict) -> str:
    return str(r["fm"].get("题型", "")).strip()


def is_choice(r: dict) -> bool:
    return rtype_of(r) == "选择题"


def big_title(recs: list, choice: bool) -> str:
    """大题标题（《试卷排版规范》§二）：选择题给「每小题X分，共Y分」，非选择题给总分。"""
    from collections import Counter
    part = [r for r in recs if is_choice(r) == choice]
    tot = sum(int(r["分值"]) for r in part if str(r["分值"]).strip().isdigit())
    if choice:
        per = Counter(str(r["分值"]).strip() for r in part).most_common(1)[0][0]
        if per.isdigit() and int(per) * len(part) == tot:      # 分值统一才写「每小题X分」
            return f"一、选择题（每小题{per}分，共{tot}分）"
        return f"一、选择题（共{len(part)}小题，{tot}分）"
    return f"二、非选择题（共{tot}分）"


def section_marks(recs: list) -> list:
    """返回 [(卷面序号, 该处需插入的大题标题或 None), …]。"""
    marks, switched = [], False
    for n, r in enumerate(recs, 1):
        if n == 1:
            marks.append((n, big_title(recs, is_choice(r))))
            switched = not is_choice(r)
        elif not is_choice(r) and not switched and any(is_choice(x) for x in recs):
            marks.append((n, big_title(recs, False)))
            switched = True
        else:
            marks.append((n, None))
    return marks


# ============================================================================
# 五、md 卷
# ============================================================================

def header_md(man: dict) -> list:
    out = []
    if man.get("标题"):
        out.append(f"# {man['标题']}")
    if man.get("副标题"):
        out.append(man["副标题"])
    notes = man.get("说明") or []
    if isinstance(notes, str):
        notes = [notes]
    if notes:
        out.append("")
        for i, n in enumerate(notes, 1):
            out.append(f"{i}.{n}")          # 半角点，与题目「1．」区分
    return out


def compose_md_body(body: str, rec: dict) -> tuple:
    """md 卷面加工：补作答括号 ＋ 选项重排（保留图片与题干原文，只规范化选项段）。

    → (新正文, 新答案字母 或 None)
    """
    m = re.search(r"\((?:A|Ａ).*", body, re.S)
    if not m:                                     # 非选择题：原样
        return body, None
    head, opt_src = body[:m.start()], m.group(0)
    opts = [x.strip() for x in re.split(r"\((?:A|Ｂ|B|Ｃ|C|Ｄ|D)\)", opt_src)[1:]]
    opts, new_ans = reorder_opts(opts, rec)
    lines = head.split("\n")
    idxs = [i for i, l in enumerate(lines) if l.strip()]
    slot = find_answer_slot([lines[i].strip() for i in idxs], rec.get("括号"))
    if slot >= 0 and idxs:
        tgt = idxs[min(slot, len(idxs) - 1)]
        lines[tgt] = lines[tgt].rstrip() + "（　　）"
    return "\n".join(lines) + opts_md(opts) + "\n", new_ans


def render_md(man: dict, recs: list, mode: str, mark_src: bool) -> str:
    parts = header_md(man)
    if parts:
        parts.append("")
    for n, mark in section_marks(recs):
        if mark:
            parts.append(f"**{mark}**")
            parts.append("")
        r = recs[n - 1]
        body, new_ans = compose_md_body(rewrite_img(r["body"], r["vol"]), r)
        r["_ans"] = new_ans if new_ans else r["fm"].get("答案", "")
        no = f"{n}．"
        if not is_choice(r) and str(r["分值"]).strip().isdigit():
            no += f"（{r['分值']}分）"              # 非选择题分值紧随题号（§四）
        if mark_src and mode == "学生" and r["来源括注"]:
            no += str(r["来源括注"])                 # 来源括注紧跟题号后（§12.1）
        parts.append(f"{no}{body.strip()}")
        if mode == "教师":
            fm = r["fm"]
            tail = [f"**答案**：{r['_ans']}"]
            jx = str(fm.get("解析", "") or "").strip()
            if jx:
                # 解析里可能内嵌图（实测 2024河南中考-01、2025河南中考-01）：
                # 与题干重复的直接去掉（否则教师卷重出一遍），独有的保留并改写命名空间
                same = set(IMG_REF_RE.findall(r["body"]))
                jx = re.sub(r"!\[\]\(images/([^)\s]+)\)",
                            lambda m: "" if m.group(1) in same else m.group(0),
                            jx).strip()
                if jx:
                    tail.append(f"**解析**：{rewrite_img(jx, r['vol'])}")
            if r["来源括注"]:
                tail.append(f"**来源**：{r['来源括注']}")
            if r["角度"]:
                tail.append(f"**考察角度**：{r['角度']}")
            if r["备注"]:
                tail.append(f"**备注**：{r['备注']}")
            parts.append("")
            parts.extend(tail)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ============================================================================
# 六、Word 卷
# ============================================================================

def render_docx(man: dict, recs: list, mode: str, out: Path,
                mark_src: bool, img_width_mm: float = 78.0,
                img_right: bool = False) -> None:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_BREAK
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    def setrun(r, size=12, bold=False, cn="宋体"):
        r.font.name = "Times New Roman"
        r.font.size = Pt(size)
        r.font.bold = bold
        r._element.rPr.rFonts.set(qn("w:eastAsia"), cn)
        return r

    def para(doc, size=12, line=24, align=None, before=0, after=0,
             left=0, keep=False, keepn=False):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing = Pt(line)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        pf.left_indent = Pt(left)
        if align is not None:
            p.alignment = align
        pf.keep_together, pf.keep_with_next = keep, keepn
        return p

    def run(p, text, size=12, bold=False, cn="宋体"):
        return setrun(p.add_run(text), size, bold, cn)

    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Mm(210), Mm(297)
    s.left_margin = s.right_margin = Mm(21)
    s.top_margin = s.bottom_margin = Mm(25)
    s.footer_distance = Mm(12)
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    # 卷头
    def centered(text, size=14, bold=False, before=2, after=2, keepn=True):
        p = para(doc, size=size, line=24, align=WD_ALIGN_PARAGRAPH.CENTER,
                 before=before, after=after, keepn=keepn)
        run(p, text, size, bold)
        return p

    if man.get("标题"):
        centered(man["标题"], 14, True, before=0, after=4)
    if man.get("副标题"):
        centered(man["副标题"], 12, False, before=0, after=4)

    notes = man.get("说明") or []
    if isinstance(notes, str):
        notes = [notes]
    if notes:
        para(doc, size=10.5, line=20, after=2, keepn=True)
        for i, n in enumerate(notes, 1):
            p = para(doc, size=10.5, line=20, after=0, keepn=True)
            run(p, f"{i}.{clean(n)}", 10.5)          # 半角点，与题目「1．」区分
        para(doc, size=10.5, line=12, after=0)

    # 题目
    for n, mark in section_marks(recs):
        r = recs[n - 1]
        if mark:                                     # 大题标题（§二）
            p = para(doc, before=6, after=2, keepn=True)
            run(p, clean(mark), 12, True)
        img_dir = out.parent / "images" / img_ns(r["vol"])
        stripped, imgs = extract_images(r["body"])
        lines, opts = split_stem_opts(stripped)
        if not lines:
            lines = [clean(stripped)]
        opts, new_ans = reorder_opts(opts, r)
        r["_ans"] = new_ans if new_ans else r["fm"].get("答案", "")
        rtype = rtype_of(r)
        if rtype == "选择题":                        # 材料题作答在答题区，不补卷内括号
            slot = find_answer_slot(lines, r.get("括号"))
            if slot >= 0:
                lines[slot] += "（　　）"

        no_txt = f"{n}．"
        if rtype and rtype != "选择题" and str(r["分值"]).strip().isdigit():
            no_txt += f"（{r['分值']}分）"           # 非选择题分值紧随题号（§四）
        if mark_src and mode == "学生" and r["来源括注"]:
            no_txt += str(r["来源括注"])              # 来源括注紧跟题号后（§12.1）

        # 连续图片占位合成一组 → 并排（§10.2）；单张 → 居中（§10.1 的靠右环绕待实现）
        k = 0
        while k < len(lines):
            ln = lines[k]
            if is_img_slot(ln):
                grp = []
                while k < len(lines) and is_img_slot(lines[k]):
                    grp.append(int(IMG_SLOT_RE.fullmatch(lines[k].strip()).group(1)))
                    k += 1
                if len(grp) == 1:
                    nm, cap = imgs[grp[0]]
                    src = img_dir / nm
                    if src.exists():
                        p = para(doc, align=(WD_ALIGN_PARAGRAPH.RIGHT if img_right
                                             else WD_ALIGN_PARAGRAPH.CENTER),
                                 before=2, after=0, keep=True, keepn=True)
                        p.add_run().add_picture(str(src), width=Mm(img_width_mm))
                    for c in cap:
                        pc = para(doc, size=10.5, line=20, align=WD_ALIGN_PARAGRAPH.CENTER,
                                  after=0, keep=True, keepn=True)
                        run(pc, c, 10.5)
                else:
                    ncol = len(grp)
                    w = (168 - 6 * (ncol - 1)) / ncol / 1.02       # 版心 168mm 均分（mm）
                    t = doc.add_table(rows=2, cols=ncol)
                    t.alignment = WD_TABLE_ALIGNMENT.CENTER
                    for j, gi in enumerate(grp):
                        nm, cap = imgs[gi]
                        src = img_dir / nm
                        c0 = t.cell(0, j)
                        c0.text = ""
                        pp = c0.paragraphs[0]
                        pp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        pp.paragraph_format.line_spacing = Pt(20)
                        pp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                        pp.paragraph_format.space_after = Pt(0)
                        if src.exists():
                            pp.add_run().add_picture(str(src), width=Mm(w))
                        c1 = t.cell(1, j)
                        c1.text = ""
                        p1 = c1.paragraphs[0]
                        p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        p1.paragraph_format.line_spacing = Pt(20)
                        p1.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                        p1.paragraph_format.space_after = Pt(0)
                        setrun(p1.add_run("\n".join(cap)), 10.5)
                    para(doc, size=10.5, line=10, after=2)
                continue
            p = para(doc, left=(12 if ln.startswith("材料") else 0),
                     align=(WD_ALIGN_PARAGRAPH.RIGHT if ln.startswith("——") else None),
                     keepn=True)
            run(p, (no_txt if k == 0 else "") + ln, 12)
            k += 1
        if opts:                                     # 两项一行、A．全角顿点（§三/§12.4）
            for i in range(0, len(opts) - 1, 2):
                p = para(doc, before=0, after=0, keepn=(i + 2 < len(opts)))
                run(p, f"{'ABCD'[i]}．{opts[i]}\u3000\u3000"
                       f"{'ABCD'[i + 1]}．{opts[i + 1]}", 12)
            if len(opts) % 2:
                p = para(doc, before=0, after=0)
                run(p, f"{'ABCD'[len(opts) - 1]}．{opts[-1]}", 12)
        para(doc, size=12, line=12, after=0)

    # 答案卷：分页另起
    if mode == "教师":
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        centered(f"{man.get('卷名', '')}　参考答案", 14, True, before=0, after=6)
        ch_ans = [(n, r) for n, r in enumerate(recs, 1) if is_choice(r)]
        if ch_ans:
            p = para(doc, after=2, keepn=True)
            run(p, clean(big_title(recs, True).split("（")[0]), 12, True)
            items = [f"{n}．{r.get('_ans', r['fm'].get('答案', ''))}" for n, r in ch_ans]
            for i in range(0, len(items), 10):        # 选择题答案连排（§六，每行 10 项）
                p = para(doc, after=0)
                run(p, "\u3000".join(items[i:i + 10]), 12)
        for n, r in enumerate(recs, 1):
            fm = r["fm"]
            jx = re.sub(r"!\[[^\]]*\]\([^)]+\)", "",            # 答案区不带图
                        str(fm.get("解析", "") or "")).strip()
            if is_choice(r):                            # 选择题答案已连排：仅补解析
                if not jx:
                    continue
                p = para(doc, size=10.5, line=20, left=12, before=2, after=0)
                run(p, f"[{n} 解析] {clean(jx)}", 10.5)
                continue
            p = para(doc, before=3, after=0, keepn=True)
            run(p, f"{n}．{r.get('_ans', fm.get('答案', ''))}", 12)
            if jx:
                p = para(doc, size=10.5, line=20, left=12, after=0)
                run(p, f"[解析] {clean(jx)}", 10.5)
            if r["角度"]:
                p = para(doc, size=10.5, line=20, left=12, after=0)
                run(p, f"[考察角度] {clean(r['角度'])}", 10.5)

    # 页脚页码域
    from docx.oxml import OxmlElement
    fp = doc.sections[0].footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pre = run(fp, f"{man.get('卷名', '')}　第 ", 9)
    for instr in ("PAGE", "NUMPAGES"):
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), instr)
        fp._p.append(fld)
        if instr == "PAGE":
            run(fp, " 页　共 ", 9)
    run(fp, " 页", 9)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


# ============================================================================
# 七、卷面体检
# ============================================================================

def render_answer_card(man: dict, recs: list, out: Path,
                       per_row: int = 10, blanks: int = 2) -> None:
    """答题卡（校级测试·手阅版，A4 两页）。版式照 2026.09.22 月考答题卡实物：

    · 页面 A4，**左边距 30mm 作装订区**（右 20 / 上 18 / 下 18，页脚距 12.7）
    · 标题两行：卷名 ＋「答　题　卡」，黑体 14pt 居中
    · 姓名栏一行（姓名/班级/考场/考号）
    · 一、选择题（每小题X分，共Y分）＋ **全框线表格**（`题号`／`答案` 两行，每行 per_row 题，
      首列 16mm、其余 14.4mm、行高 8.5mm）
    · 二、非选择题（共Z分）＋ 逐题逐小问「21．（1）」各留 blanks 行
    · 页脚「历史答题卡　第 X 页　共 Y 页」
    """
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    def setrun(rr, size=12, bold=False, cn="宋体"):
        rr.font.name = "Times New Roman"
        rr.font.size = Pt(size)
        rr.font.bold = bold
        rr._element.rPr.rFonts.set(qn("w:eastAsia"), cn)
        return rr

    def run(p, text, size=12, bold=False, cn="宋体"):
        return setrun(p.add_run(text), size, bold, cn)

    def para(doc, size=12, line=24, align=None, after=0, keepn=False):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing = Pt(line)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.space_before, pf.space_after = Pt(0), Pt(after)
        if align is not None:
            p.alignment = align
        pf.keep_with_next = keepn
        return p

    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Mm(210), Mm(297)
    s.left_margin, s.right_margin = Mm(30), Mm(20)
    s.top_margin = s.bottom_margin = Mm(18)
    s.footer_distance = Mm(12.7)
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    for txt in (man.get("标题") or man.get("卷名") or "", "答　题　卡"):
        p = para(doc, size=14, align=WD_ALIGN_PARAGRAPH.CENTER, after=2, keepn=True)
        setrun(p.add_run(txt), 14, True, "黑体")

    # 姓名栏放**首页页眉**（页面最上方）——只第 1 页有，第 2 页起不重复。
    # ⚠ 不能放正文：那样第二页顶部直接是学生作答区，装订时被钉住/盖住（2026-09-25 用户实测反馈）
    s.header_distance = Mm(10)
    s.different_first_page_header_footer = True          # 首页页眉页脚独立
    fh = s.first_page_header
    fh.is_linked_to_previous = False
    hp = fh.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hpf = hp.paragraph_format
    hpf.line_spacing = Pt(20)
    hpf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    hpf.space_before = hpf.space_after = Pt(0)
    setrun(hp.add_run("姓名__________　班级__________　考场__________　考号__________"), 12)
    s.header.is_linked_to_previous = False               # 后续页页眉留空

    pairs = list(enumerate(recs, 1))
    ch = [(n, r) for n, r in pairs if is_choice(r)]
    ot = [(n, r) for n, r in pairs if not is_choice(r)]

    if ch:
        p = para(doc, after=2, keepn=True)
        run(p, clean(big_title(recs, True)), 12)
        for start in range(0, len(ch), per_row):
            grp = ch[start:start + per_row]
            t = doc.add_table(rows=2, cols=len(grp) + 1)
            t.style = "Table Grid"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            t.autofit = False
            for j in range(len(t.columns)):           # 首列 16mm、其余 14.4mm（照参考卡）
                t.columns[j].width = Mm(16) if j == 0 else Mm(14.4)
            for row in t.rows:
                row.height = Mm(8.5)
                for j, cell in enumerate(row.cells):
                    cell.width = Mm(16) if j == 0 else Mm(14.4)
                    cell.text = ""
                    cp = cell.paragraphs[0]
                    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    pf = cp.paragraph_format
                    pf.line_spacing = Pt(20)
                    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                    pf.space_before = pf.space_after = Pt(0)
            setrun(t.cell(0, 0).paragraphs[0].add_run("题号"), 10.5)
            setrun(t.cell(1, 0).paragraphs[0].add_run("答案"), 10.5)
            for j, (qn_, _r) in enumerate(grp, 1):
                setrun(t.cell(0, j).paragraphs[0].add_run(str(qn_)), 10.5)
            para(doc, size=12, line=12, after=4)

    if ot:
        p = para(doc, after=2, keepn=True)
        run(p, clean(big_title(recs, False)), 12)
        for n, r in ot:
            subs = list(dict.fromkeys(
                re.findall(r"[（(]\s*(\d+)\s*[）)]", r["body"])))
            if not subs:                             # 无小问（小论文/论述题）：整题留白更多
                p = para(doc, after=0, keepn=True)
                run(p, f"{n}．", 12)
                for _ in range(max(blanks, 6)):
                    para(doc, after=0)
                para(doc, size=12, line=12, after=0)
                continue
            for si, sub in enumerate(subs):
                label = f"{n}．" if si == 0 else ""
                p = para(doc, after=0, keepn=True)
                run(p, f"{label}（{sub}）", 12)
                for _ in range(blanks):
                    para(doc, after=0)
            para(doc, size=12, line=12, after=0)

    # 页脚：首页与后续页都要页码（启用「首页不同」后两处须分别写）
    for part in (s.footer, s.first_page_footer):
        part.is_linked_to_previous = False
        fp = part.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run(fp, "历史答题卡\u3000第 ", 9)
        for instr in ("PAGE", "NUMPAGES"):
            fld = OxmlElement("w:fldSimple")
            fld.set(qn("w:instr"), instr)
            fp._p.append(fld)
            if instr == "PAGE":
                run(fp, " 页\u3000共 ", 9)
        run(fp, " 页", 9)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


def checkup(man: dict, recs: list) -> None:
    from collections import Counter
    total = sum(int(r["分值"]) for r in recs if str(r["分值"]).isdigit())
    ttypes = Counter(str(r["fm"].get("题型", "?")) for r in recs)
    diff = Counter(str(r["fm"].get("难度", "?")) for r in recs)
    ch = [r for r in recs if str(r["fm"].get("题型", "")).strip() == "选择题"]
    ans = Counter(str(r["fm"].get("答案", "")).strip() for r in ch)
    ans = Counter({k: v for k, v in ans.items() if k in ("A", "B", "C", "D")})
    vols = Counter(r["vol"] for r in recs)
    books = Counter(b for r in recs for b in as_list(r["fm"].get("册别")))
    lessons = Counter(l for r in recs for l in as_list(r["fm"].get("课"))[:1])
    srcs = Counter(("教材改编" if "教材改编" in as_list(r["fm"].get("来源"))
                    else "真题" if "真题" in as_list(r["fm"].get("来源"))
                    else "原创题" if "原创题" in as_list(r["fm"].get("来源"))
                    else "模拟/其他") for r in recs)

    print(f"\n===== 卷面体检：{man.get('卷名', '')} =====")
    print(f"题量 {len(recs)}　总分 {total}　"
          f"题型 {' / '.join(f'{k} {v}' for k, v in ttypes.most_common())}")
    print(f"难度 {' · '.join(f'{k} {v}' for k, v in diff.most_common())}")
    ac = " · ".join(f"{k} {v}" for k, v in sorted(ans.items())) or "（无选择题）"
    print(f"答案 {ac}　（{len(ch)} 道选择题）")
    if ans and len(ans) > 1 and max(ans.values()) > max(2, len(ch) * 0.35):
        print("     ⚠ 答案分布偏集中 → 对部分题加「选项序」重排（答案会自动同步换算）")
    n_re = sum(1 for r in recs if r.get("选项序"))
    if n_re:
        print(f"选项重排 {n_re} 题（答案已同步换算，题库原题不动）")
    print(f"来源 {' · '.join(f'{k} {v}' for k, v in srcs.most_common())}")
    print(f"册别 {' · '.join(f'{k} {v}' for k, v in books.most_common())}")
    print(f"卷源 {len(vols)} 个 —— " + " · ".join(f"{k}({v})" for k, v in vols.most_common()))
    print(f"考点覆盖 {len(lessons)} 课")
    for k, v in lessons.most_common():
        if v > 1:
            print(f"     ⚠ 同课多题：{k} × {v}")
    head_slot = []
    for i, r in enumerate(recs, 1):
        if str(r["fm"].get("题型", "")).strip() != "选择题":
            continue
        ln, _ = split_stem_opts(extract_images(r["body"])[0])
        if any("（　　）" in l for l in ln):
            continue
        sl = find_answer_slot(ln, r.get("括号"))
        if sl >= 0 and sl != len(ln) - 1:
            head_slot.append(i)
    if head_slot:
        print(f"     ⚠ {len(head_slot)} 道题的作答括号自动补在**非末行**（设问在材料之前），"
              f"成卷前请核对：第 {'、'.join(map(str, head_slot))} 题")
    seq = [str(r["fm"].get("答案", "")).strip() for r in ch]
    run_same, run_start, prev = 1, 0, (seq[0] if seq else "")
    for i in range(1, len(seq)):
        if seq[i] and seq[i] == prev:
            run_same += 1
        else:
            if run_same >= 4:
                print(f"     ⚠ 选择题第 {run_start + 1}—{i} 题答案连续同为 {prev}")
            run_same, run_start = 1, i
        prev = seq[i]
    if run_same >= 4:
        print(f"     ⚠ 选择题第 {run_start + 1}—{len(seq)} 题答案连续同为 {prev}")


# ============================================================================
# 八、兼容：按卷导出（原用法）
# ============================================================================

def find_volume(vol: str, stage: str = None, category: str = None) -> Path:
    cands = []
    for root in stage_roots():
        if stage and not root.name.startswith(stage):
            continue
        if category and category not in root.name:
            continue
        cands.extend(p for p in root.glob(vol) if p.is_dir())
    cands = sorted(set(cands))
    if not cands:
        raise FileNotFoundError(f"未找到卷目录：{vol}")
    if len(cands) > 1:
        print(f"[提示] 命中多个同名目录，取第一个：\n  " + "\n  ".join(map(str, cands)))
    return cands[0]


def sort_key(p: Path, mode: str):
    if mode == "id":
        m = re.search(r"(\d+)", p.stem)
        return (0, int(m.group(1))) if m else (1, p.stem)
    return (0, p.stem)


def split_nums(s: str) -> list:
    nums = [x.strip() for x in re.split(r"[,，、\s]+", s) if x.strip()]
    if not nums:
        raise SystemExit("[失败] --only 里有空的题号组")
    seen, out = set(), []
    for n in nums:
        k = norm_no(n)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def parse_specs(vol_arg: str, only_arg: str) -> list:
    vols = [v.strip() for v in re.split(r"[,，]", vol_arg) if v.strip()]
    if not vols:
        raise SystemExit("[失败] --卷 不能为空")
    dup = sorted({v for v in vols if vols.count(v) > 1})
    if dup:
        raise SystemExit(f"[失败] --卷 里同一卷出现多次：{'、'.join(dup)}")
    if not only_arg:
        return [(v, None) for v in vols]
    groups = [s.strip() for s in re.split(r"[;；]", only_arg) if s.strip()]
    if not groups:
        raise SystemExit("[失败] --only 里没有任何题号组")
    named = [bool(re.search(r"[:：]", g)) for g in groups]
    if any(named):
        if not all(named):
            raise SystemExit("[失败] --only 混用两种写法（带卷名与不带卷名），请统一")
        specs = []
        for g in groups:
            name, nums = re.split(r"[:：]", g, maxsplit=1)
            specs.append((name.strip(), split_nums(nums)))
        unknown = [n for n, _ in specs if n not in vols]
        if unknown:
            raise SystemExit(f"[失败] --only 里的卷名不在 --卷 列表内：{'、'.join(unknown)}")
        return specs
    if len(groups) == 1:
        return [(v, split_nums(groups[0])) for v in vols]
    if len(groups) == len(vols):
        return [(v, split_nums(g)) for v, g in zip(vols, groups)]
    raise SystemExit(f"[失败] --only 有 {len(groups)} 组、--卷 有 {len(vols)} 卷，数量不匹配")


def collect(vol_dir: Path, qtype: str = None, only: list = None):
    files = [p for p in vol_dir.rglob("*.md") if is_question_file(p)]
    if qtype:
        files = [p for p in files if f"_{qtype}" in p.parent.name]
    if only:
        wanted = {norm_no(x) for x in only}
        files = [p for p in files if norm_no(p.stem) in wanted]
    return files


def export_by_volume(args) -> int:
    specs = parse_specs(args.卷, args.only)
    multi = len(specs) > 1
    if multi and not args.输出:
        raise SystemExit("[失败] 跨卷组卷必须显式指定 --输出")
    units, total = [], 0
    for vol, nums in specs:
        try:
            vol_dir = find_volume(vol, args.学段, args.类别)
        except FileNotFoundError as e:
            raise SystemExit(f"[失败] {e}")
        files = collect(vol_dir, args.题型, nums)
        if nums:
            idx = {norm_no(x): i for i, x in enumerate(nums)}
            files.sort(key=lambda p: idx.get(norm_no(p.stem), len(idx)))
            found = {norm_no(p.stem) for p in files}
            missing = [n for n in nums if norm_no(n) not in found]
            if missing:
                print(f"[警告] {vol}：题号未找到 → {'、'.join(missing)}")
        else:
            files.sort(key=lambda p: sort_key(p, args.排序))
        units.append((vol, vol_dir, files))
        total += len(files)
    if total == 0:
        print("[失败] 未选到任何题目", file=sys.stderr)
        return 1
    out = Path(args.输出) if args.输出 else (
        units[0][1] / f"{units[0][1].name}_{'教师' if args.模式 == '教师' else '学生'}版.md")
    blocks = []
    for vol, _d, files in units:
        for p in files:
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            body = strip_comments(body)
            if not body:
                continue
            if multi:
                body = rewrite_img(body, vol)
            if args.模式 == "教师":
                tail = f"\n\n**答案**：{fm.get('答案', '')}"
                jx = str(fm.get("解析", "") or "").strip()
                if jx:
                    tail += f"\n**解析**：{jx}"
                if args.来源标注:
                    tail += f"\n\n**来源**：{vol}（原第 {p.stem} 题）"
                body += tail
            blocks.append(body)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n---\n\n".join(blocks) + "\n", encoding="utf-8")
    n_img = 0
    for vol, _d, files in units:
        img_dir = (out.parent / "images" / img_ns(vol)) if multi else (out.parent / "images")
        for p in files:
            for name in IMG_REF_RE.findall(p.read_text(encoding="utf-8")):
                src, dst = p.parent / "images" / name, img_dir / name
                if not src.exists():
                    print(f"[警告] 图片缺失：{src}")
                    continue
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    continue
                img_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    n_img += 1
                except PermissionError:
                    print(f"[警告] 图片被占用跳过：{dst}")
    if n_img:
        print(f"[图片] 已打包 {n_img} 张")
    print(f"[导出] {out}（{len(blocks)} 题，{args.模式}版）")
    for vol, _d, files in units:
        print(f"  · {vol}：{len(files)} 题 → " + "、".join(p.stem for p in files))
    return 0


# ============================================================================
# 九、入口
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="历史题库组卷导出器（清单驱动）")
    ap.add_argument("--清单", help="组卷清单（.yaml/.yml/.json；.txt 一行一个 id）")
    ap.add_argument("--卷", help="[兼容] 卷名；多卷用逗号分隔")
    ap.add_argument("--only", help="[兼容] 题号；跨卷用分号分组，可写 卷名:题号")
    ap.add_argument("--学段", choices=["初中", "高中"])
    ap.add_argument("--类别", choices=["中考真题", "中考模拟题", "阶段测试", "高考真题", "高考模拟题"])
    ap.add_argument("--题型")
    ap.add_argument("--模式", choices=["学生", "教师"], default="学生")
    ap.add_argument("--输出")
    ap.add_argument("--排序", choices=["路径", "id"], default="id")
    ap.add_argument("--来源标注", action="store_true", help="[兼容] 教师卷附原卷来源")
    ap.add_argument("--标来源", action="store_true",
                    help="学生卷在末行附来源括注（近5年中考真题等）")
    ap.add_argument("--docx", help="另出 Word 卷到该路径")
    ap.add_argument("--图片宽", type=float, default=78.0, help="Word 卷图片宽度 mm（默认 78）")
    ap.add_argument("--图靠右", action="store_true",
                    help="单图段落右对齐（《试卷排版规范》§10.1；真正的文字环绕需人工调整）")
    ap.add_argument("--答题卡", help="另出答题卡到该路径（给了 --docx 时默认同目录同出）")
    ap.add_argument("--无答题卡", action="store_true", help="给了 --docx 也不出答题卡")
    ap.add_argument("--答题卡每行", type=int, default=10, help="选择题答题栏每行题数（默认 10）")
    ap.add_argument("--答题卡行数", type=int, default=2, help="非选择题每小问留白行数（默认 2）")
    ap.add_argument("--体检", action="store_true", help="只打印卷面体检，不导出")
    args = ap.parse_args()

    if not args.清单:
        if not args.卷:
            raise SystemExit("[失败] 请给 --清单 <清单文件>（或兼容用法 --卷）")
        return export_by_volume(args)

    man = load_manifest(Path(args.清单))
    items = norm_items(man)
    if not items:
        raise SystemExit("[失败] 清单里没有任何题目")
    index = build_index()
    recs, miss = resolve_items(items, index)
    if not recs:
        raise SystemExit("[失败] 全部条目均未定位到题目")
    for r in recs:
        if r["yaml_err"]:
            print(f"[警告] {r['path'].name}：{r['yaml_err']}")
    print(f"[清单] {len(recs)} 题已定位" + (f"，{len(miss)} 题未定位" if miss else ""))

    checkup(man, recs)
    if args.体检:
        return 0

    vol_title = man.get("卷名") or "组卷"
    out = Path(args.输出) if args.输出 else None
    if out is None:
        base = Path(args.清单).parent
        out = base / f"{vol_title}_{'教师' if args.模式 == '教师' else '学生'}版.md"
    # 图片：md 与 docx 各自所在目录都要有（两者目录可能不同）
    targets = {out.parent}
    if args.docx:
        targets.add(Path(args.docx).parent)
    n_img = 0
    for t in targets:
        n_img += bundle_images(recs, t)
    if n_img:
        print(f"[图片] 已打包 {n_img} 张 → "
              + "；".join(str(t / 'images') for t in targets))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(man, recs, args.模式, args.标来源), encoding="utf-8")
    print(f"[导出] {out}（{len(recs)} 题，{args.模式}版 md）")

    if args.docx:
        dx = Path(args.docx)
        render_docx(man, recs, args.模式, dx, args.标来源, args.图片宽, args.图靠右)
        print(f"[导出] {dx}（{len(recs)} 题，{args.模式}版 docx）")

    card = Path(args.答题卡) if args.答题卡 else None
    if card is None and args.docx and not args.无答题卡:
        card = Path(args.docx).parent / f"{vol_title}_答题卡.docx"
    if card:
        render_answer_card(man, recs, card, args.答题卡每行, args.答题卡行数)
        print(f"[导出] {card}（答题卡，每行 {args.答题卡每行} 题 / 每问留白 {args.答题卡行数} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
