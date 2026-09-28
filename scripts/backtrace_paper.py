# -*- coding: utf-8 -*-
"""backtrace_paper.py — 从**成品卷**反查题库 id，还原「组卷清单」

场景：手上只有一份已经出好的卷子（docx / md / 文字版 pdf），题库里也已录入过这些题，
要还原出可复用的组卷清单（交给 export_student_paper.py 重出、微调、换题、配答题卡）。

做法（不依赖任何命题说明）：
  1. 读成品卷 → 按题号切题（自动跳过卷头说明区：题号须从 1 起严格递增）
  2. 每题取「题干」规范化（剥掉题号/分值/选项/作答括号/标点空白）
  3. **全库题干匹配**（difflib）→ 取最高分为命中，低分题列出候选供人工确认
  4. 自动**检测选项重排**：成品卷选项顺序与题库不同时，反推出 `选项序: [原第几项…]`
  5. （可选）成品卷若含答案连排行，提取答案与题库逐题比对，提示"被改过/重排过"的题
  6. 输出组卷清单 YAML ＋ 逐题匹配报告

用法：
  python backtrace_paper.py --卷 成品卷.docx                      # 默认输出 <卷名>_清单.yaml
  python backtrace_paper.py --卷 成品卷.docx --输出 清单.yaml
  python backtrace_paper.py --卷 成品卷.docx --报告               # 只打印报告，不写清单
  python backtrace_paper.py --卷 成品卷.docx --阈值 0.55 --候选数 5

限制：
  · 只支持**文字版** docx / md / pdf；**扫描件（图片型 PDF）先走录入链路**（MinerU 提取后再来）
  · 匹配分数低于 `--阈值` 的题不写入清单，改在报告里列候选，人工确认后再补
  · 题干被大幅改写的题（换题、重写设问）天然匹配不上——这正是需要人工介入的地方

⚠ 读 docx 用 python-docx（与 skill 内其他脚本一致，脚本须能独立运行）。
"""
import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

sys.path.insert(0, str(Path(__file__).parent))
from qb_schema import (VAULT, as_list, is_question_file, split_frontmatter,
                       stage_roots)

PUNCT = r"[\s，。、；：？！“”‘’（）()【】\[\]｜|·—…\-－–—〈〉《》]"


# ============================================================================
# 一、读成品卷
# ============================================================================

def read_docx_lines(path: Path) -> list:
    """按**文档顺序**取段落与表格（表格题的表体必须落在题干附近，否则匹配不上）。"""
    from docx import Document
    doc = Document(str(path))
    out = []
    try:
        for item in doc.iter_inner_content():          # python-docx ≥1.1
            if hasattr(item, "rows"):
                for row in item.rows:
                    out.append(" ".join(c.text.strip() for c in row.cells))
            else:
                out.append(item.text)
    except AttributeError:                             # 旧版兜底：段落在前、表格在后
        out = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            for row in t.rows:
                out.append(" ".join(c.text.strip() for c in row.cells))
    return out


def read_md_lines(path: Path) -> list:
    return path.read_text(encoding="utf-8").splitlines()


def read_pdf_lines(path: Path) -> list:
    try:
        import fitz
    except ImportError:
        raise SystemExit("[失败] 读 PDF 需要 PyMuPDF（venv 已内置）")
    doc = fitz.open(str(path))
    lines = []
    for page in doc:
        lines.extend((page.get_text() or "").splitlines())
    doc.close()
    if not any(l.strip() for l in lines):
        raise SystemExit("[失败] PDF 无可提取文字（疑为扫描件）——请先用 MinerU 提取录入")
    return lines


Q_HEAD = re.compile(r"^\s*(\d{1,2})\s*[．.、]\s*(.*)")     # 不锚 $：行内可能残留软换行
SEC_HEAD = re.compile(r"^\s*[一二三四五六七八九十]\s*、")   # 大题标题：一、选择题／二、非选择题
INVISIBLE = "\u200b\u200c\u200d\ufeff\u2060\u00ad"          # 零宽/软连字符等，会让 ^\s* 失配


def _clean_line(s: str) -> str:
    """剔不可见字符、把段内软换行（docx 的 <w:br/> 会变成 \\n）压成空格。

    ⚠ 段内 `\\n` 必须处理：否则 `.*$` 类正则匹配不上，整题漏切
    （2026-09-25 实测：题 8、11 的题号字符与其他题完全相同却不匹配）。
    """
    for ch in INVISIBLE:
        if ch in s:
            s = s.replace(ch, "")
    s = re.sub(r"[\r\n\t\v\f\u2028\u2029]", " ", s)
    return s.strip()


def split_questions(lines: list) -> list:
    """按「题号．」切题 → [(题号, 全文)]。

    ⚠ 三个必须（2026-09-25 实测逐条踩过）：
    ① 题号须从当前计数 +1 **严格递增**；
    ② 遇到大题标题（`一、选择题`）**重置计数** —— 否则卷头「注意事项」的 `1．2．3．`
       会被当成题 1~3，真正的题 1 起就接不上、整卷只切出几题；
    ③ 先剔**零宽字符**并把段内软换行压成空格 —— 个别题号行前带 `\\u200b` 之类、
       或段内含 `\\n`（实测第 8、11 题），`^\\s*` / `.*$` 匹配不上就会整题漏切；
    ④ 大题标题后计数置 **-1（等待任意题号）**，不能置 0 —— 河南卷「小题跨大题连续编号」，
       非选择题从 21 起，置 0 会导致 21≠1 而整块非选择题漏切（实测）。
    大题标题之前的切分结果（卷头说明区）默认丢弃；若全卷没有任何大题标题，则退回全保留。
    """
    out_pre, out_post, cur, cur_no = [], [], [], 0
    sec_seen = False

    def flush():
        if cur_no and cur_no > 0 and cur:
            (out_post if sec_seen else out_pre).append(
                (cur_no, "\n".join(cur).strip()))

    for raw in lines:
        line = _clean_line(raw)
        if SEC_HEAD.match(line):
            flush()
            cur_no, cur, sec_seen = -1, [], True     # -1：下一题接受任意题号
            continue
        m = Q_HEAD.match(line)
        if m:
            no = int(m.group(1))
            if cur_no == -1 or no == cur_no + 1:     # 大题后可重新起数（连续编号）
                flush()
                cur_no, cur = no, [m.group(2)]
                continue
        if cur_no:
            cur.append(line)
    flush()
    return out_post if out_post else out_pre


# ============================================================================
# 二、规范化与选项解析
# ============================================================================

def norm_text(t: str, n: int = 60) -> str:
    t = re.sub(r"^[（(]\s*\d+\s*分\s*[）)]", "", t.strip())      # 题号后的分值
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", t)                    # md 图片
    t = re.sub(r"[（(]\s*[）)]", "", t)                            # 作答括号
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(PUNCT, "", t)
    return t[:n]


OPT_HEAD = re.compile(r"[（(]?\s*([A-DＡ-Ｄ])\s*[．.、）)]")


def parse_options(text: str) -> list:
    """从整题文本里解析 4 个选项 → ['选项文本', …]（不足 4 个返回 []）。"""
    marks = [(m.start(), m.group(1), m.end()) for m in OPT_HEAD.finditer(text)]
    marks = [m for m in marks if m[1] in "ABCDＡＢＣＤ"]
    if len(marks) < 2:
        return []
    # 取其最后一次连续 A..D 的四连（避免把正文里的字母误当选项）
    best = []
    for i in range(len(marks)):
        seq = marks[i:i + 4]
        letters = [s[1].translate(str.maketrans("ＡＢＣＤ", "ABCD")) for s in seq]
        if letters == ["A", "B", "C", "D"]:
            best = seq
    if not best:
        return []
    opts = []
    for i, (st, _l, en) in enumerate(best):
        end = best[i + 1][0] if i + 1 < len(best) else len(text)
        opts.append(text[en:end].strip())
    return opts


def stem_only(text: str) -> str:
    """题干＝第一个选项标记之前的部分（无选项则到第一个小问之前）。"""
    m = OPT_HEAD.search(text)
    if m and m.group(1) in "ABCDＡＢＣＤ":
        return text[:m.start()]
    for mm in re.finditer(r"[（(]\s*\d+\s*[）)]", text):
        if mm.start() > 12:                      # 跳过题号后紧跟的分值
            return text[:mm.start()]
    return text


# ============================================================================
# 三、题库索引与匹配
# ============================================================================

def build_db() -> list:
    """题库索引：[{id, path, stem(规范化), opts, ans, 分值, 题型}]"""
    db = []
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            fm, body, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            ids = as_list(fm.get("id"))
            if not ids:
                continue
            db.append({
                "id": str(ids[0]), "path": p,
                "stem": norm_text(stem_only(body)),
                "opts": parse_options(body),
                "ans": str(fm.get("答案", "")).strip(),
                "分值": fm.get("分值"), "题型": str(fm.get("题型", "")),
            })
    return db


def match_stem(stem: str, opts: list, db: list, topn: int = 5, pool: int = 40) -> list:
    """题干 ＋ 选项 综合匹配 → [(分数, 记录), …] 降序。

    两段式：先按题干粗排取前 `pool` 名入库，再对这批补算**选项相似度**（顺序无关，
    可容忍选项重排）后重排 —— 表格题 / 材料题的题干信息弱，靠选项能显著找回
    （2026-09-25 实测：月考第 18 题「据下表可知…」题干仅 12 字，纯题干匹配 0.23，
    加选项后命中正确题）。
    """
    target = norm_text(stem)
    rough = sorted(((difflib.SequenceMatcher(None, target, r["stem"]).ratio(), r)
                    for r in db), key=lambda x: -x[0])[:pool]
    topts = [norm_text(o, 40) for o in (opts or [])]
    topts = [o for o in topts if o]
    scored = []
    for s1, r in rough:
        if topts and r["opts"]:
            sims = [max(difflib.SequenceMatcher(None, t, norm_text(d, 40)).ratio()
                        for d in r["opts"]) for t in topts]
            score = 0.6 * s1 + 0.4 * (sum(sims) / len(sims))
        else:
            score = s1
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return scored[:topn]


def detect_order(paper_opts: list, db_opts: list) -> list:
    """检测选项重排 → `选项序`（新 A/B/C/D 位依次放**原第几项**）；未重排返回 []。"""
    if len(paper_opts) != 4 or len(db_opts) != 4:
        return []
    perm, used = [], set()
    for po in paper_opts:                        # 每个成品选项 → 题库里最像的那一项
        best, bi = -1.0, None
        a = norm_text(po, 40)
        for i, dbo in enumerate(db_opts):
            if i in used:
                continue
            sc = difflib.SequenceMatcher(None, a, norm_text(dbo, 40)).ratio()
            if sc > best:
                best, bi = sc, i
        if bi is None or best < 0.5:
            return []
        perm.append(bi + 1)
        used.add(bi)
    return [] if perm == [1, 2, 3, 4] else perm


ANS_LINE = re.compile(r"^\s*\d{1,2}\s*[．.、]\s*[A-DＡ-Ｄ](?:\s|　)*\d{1,2}\s*[．.、]\s*[A-DＡ-Ｄ]")


def extract_answers(lines: list) -> dict:
    """从成品卷提取答案连排行（成品卷卷内带答案时）→ {题号: 答案}。"""
    ans = {}
    for line in lines:
        if not ANS_LINE.match(line):
            continue
        for m in re.finditer(r"(\d{1,2})\s*[．.、]\s*([A-DＡ-Ｄ])", line):
            ans[int(m.group(1))] = m.group(2).translate(str.maketrans("ＡＢＣＤ", "ABCD"))
    return ans


# ============================================================================
# 四、主流程
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="从成品卷反查题库 id，还原组卷清单",
                                 allow_abbrev=False)   # 避免 --卷 被当成 --卷名 的缩写
    ap.add_argument("--卷", required=True, help="成品卷：.docx / .md / 文字版 .pdf")
    ap.add_argument("--输出", help="清单输出路径（默认 <卷名>_清单.yaml）")
    ap.add_argument("--阈值", type=float, default=0.45, help="题干匹配分数阈值（默认 0.45）")
    ap.add_argument("--候选数", type=int, default=5, help="低分题列出的候选数（默认 5）")
    ap.add_argument("--报告", action="store_true", help="只打印报告，不写清单")
    ap.add_argument("--卷名", help="清单里的卷名（默认取成品卷文件名）")
    args = ap.parse_args()

    src = Path(args.卷)
    if not src.exists():
        raise SystemExit(f"[失败] 找不到成品卷：{src}")
    suffix = src.suffix.lower()
    if suffix == ".docx":
        lines = read_docx_lines(src)
    elif suffix in (".md", ".markdown", ".txt"):
        lines = read_md_lines(src)
    elif suffix == ".pdf":
        lines = read_pdf_lines(src)
    else:
        raise SystemExit(f"[失败] 不支持的格式：{suffix}（支持 .docx/.md/.pdf）")

    qs = split_questions(lines)
    if len(qs) < 2:
        raise SystemExit("[失败] 未切出成卷题目——请确认是文字版卷面（扫描件需先 MinerU 提取）")
    print(f"[读卷] {src.name}：切出 {len(qs)} 题")

    db = build_db()
    print(f"[题库] {len(db)} 题（索引已建）")
    paper_ans = extract_answers(lines)

    ok, low = [], []
    print("\n=== 逐题匹配 ===")
    for no, text in qs:
        cands = match_stem(stem_only(text), parse_options(text), db, args.候选数)
        if not cands:
            low.append((no, text, []))
            print(f"{no:>3}  ✗ 题干为空")
            continue
        score, rec = cands[0]
        order = detect_order(parse_options(text), rec["opts"])
        ans_diff = ""
        if paper_ans.get(no) and rec["ans"]:
            paper_a = paper_ans[no]
            db_a = "".join(sorted(rec["ans"]))[:1]
            if paper_a != db_a:
                ans_diff = f"  ⚠ 答案差异（卷面 {paper_a} / 题库 {db_a}）"
        flag = "✓" if score >= args.阈值 else "?"
        print(f"{no:>3}  {flag} {score:.2f}  {rec['id']}"
              + (f"  选项序={order}" if order else "") + ans_diff)
        if score >= args.阈值:
            ok.append({"no": no, "id": rec["id"], "选项序": order,
                       "分值": rec["分值"], "原答案": rec["ans"],
                       "卷面答案": paper_ans.get(no, "")})
        else:
            low.append((no, text, cands))
            for sc, r in cands[:3]:
                print(f"        候选 {sc:.2f}  {r['id']}")

    print(f"\n[汇总] 命中 {len(ok)}／{len(qs)} 题（阈值 {args.阈值}）；"
          f"待人工确认 {len(low)} 题")
    if paper_ans:
        print(f"[答案] 从卷内提取到 {len(paper_ans)} 个选择题答案，已用于交叉校验")

    if args.报告:
        return 0

    vol = args.卷名 or re.sub(r"[_（(].*$", "", src.stem)
    out = Path(args.输出) if args.输出 else src.with_name(f"{vol}_清单.yaml")
    man = {"卷名": vol, "题目": []}
    by_no = {x["no"]: x for x in ok}
    for no, _text in qs:
        x = by_no.get(no)
        if not x:
            continue
        item = {"id": x["id"]}
        if x["分值"] not in (None, ""):
            item["分值"] = x["分值"]
        if x["选项序"]:
            item["选项序"] = x["选项序"]
        man["题目"].append(item)
    if yaml is None:
        out = out.with_suffix(".json")
        out.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        out.write_text(yaml.safe_dump(man, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
    print(f"[输出] {out}（{len(man['题目'])} 题）")
    if low:
        print("       ⚠ 以下题号未写入清单，需人工确认后补："
              + "、".join(str(n) for n, _, _ in low))
    print("       提示：清单只是起点——请核对分值/选项序，并用 --体检 复查答案分布")
    return 0


if __name__ == "__main__":
    sys.exit(main())
