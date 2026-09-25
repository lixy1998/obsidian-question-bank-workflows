# -*- coding: utf-8 -*-
"""verify_paper_layout.py — 试卷版式校验器（国标合规 + 卷面结构 + 图文混排可判定项）。

核验矩阵见 references/试卷版式核验矩阵.md（条款编号 A1..C9 与本脚本一致）。

用法：
  python verify_paper_layout.py --docx "成卷.docx"        # Word 成卷：字号/行距/字体/图片/答案分页/页边距
  python verify_paper_layout.py --卷 2026河南中考          # 题库内某卷的 md 结构层校验
  python verify_paper_layout.py --docx x.docx --json
  python verify_paper_layout.py --docx x.docx --正文最少字号 12 --行空最少 3.0
退出码：0 = 无「不合规」项；1 = 有「不合规」项（供流程阻断）

合规判定依据：GB 40070-2021 §7（→§4.3/4.4/4.5/6.1）
  · 五~九年级和高中正文 ≥12P（小 4 号 = 12pt），汉字以宋体为主
  · 辅文（目录/注释/图注/表内等）≥9P（小 5 号 = 9pt）
  · 行空（两行字之间的空隙宽）≥3.0mm ⇒ 行距 ≥ 字高 + 行空
⚠ 标准附录 A.9：字号用分度值 0.1mm 量具、行空用 0.5mm 量具实测；本脚本为文档侧等效核算。
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

PT2MM = 25.4 / 72.0

LEVEL_OK, LEVEL_BAD, LEVEL_MANUAL, LEVEL_OUT = "合规", "不合规", "需人工", "不承诺"
MANUAL_ITEMS = [
    ("B2", "大题用中文数字、小题用阿拉伯数字"),
    ("B4", "选项两项一行、全角空格分隔、`A．`式顿点"),
    ("B6", "整题分值紧随题号 `22．(6分)`"),
    ("B8", "同一道试题不得跨页（Word 分页预览）"),
    ("C2", "单图题：图靠右、宽约版心 1/3"),
    ("C3", "多图并排等距居中"),
    ("C5", "图注格式：地点遗址 / 出土器物（距今约X—Y年）"),
    ("C7", "图注不脱离图（图+图注同页）"),
    ("C9", "图片等比缩放、扫描图去噪裁边"),
]
OUT_OF_SCOPE = [("A5", "打印纸张定量/亮度/荧光亮度（印制环节，需打印样张按附录A检测）"),
                ("A6", "印刷套印误差 ≤0.30mm（印制环节）")]


class Report:
    def __init__(self):
        self.items = []

    def add(self, code, level, msg, fix=""):
        self.items.append({"条款": code, "判定": level, "说明": msg, "修法": fix})

    def dump(self, name, as_json=False):
        bad = [i for i in self.items if i["判定"] == LEVEL_BAD]
        if as_json:
            print(json.dumps({"对象": name, "items": self.items,
                              "不合规数": len(bad)}, ensure_ascii=False, indent=2))
            return len(bad)
        print(f"═══ 试卷版式校验 · {name} ═══")
        print("依据：GB 40070-2021 §7（字号/行距/辅文字号强制下限）+ 本 skill 试卷排版规范")
        for lv in (LEVEL_BAD, LEVEL_OK, LEVEL_MANUAL, LEVEL_OUT):
            rows = [i for i in self.items if i["判定"] == lv]
            if not rows:
                continue
            mark = {"不合规": "✗", "合规": "✓", "需人工": "⚠", "不承诺": "—"}[lv]
            print(f"\n【{mark} {lv} {len(rows)} 项】")
            for i in rows:
                line = f"  {i['条款']} {i['说明']}"
                if i["修法"]:
                    line += f" → 修法：{i['修法']}"
                print(line)
        print(f"\n结论：{'✗ 存在不合规项，修正后再交付' if bad else '✓ 文档侧无不合规项（人工项仍需核对）'}")
        return len(bad)


# ---------------- Word 成卷 ----------------

def _east_asia_font(run, para, doc):
    """取中文（东亚）字体名：run → 段落样式 → Normal 样式。"""
    from docx.oxml.ns import qn

    def from_rpr(rpr):
        if rpr is None:
            return None
        rf = rpr.find(qn("w:rFonts"))
        return rf.get(qn("w:eastAsia")) if rf is not None else None

    name = from_rpr(run._element.rPr)
    if not name and para.style is not None:
        name = from_rpr(para.style.element.rPr)
    if not name:
        try:
            name = from_rpr(doc.styles["Normal"].element.rPr)
        except KeyError:
            name = None
    return name


def _size_pt(run, para, doc):
    if run.font.size is not None:
        return run.font.size.pt
    if para.style is not None and para.style.font.size is not None:
        return para.style.font.size.pt
    try:
        n = doc.styles["Normal"].font.size
        if n is not None:
            return n.pt
    except KeyError:
        pass
    return None


def _check_tables_figures(manifest, doc, rpt):
    """D1 表图完整性：按清单还原题库题目，数出「应有几张表、几张图」，与卷面实际数比对。

    起因（2026-09-25，用户实测发现）：`split_stem_opts` 曾把 HTML `<table>` 与 md 管道表
    `re.sub` 删掉，**200 道表格题的表格在 Word 卷上静默消失**，而字号/行距/图片类检查
    全部照常通过 —— 说明"版式合规"不等于"内容没丢"。本项专门堵这个口子。
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import export_student_paper as exp
        man = exp.load_manifest(Path(manifest))
        recs, miss = exp.resolve_items(exp.norm_items(man),
                                       exp.build_index(verbose=False), strict=False)
    except Exception as e:                        # 清单坏了不应阻断版式校验
        rpt.add("D1", LEVEL_MANUAL, f"表图完整性未比对（清单不可用：{e}）")
        return

    exp_tbl = exp_img = 0
    for r in recs:
        no_img, imgs = exp.extract_images(r["body"])
        exp_img += len(imgs)
        exp_tbl += len(exp.extract_tables(no_img)[1])

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    n_tbl, n_img = len(doc.tables), len(doc.element.body.findall(f".//{W}drawing"))

    if exp_tbl == n_tbl and exp_img == n_img:
        rpt.add("D1", LEVEL_OK,
                f"表图完整性：题库 {exp_tbl} 表 / {exp_img} 图，卷面一致（{n_tbl} 表 / {n_img} 图）")
    else:
        if exp_tbl > n_tbl:
            rpt.add("D1", LEVEL_BAD,
                    f"卷面缺表格：题库应 {exp_tbl} 张，卷面只有 {n_tbl} 张（少 {exp_tbl - n_tbl}）",
                    "表格可能被当语法清掉了——检查 extract_tables 摘取与 render_docx 的表格渲染")
        if exp_img > n_img:
            rpt.add("D1", LEVEL_BAD,
                    f"卷面缺图片：题库正文应 {exp_img} 张，卷面只有 {n_img} 张（少 {exp_img - n_img}）",
                    "检查 extract_images 占位与 add_picture 插入链路")
        if exp_tbl < n_tbl or exp_img < n_img:
            rpt.add("D1", LEVEL_MANUAL,
                    f"卷面表/图多于题库统计（卷 {n_tbl} 表 {n_img} 图 vs 题库 {exp_tbl} 表 {exp_img} 图）"
                    "——可能含答题卡选题栏等非题目元素，人工确认")
    if miss:
        rpt.add("D1", LEVEL_MANUAL, f"清单有 {len(miss)} 条未定位，表图比对口径可能偏小")


def verify_docx(path, min_body=12.0, min_sub=9.0, min_net_mm=3.0, rpt=None, manifest=None):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.text import WD_LINE_SPACING

    doc = Document(str(path))
    rpt = rpt or Report()
    body_sizes, sub_sizes = Counter(), Counter()
    line_rows, fonts, body_texts, sub_texts = [], Counter(), [], []
    pic_paras, aligned = [], []
    total_pic_w = 0.0

    def walk(paras, in_table=False):
        nonlocal total_pic_w
        for pi, para in enumerate(paras):
            txt = (para.text or "").strip()
            size = None
            for run in para.runs:
                if run.text and run.text.strip():
                    size = size or _size_pt(run, para, doc)
                    fn = _east_asia_font(run, para, doc)
                    if fn:
                        fonts[fn] += 1
                # ⚠ 图片 run 通常不含文字，故图片检测必须在「有文字」分支之外
                # （2026-09-21 修正：原先嵌在 if run.text 内，导致纯图片段落一律漏检，C1 居中项恒报 0/0）
                if len(run._element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing")) \
                        or len(run._element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pict")):
                    pic_paras.append((pi, para.alignment))
            if para.style is not None and size is None and para.style.font.size is not None:
                size = para.style.font.size.pt
            if size is not None and txt:
                (sub_sizes if in_table else body_sizes)[round(size, 1)] += 1
                (sub_texts if in_table else body_texts).append((pi, txt[:24], size))
            pf = para.paragraph_format
            if txt:
                rule, ls = pf.line_spacing_rule, pf.line_spacing
                if hasattr(ls, "pt"):
                    line_rows.append(("固定", ls.pt, size, len(txt)))
                elif rule == WD_LINE_SPACING.MULTIPLE and isinstance(ls, float):
                    line_rows.append(("倍数", ls, size, len(txt)))
                elif not in_table and para.alignment != WD_ALIGN_PARAGRAPH.CENTER:
                    # 未显式设置行距且非居中段（居中多为标题/图注/表题）→ 按正文单倍行距判定
                    line_rows.append(("单倍", None, size, len(txt)))
            if txt.startswith("——") and para.alignment == WD_ALIGN_PARAGRAPH.RIGHT:
                aligned.append(txt[:20])
        return

    walk(doc.paragraphs)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                walk(cell.paragraphs, in_table=True)

    # A1/A2 正文字号与字体
    if body_sizes:
        main_size = body_sizes.most_common(1)[0][0]
        if main_size < min_body - 1e-6:
            rpt.add("A1", LEVEL_BAD, f"正文主体字号 {main_size}pt < 国标下限 {min_body}pt（小四）",
                    f"正文样式改 {min_body}pt（小四）")
        else:
            rpt.add("A1", LEVEL_OK, f"正文主体字号 {main_size}pt（≥{min_body}pt）")
        small = [s for s in body_sizes if s < min_body - 1e-6]
        if small:
            rpt.add("A4", LEVEL_MANUAL,
                    f"存在小于正文的字号档：{sorted(small)}（可能是图注/说明，属辅文）"
                    f"，需确认 ≥{min_sub}pt（小五）")
        else:
            rpt.add("A4", LEVEL_OK, f"未见小于正文的字号档（辅文下限 {min_sub}pt）")
    if fonts:
        fam = fonts.most_common(1)[0][0]
        rpt.add("A2", LEVEL_OK if ("宋体" in fam or "SimSun" in fam) else LEVEL_BAD,
                f"中文主体字体：{fam}" + ("" if ("宋体" in fam or "SimSun" in fam) else "（国标要求以宋体为主）"),
                "" if ("宋体" in fam or "SimSun" in fam) else "正文样式中文（东亚）字体改宋体")
    if sub_sizes:
        sub_min = min(sub_sizes)
        rpt.add("A4", LEVEL_OK if sub_min >= min_sub - 1e-6 else LEVEL_BAD,
                f"表格/辅文字号下限 {sub_min}pt（要求 ≥{min_sub}pt）",
                "" if sub_min >= min_sub - 1e-6 else f"表格/图注字号改 ≥{min_sub}pt")

    # A3 行距（固定值精确判定；倍数近似）
    fixed = [(pt, sz, L) for kind, pt, sz, L in line_rows if kind == "固定" and sz]
    if fixed:
        nets = [((pt - sz) * PT2MM, pt, L) for pt, sz, L in fixed]
        bad = [x for x in nets if x[0] < min_net_mm - 1e-6]
        if bad:
            n, pt, _ = min(bad, key=lambda x: x[0])
            rpt.add("A3", LEVEL_BAD,
                    f"固定行距 {pt:.1f}pt ⇒ 行间净空 {n:.2f}mm < 国标 {min_net_mm}mm（行距下限 "
                    f"≥{(min_net_mm / PT2MM + (sorted(body_sizes)[0] if body_sizes else 12)):.1f}pt）",
                    "段落行距改固定值 24pt（对标河南中考卷）")
        else:
            rpt.add("A3", LEVEL_OK,
                    f"固定行距净空最小 {min(n for n, _, _ in nets):.2f}mm（≥{min_net_mm}mm）")
    mult = [x for x in line_rows if x[0] == "倍数"]
    if mult:
        rpt.add("A3", LEVEL_MANUAL,
                f"存在倍数行距段落 {len(mult)} 处（倍数≈{round(mult[0][1], 2)}；Word 倍数折算不精确）",
                "建议统一改固定值以便核定（净空 = 行距 − 字号）")
    # 单倍行距只对「正文字号 + 较长文本（≥20 字）」判定，避免把标题/图注/出处误判为正文
    single = [x for x in line_rows
              if x[0] == "单倍" and (x[2] or 0) >= (min_body - 1e-6) and x[3] >= 20]
    if single:
        rpt.add("A3", LEVEL_BAD,
                f"{len(single)} 个正文段落未设显式行距 ⇒ Word 单倍行距，净空 ≈0.6~0.9mm < 国标 {min_net_mm}mm",
                "段落行距改固定值 24pt（下限 21pt）")

    # C1/C8 图片
    sec = doc.sections[0]
    body_w_mm = (sec.page_width - sec.left_margin - sec.right_margin) / 36000.0 if sec.page_width else None
    pics = []
    for sh in doc.inline_shapes:
        w_mm = sh.width / 36000.0
        h_mm = sh.height / 36000.0
        pics.append((w_mm, h_mm))
        total_pic_w += w_mm * h_mm
    if pics:
        over = [p for p in pics if body_w_mm and p[0] > body_w_mm + 0.1]
        if over:
            rpt.add("C1", LEVEL_BAD, f"{len(over)} 张图片宽度超出正版心（版心 {body_w_mm:.1f}mm）",
                    "等比缩放至版心内")
        else:
            rpt.add("C1", LEVEL_OK, f"{len(pics)} 张图片均在版心内（最宽 {max(p[0] for p in pics):.1f}mm）")
        if body_w_mm and sec.page_height:
            body_h_mm = (sec.page_height - sec.top_margin - sec.bottom_margin) / 36000.0
            ratio = total_pic_w / (body_w_mm * body_h_mm) if body_w_mm * body_h_mm else 0
            rpt.add("C8", LEVEL_MANUAL,
                    f"本卷图片总面积 ≈ 版心的 {ratio * 100:.0f}%（内部约定 ≤50%，仅按首页版心估算）")
        centered = [al for _, al in pic_paras if al == WD_ALIGN_PARAGRAPH.CENTER]
        rpt.add("C1", LEVEL_OK if centered else LEVEL_MANUAL,
                f"图片所在段落居中 {len(centered)}/{len(pic_paras)}",
                "" if centered else "图片段落设为居中")
    # B5 材料出处右对齐
    if aligned:
        rpt.add("B5", LEVEL_OK, f"材料出处右对齐 {len(aligned)} 处（如「{aligned[0]}」）")
    else:
        rpt.add("B5", LEVEL_MANUAL, "未检出右对齐的材料出处（河南卷惯例：`——摘编自……` 右对齐）")

    # B7 教师卷答案分页
    page_break = False
    for para in doc.paragraphs:
        if para.paragraph_format.page_break_before:
            page_break = True
        xml = para._element.xml
        if 'w:type="page"' in xml or "lastRenderedPageBreak" in xml:
            page_break = True
    has_ans = any("参考答案" in (p.text or "") for p in doc.paragraphs)
    rpt.add("B7", LEVEL_OK if (not has_ans or page_break) else LEVEL_MANUAL,
            ("含答案区且检出分页" if (has_ans and page_break) else
             "未含答案区（学生卷）" if not has_ans else "含答案区但未检出分页"),
            "" if (not has_ans or page_break) else "答案区首段设「段前分页」或插入分页符")

    # B9 页脚
    ftext = " ".join(p.text for p in sec.footer.paragraphs if p.text).strip()
    rpt.add("B9", LEVEL_OK if ftext else LEVEL_MANUAL,
            f"页脚：{ftext or '未检出文本（页码域可能需在 Word 中复核）'}")

    # 页边距（惯例项，仅提示）
    lmm = sec.left_margin / 36000.0
    rmm = sec.right_margin / 36000.0
    rpt.add("B-margin", LEVEL_MANUAL,
            f"左右页边距 {lmm:.1f}/{rmm:.1f}mm（国标未规定；常规 20~22mm）")

    # B11 页面尺寸模式与版心宽（惯例层：A4 打印模式 / 中考仿真模式 185×260）
    if sec.page_width and sec.page_height:
        w_mm, h_mm = sec.page_width / 36000.0, sec.page_height / 36000.0
        if abs(w_mm - 210) < 3 and abs(h_mm - 297) < 3:
            mode = "A4 打印模式"
        elif abs(w_mm - 185) < 6 and abs(h_mm - 260) < 6:
            mode = "中考仿真模式（6 开长条三折单页 185×260）"
        else:
            mode = "自定义/其他"
        rpt.add("B11", LEVEL_OK,
                f"页面尺寸 {w_mm:.0f}×{h_mm:.0f}mm → {mode}；版心宽 "
                f"{body_w_mm:.0f}mm" if body_w_mm else f"页面尺寸 {w_mm:.0f}×{h_mm:.0f}mm → {mode}")

    if manifest:                                  # D1 表图完整性（需 --清单 才能比对）
        _check_tables_figures(manifest, doc, rpt)

    for code, desc in MANUAL_ITEMS:
        if not any(i["条款"] == code for i in rpt.items):
            rpt.add(code, LEVEL_MANUAL, desc)
    for code, desc in OUT_OF_SCOPE:
        rpt.add(code, LEVEL_OUT, desc)
    return rpt


# ---------------- 题库 md 卷结构层 ----------------

def verify_volume(vol, rpt=None):
    sys.path.insert(0, str(Path(__file__).parent))
    import qb_schema as q

    rpt = rpt or Report()
    roots = q.stage_roots()
    cands = [p for root in roots for p in root.glob(vol) if p.is_dir()]
    if not cands:
        print(f"[失败] 未找到卷目录：{vol}", file=sys.stderr)
        return None
    vol_dir = sorted(set(cands))[0]
    files = [p for p in vol_dir.rglob("*.md") if q.is_question_file(p)]
    seqs, bad_opt, miss_img, empty = [], [], [], []
    for p in sorted(files):
        try:
            seqs.append(int(p.stem))
        except ValueError:
            rpt.add("B1", LEVEL_MANUAL, f"文件名非数字序号：{p.name}")
        fm, body, err = q.split_frontmatter(p.read_text(encoding="utf-8"))
        if err:
            rpt.add("B1", LEVEL_BAD, f"{p.name} frontmatter 解析失败：{err}")
        if not body.strip():
            empty.append(p.name)
        opts = q.as_list(fm.get("选项"))
        if fm.get("题型") == "选择题" and opts and len(opts) != 4:
            bad_opt.append(f"{p.name}({len(opts)}项)")
        for m in re.finditer(r"!\[\]\(images/([^)\s]+)\)", p.read_text(encoding="utf-8")):
            if not (p.parent / "images" / m.group(1)).exists():
                miss_img.append(f"{p.name}→{m.group(1)}")
    if seqs:
        full = set(range(min(seqs), max(seqs) + 1))
        missing = sorted(full - set(seqs))
        rpt.add("B1", LEVEL_OK if not missing else LEVEL_BAD,
                f"题号连续：{min(seqs)}~{max(seqs)} 共 {len(seqs)} 题"
                + ("" if not missing else f"，缺号 {missing}"),
                "" if not missing else "补齐缺号或核对文件命名")
    rpt.add("B3", LEVEL_OK if not bad_opt else LEVEL_BAD,
            f"选择题选项数：{'全部 4 项' if not bad_opt else '异常 ' + '、'.join(bad_opt)}")
    rpt.add("C-img-miss", LEVEL_OK if not miss_img else LEVEL_BAD,
            f"正文图片引用：{'全部存在' if not miss_img else '缺失 ' + '、'.join(miss_img[:5])}")
    rpt.add("B1-empty", LEVEL_OK if not empty else LEVEL_BAD,
            f"正文非空：{'全部有内容' if not empty else '空正文 ' + '、'.join(empty[:5])}")
    # 导出物（学生卷）不应含答案
    for stu in vol_dir.glob("*_学生版.md"):
        txt = stu.read_text(encoding="utf-8")
        hit = "答案" in txt or "解析" in txt
        rpt.add("B7", LEVEL_BAD if hit else LEVEL_OK,
                f"{stu.name}：{'检出答案/解析字样（学生卷不得含）' if hit else '无答案泄漏'}",
                "冲跑 export_student_paper.py 生成" if hit else "")
    for code, desc in MANUAL_ITEMS:
        if not any(i["条款"] == code for i in rpt.items):
            rpt.add(code, LEVEL_MANUAL, desc)
    for code, desc in OUT_OF_SCOPE:
        rpt.add(code, LEVEL_OUT, desc)
    return rpt


def main():
    ap = argparse.ArgumentParser(description="试卷版式校验器（国标合规 + 结构 + 图文混排可判定项）")
    ap.add_argument("--docx", help="Word 成卷路径")
    ap.add_argument("--清单", help="组卷清单：给了就做 D1「表图完整性」比对（题库有表/有图→卷面是否也在）")
    ap.add_argument("--卷", help="题库内卷名（结构层校验，需已配置 vault）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--正文最少字号", type=float, default=12.0, help="正文最小字号 pt（默认 12=小四）")
    ap.add_argument("--辅文最少字号", type=float, default=9.0, help="辅文最小字号 pt（默认 9=小五）")
    ap.add_argument("--行空最少", type=float, default=3.0, help="行间净空下限 mm（默认 3.0）")
    args = ap.parse_args()

    if not args.docx and not args.卷:
        ap.print_help()
        sys.exit(2)

    bad = 0
    if args.docx:
        p = Path(args.docx)
        if not p.exists():
            sys.exit(f"[失败] 文件不存在：{p}")
        rpt = verify_docx(p, args.正文最少字号, args.辅文最少字号, args.行空最少,
                          manifest=args.清单)
        bad += rpt.dump(p.name, args.json)
    if args.卷:
        rpt = verify_volume(args.卷)
        if rpt is not None:
            bad += rpt.dump(f"题库卷 · {args.卷}", args.json)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
