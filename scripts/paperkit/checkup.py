# -*- coding: utf-8 -*-
"""paperkit.checkup — 卷面体检（结构、答案分布、覆盖度告警）。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import re
from collections import Counter

from qb_schema import as_list

from paperkit.balance import print_balance
from paperkit.markup import extract_images, split_stem_opts
from paperkit.sections import is_choice, rtype_of
from paperkit.slots import find_answer_slot
from paperkit.spec import effective_answer, short_lesson

# ===== 卷面教学结构体检（2026-10-29 补）=====
# 对标《关于高质量组卷的几点建议》（开花仙人掌 2026-10-29）的四条整卷要求：
# ① 材料形式多元（文字/表格/图像）② 主观题素养各有侧重、不重复
# ③ 通史时序顺排 ④ 整卷难度按分值合 7:2:1。四项**均为提示级**，不阻断出卷。
DIFF_TARGET = {"易": 0.70, "中": 0.20, "难": 0.10}   # 整卷按分值的**设计目标** 7:2:1
DIFF_TOL = 0.10                                   # 三档允许偏离设计目标 10 个百分点
# 真题难度**标注**基线（河南中考 2022-2026 实测，2026-10-29 统计；证据与口径见
# references/分析基线-洛阳.md「真题难度标注基线」）。⚠ 这是「人怎么标」的分布，不是难度
# 系数——2010-2021 那 398 题 100% 标「中」，2022 年起才分化；故用**选择题题量**比对最有分辨力。
DIFF_BASELINE = {
    "choice": {"易": 0.37, "中": 0.63, "难": 0.00},   # 选择题题量口径（n=100）
    "total": {"易": 0.15, "中": 0.75, "难": 0.10},    # 整卷分值口径（300 分）
}
BASE_GAP = 0.15                                   # 选择题易题低于基线 15 个点才告警
OPEN_TYPES = {"论述题", "综合探究题"}                # 开放性题型（设问梯度靠它撑）
BOOK_ORDER = ["7上", "7下", "8上", "8下", "9上", "9下",
              "纲要上", "纲要下", "选必1", "选必2", "选必3", "选修1", "选修2"]
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$")


def checkup(man: dict, recs: list) -> None:
    from collections import Counter
    total = sum(int(r["分值"]) for r in recs if str(r["分值"]).isdigit())
    ttypes = Counter(str(r["fm"].get("题型", "?")) for r in recs)
    diff = Counter(str(r["fm"].get("难度", "?")) for r in recs)
    ch = [r for r in recs if is_choice(r)]
    ans = Counter(effective_answer(r) for r in ch)
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
    seq = [effective_answer(r) for r in ch]
    run_same, run_start, prev = 1, 0, (seq[0] if seq else "")
    for i in range(1, len(seq)):
        if seq[i] and seq[i] == prev:
            run_same += 1
        else:
            if run_same >= 3:
                print(f"     ⚠ 选择题第 {run_start + 1}—{i} 题答案连续同为 {prev}")
            run_same, run_start = 1, i
        prev = seq[i]
    if run_same >= 3:
        print(f"     ⚠ 选择题第 {run_start + 1}—{len(seq)} 题答案连续同为 {prev}")
    if ans:
        spread = max(ans.values()) - min(ans.values()) if len(ans) > 1 else 0
        if spread > max(1, len(ch) * 0.15):
            print(f"     → 分布偏斜（极差 {spread}）；可按下列方案加「选项序」，或直接跑 `--均衡选项`")
            print_balance(recs)
    _structural(recs)


# ---------------------------------------------------------------------------
# 卷面教学结构体检（四项）
# ---------------------------------------------------------------------------

def _has_md_table(body: str) -> bool:
    """md 表格判据：某行含 `|`，且下一行是 `---|---` 型分隔行。

    ⚠ 不能只判「有没有 `|`」——题干、答案里都可能有竖线；`---|---` 分隔行才是表格的
    独有特征。也兼容行首不带 `|` 的写法（转录稿偶见），只用于统计、不影响渲染。
    """
    lines = body.split("\n")
    return any("|" in lines[i] and _TABLE_SEP_RE.match(lines[i + 1])
               for i in range(len(lines) - 1))


def material_form(r: dict) -> str:
    """正文材料形式 → 图 / 表格 / 纯文字（图优先）。

    表格两种写法都算：md 表格（题库录入体例）与 HTML `<table>`（MinerU 转录体例）。
    """
    body = r.get("body") or ""
    if extract_images(body)[1]:
        return "图"
    if "<table" in body.lower() or _has_md_table(body):
        return "表格"
    return "纯文字"


def _lesson_key(r: dict, bidx: dict):
    """按时序返回 (册序, 册内课序)；非内容课或词表缺项 → None（不参与时序判定）。

    跳过的三类：学史方法条目（`如何…`，跨册使用无时序义）、活动课（册末另立）、
    词表未收录或不识别的册别。
    """
    lessons, books = as_list(r["fm"].get("课")), as_list(r["fm"].get("册别"))
    if not lessons or not books:
        return None
    name, bk = lessons[0], books[0]
    if name.startswith("如何") or "活动课" in name or bk not in BOOK_ORDER:
        return None
    lst = bidx.get(bk) or []
    return (BOOK_ORDER.index(bk), lst.index(name)) if name in lst else None


def _structural(recs: list) -> None:
    """材料形式 / 主观题素养 / 时序 / 难度比例——四项整卷层面的教学结构体检。"""
    nonch = [r for r in recs if not is_choice(r)]

    # ① 材料形式多元（文章：文字史料、地图、表格、图片、漫画等）
    forms = Counter(material_form(r) for r in recs)
    nf = Counter(material_form(r) for r in nonch)
    tail = (f"（非选择 {len(nonch)} 题：纯文字 {nf.get('纯文字', 0)}／"
            f"表格 {nf.get('表格', 0)}／图 {nf.get('图', 0)}）") if nonch else ""
    print(f"材料形式 纯文字 {forms.get('纯文字', 0)} · 表格 {forms.get('表格', 0)} · "
          f"图 {forms.get('图', 0)} {tail}")
    if forms.get("表格", 0) + forms.get("图", 0) == 0:
        print("     ⚠ 全卷无表格、无图像材料 → 材料形式单一（宜各留 1 道图表题、图像题）")
    elif nonch and nf.get("纯文字", 0) == len(nonch):
        print("     ⚠ 非选择题全为纯文字材料 → 建议换 1 题带表格/图像的（史料呈现宜多元）")

    # ② 主观题素养是否各有侧重（文章：不重复考查同一素养）
    if nonch:
        sub_ab = Counter(ab for r in nonch
                         for ab in as_list(r["fm"].get("关键能力")))
        dup = [(ab, c) for ab, c in sub_ab.items() if c >= 2]
        if dup:
            print("     ⚠ 非选择题同一关键能力重复："
                  + "、".join(f"{ab}×{c}" for ab, c in dup)
                  + "（各大题素养宜各有侧重）")
        n_open = sum(1 for r in nonch if rtype_of(r) in OPEN_TYPES)
        if n_open == 0:
            print("     ⚠ 非选择题无论述题/综合探究题 → 全为封闭性设问，开放性与设问梯度不足")
    lack = sum(1 for r in recs if not as_list(r["fm"].get("关键能力")))
    if lack:
        print(f"     · 关键能力未标注 {lack} 题（能力重复检测不完整，不影响出卷）")

    # ③ 通史时序顺排（文章：降低学生阅读的思维跳跃感）
    try:
        from qb_schema import load_book_index
        bidx = load_book_index()
    except Exception:
        bidx = None
        print("     · 课序词表读取失败，跳过时序检测")
    if bidx:
        seq = [(n, r, _lesson_key(r, bidx)) for n, r in enumerate(recs, 1)]
        seq = [x for x in seq if x[2] is not None]
        inv = []
        for grp in (True, False):          # ⚠ 只在同一题型块内比较：选择题覆盖全册、
            block = [x for x in seq if is_choice(x[1]) == grp]   # 材料题按专题组合，
            inv += [(a, b) for a, b in zip(block, block[1:])     # 跨大题回退属正常结构
                    if b[2] < a[2]]
        if inv:
            print(f"     → 时序逆序 {len(inv)} 处（宜顺排；跨册题按落点册排）")
            for (n1, r1, _), (n2, r2, _) in inv[:5]:
                print(f"        第{n1}题（{short_lesson(as_list(r1['fm'].get('课'))[0])}）"
                      f" → 第{n2}题（{short_lesson(as_list(r2['fm'].get('课'))[0])}）")

    # ④ 难度结构：**双口径**——分值对设计目标（7:2:1），选择题题量对真题基线
    #    ⚠ 分值口径必然被非选择题放大：5 道材料题各 12 分、整题只能标一档，中档天然 ≥60%。
    #    故分值偏离降为提示，真正可执行的是「选择题基础题够不够」这条。
    ds, no_diff = Counter(), 0
    for r in recs:
        v = r["分值"]
        if not str(v).isdigit():
            continue
        lvl = str(r["fm"].get("难度") or "").strip()
        if lvl in DIFF_TARGET:
            ds[lvl] += int(v)
        else:
            no_diff += int(v)
    total = sum(ds.values())                # ⚠ 分母只算三档：难度未标注的题不计入，
    if total:                               # 否则占比偏低会误报「某档不足」
        parts = " · ".join(f"{k} {ds.get(k, 0)}分({ds.get(k, 0) / total:.0%})" for k in ("易", "中", "难"))
        print(f"难度分值 {parts}　设计目标 7:2:1")
        dev = [(k, ds.get(k, 0) / total - tgt) for k, tgt in DIFF_TARGET.items()
               if abs(ds.get(k, 0) / total - tgt) >= DIFF_TOL]
        if dev:
            print("     · 分值口径偏离设计目标：" + "、".join(f"{k} {g * 100:+.0f} 点" for k, g in dev)
                  + "（非选择题整题只标一档会放大此差，**作参考作不了准**）")
        if no_diff:
            print(f"     · 另有 {no_diff} 分难度未标注，未计入比例")
    ch = [r for r in recs if is_choice(r)]
    if ch:
        n = len(ch)
        cd = Counter(str(r["fm"].get("难度") or "?").strip() for r in ch)
        print("选择题 " + " · ".join(f"{k} {cd.get(k, 0)}({cd.get(k, 0) / n:.0%})" for k in ("易", "中", "难"))
              + f"　真题基线（河南中考2022-2026，{n} 题≈易 {round(n * DIFF_BASELINE['choice']['易'])} 道）")
        gap = cd.get("易", 0) / n - DIFF_BASELINE["choice"]["易"]
        if gap <= -BASE_GAP:
            print(f"     ⚠ 选择题基础题偏少：易 {cd.get('易', 0)} 道，按真题基线应约 "
                  f"{round(n * DIFF_BASELINE['choice']['易'])} 道（差 {round(-gap * n)} 道）")
        if no_diff:
            print(f"     · 另有 {no_diff} 分难度未标注，未计入比例")
