# -*- coding: utf-8 -*-
"""paperkit.spec — 命题说明（题目来源与考察角度）的表格数据加工。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import re

from qb_schema import as_list

from paperkit.images import iter_img_refs
from paperkit.sections import is_choice


SRC_TYPE_TAGS = {"真题", "模拟卷", "期末卷", "期中卷", "月考卷", "原创题",
                 "教材改编", "中考改编", "阶段测试", "练习卷", "联考卷", "期末"
                 }


CHOICE_COLS = ["A1-识记", "A2-说明", "A3-概括", "B1-比较", "B2-分析", "B3-评价／C 类"]


KP_DROP_RE = re.compile(r"已弃用|待核|存疑|待删|废弃|新教材已")


_LESSON_RE = re.compile(r"第\s*(\d+)\s*课")


def effective_answer(r: dict) -> str:
    """清单「选项序」换算后的答案字母（未重排则取题库答案）。"""
    old = str(r["fm"].get("答案", "")).strip()
    seq = r.get("选项序")
    if not seq or len(old) != 1 or old not in "ABCD":
        return old
    perm = [int(x) - 1 for x in seq]
    if len(perm) != 4 or sorted(perm) != [0, 1, 2, 3]:
        return old
    return "ABCD"[perm.index("ABCD".index(old))]


def short_lesson(name) -> str:
    """课名取「第X课」前缀，用于兼涉标注等紧凑场景。"""
    m = _LESSON_RE.match(str(name).strip())
    return f"第{m.group(1)}课" if m else str(name).strip()


def lesson_no(name):
    m = _LESSON_RE.search(str(name))
    return int(m.group(1)) if m else None


def lesson_cell(r: dict) -> str:
    """考查课：首项＝主考；其余为兼涉（须在材料/设问中可指认，选项不算）。"""
    ls = [x for x in as_list(r["fm"].get("课")) if str(x).strip()]
    if not ls:
        return ""
    head, rest = str(ls[0]).strip(), [short_lesson(x) for x in ls[1:]]
    return head + ("（兼涉" + "、".join(rest) + "）" if rest else "")


def clean_src(r: dict) -> str:
    """题目来源：题库卷名（剔除类型标签后的首项）。"""
    srcs = [str(x).strip() for x in as_list(r["fm"].get("来源"))
            if str(x).strip() and str(x).strip() not in SRC_TYPE_TAGS]
    return srcs[0] if srcs else str(r["vol"])


def angle_cell(r: dict) -> str:
    """考查角度：清单「角度」优先，否则取知识点前两项（剔除内部标注项）。"""
    if r.get("角度"):
        return str(r["角度"])
    kps = [str(x).strip() for x in as_list(r["fm"].get("知识点"))
           if str(x).strip() and not KP_DROP_RE.search(str(x))]
    return "｜".join(kps[:2])


def scope_cell(r: dict) -> str:
    """非选择题考查范围：主考课＋兼涉课（跨册时带上册别，避免「第9课」歧义）。"""
    ls = [str(x).strip() for x in as_list(r["fm"].get("课")) if str(x).strip()]
    if not ls:
        return "—"
    books = [str(x).strip() for x in as_list(r["fm"].get("册别"))]
    head_book = books[0] if books else ""
    rest = []
    for i, c in enumerate(ls[1:], start=1):
        bk = books[i] if i < len(books) else (books[-1] if books else "")
        tag = short_lesson(c)
        if bk and bk != head_book:
            tag = f"{bk}{tag}"
        rest.append(tag)
    return ls[0] + ("（兼涉" + "、".join(rest) + "）" if rest else "")


def ability_cells(r: dict) -> list:
    """关键能力 → 表内文本（多值以「／」连接）。"""
    ks = [str(x).strip() for x in as_list(r["fm"].get("关键能力")) if str(x).strip()]
    return "／".join(ks) if ks else "—"


def ability_bucket(key: str) -> str:
    """关键能力归并到 9 要素六列（C 类合并）。"""
    k = str(key).strip().upper()
    for pre, col in (("A1", "A1-识记"), ("A2", "A2-说明"), ("A3", "A3-概括"),
                     ("B1", "B1-比较"), ("B2", "B2-分析"), ("B3", "B3-评价")):
        if k.startswith(pre):
            return "B3-评价／C 类" if col == "B3-评价" else col
    if k.startswith(("C1", "C2", "C3")):
        return "B3-评价／C 类"
    return ""


def sub_questions(body: str, total=None, override=None, limit: int = 46) -> list:
    """非选择题小问 → [(号, 文字, 分值)]。

    分值口径（与答题卡留白同一套判定）：清单「小问分值」> 正文自带分值之和恰等于卷面分值
    > 按卷面分值均分（避免原卷赋分与本卷赋分不符时细目表失真）。
    """
    txt = re.sub(r"<[^>]+>", " ", body)
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    raw = []
    for p in re.split(r"(?=[（(]\d+[）)])", txt):
        m = re.match(r"^[（(](\d+)[）)]\s*(.*)$", p.strip())
        if not m:
            continue
        seg = m.group(2).strip()
        sc = re.search(r"[（(]\s*(\d+)\s*分[^）)]*[）)]", seg)
        seg = re.split(r"[（(]\s*\d+\s*分", seg)[0].strip().strip("，,。；;、 ")
        if len(seg) > limit:
            seg = seg[:limit] + "…"
        raw.append((m.group(1), seg, int(sc.group(1)) if sc else None))
    n = len(raw)
    if not n:
        return []
    scores = None
    if override and len(override) == n:
        scores = [int(x) for x in override]
    elif str(total).strip().isdigit() and all(x[2] is not None for x in raw) \
            and sum(x[2] for x in raw) == int(total):
        scores = [x[2] for x in raw]
    elif str(total).strip().isdigit():
        base, rest = divmod(int(total), n)
        scores = [base + (1 if i < rest else 0) for i in range(n)]
    return [(a, b, scores[i] if scores else c) for i, (a, b, c) in enumerate(raw)]


def spec_lines(man: dict, recs: list) -> list:
    """生成「命题说明（题目来源与考察角度）」的 md 行列表。"""
    from collections import Counter, OrderedDict
    ch = [r for r in recs if is_choice(r)]
    nc = [r for r in recs if not is_choice(r)]
    tot = sum(int(r["分值"]) for r in recs if str(r["分值"]).strip().isdigit())
    ch_tot = sum(int(r["分值"]) for r in ch if str(r["分值"]).strip().isdigit())
    nc_tot = sum(int(r["分值"]) for r in nc if str(r["分值"]).strip().isdigit())

    out = []
    title = man.get("标题") or man.get("卷名") or "组卷"
    out.append(f"# {title}")
    out.append("")
    out.append("## 命题说明（题目来源与考察角度）")
    out.append("")
    out.append("（内部资料，不随试卷印发）")
    out.append("")

    # ------------------------------ 一、试卷结构与命题思路
    out.append("### 一、试卷结构与命题思路")
    out.append("")
    out.append("**1．卷面结构**")
    out.append("")
    if ch:
        per = Counter(str(r["分值"]).strip() for r in ch).most_common(1)[0][0]
        if per.isdigit() and int(per) * len(ch) == ch_tot:
            ch_desc = f"选择题 {len(ch)} 小题（每小题 {per} 分，共 {ch_tot} 分）"
        else:
            ch_desc = f"选择题 {len(ch)} 小题（共 {ch_tot} 分）"
    else:
        ch_desc = "本卷不含选择题"
    nc_desc = (f"非选择题 {len(nc)} 小题（共 {nc_tot} 分）" if nc else "本卷不含非选择题")
    head_note = ""
    for n in (man.get("说明") or []):
        if re.search(r"满分|用时|时间", str(n)):
            head_note = f"（卷头注明「{str(n).strip()}」）"
            break
    out.append(f"本卷共 {len(recs)} 小题，满分 {tot} 分。{ch_desc}；{nc_desc}。{head_note}")
    out.append("")

    # 2．各部分比例安排（按单元）
    out.append("**2．各部分比例安排**")
    out.append("")
    groups = OrderedDict()
    for r in recs:
        us = [str(x).strip() for x in as_list(r["fm"].get("单元")) if str(x).strip()]
        key = us[0] if us else "（未标注单元）"
        groups.setdefault(key, []).append(r)
    has_unit = any(k != "（未标注单元）" for k in groups)
    if has_unit:
        out.append("| 单元 | 课时 | 选择题 | 分值 | 涵盖课 |")
        out.append("|---|---|---|---|---|")
        for u, rs in groups.items():
            sel = [r for r in rs if is_choice(r)]
            s = sum(int(r["分值"]) for r in rs if str(r["分值"]).strip().isdigit())
            # 课时范围与涵盖课只取**主考课**（首项）——兼涉可能是跨册课（如 7上第9课），会污染范围
            main = [str(as_list(r["fm"].get("课"))[0]).strip()
                    for r in rs if as_list(r["fm"].get("课"))]
            nos = sorted({n for n in (lesson_no(x) for x in main) if n})
            span = (f"第{nos[0]}—{nos[-1]}课" if len(nos) > 1
                    else f"第{nos[0]}课" if nos else "—")
            covered = "、".join(dict.fromkeys(
                x.split(" ", 1)[1] if " " in x else x for x in main)) or "—"
            out.append(f"| {u} | {span} | {len(sel)}题 | {s}分 | {covered} |")
        out.append(f"| 合计 | {len(recs)}题 | {len(ch)}题 | {tot}分 | — |")
        out.append("")

    # 3．学科能力维度
    out.append("**3．学科能力维度（郑林 9 要素）**")
    out.append("")
    bucket = Counter()
    for r in recs:
        for k in as_list(r["fm"].get("关键能力")):
            col = ability_bucket(k)
            if col:
                bucket[col] += 1
    n_b = sum(bucket.values()) or 1
    out.append("| 能力要素 | " + " | ".join(CHOICE_COLS) + " |")
    out.append("|---|" + "---|" * len(CHOICE_COLS))
    out.append("| 题量（含兼涉） | "
               + " | ".join(f"{bucket.get(c, 0)}题" for c in CHOICE_COLS) + " |")
    out.append("| 占比 | "
               + " | ".join(f"{round(bucket.get(c, 0) * 100 / n_b)}%" for c in CHOICE_COLS)
               + " |")
    out.append("")
    top = bucket.most_common(2)
    if top:
        pair = "＋".join(c for c, _ in top)
        share = round(sum(v for _, v in top) * 100 / n_b)
        out.append(f"以 {pair} 为主，合计约 {share}%（含兼涉口径，一题多要素重复计入）。")
        out.append("")

    # 4．难度与答案分布
    out.append("**4．难度与答案分布**")
    out.append("")
    diff = Counter(str(r["fm"].get("难度", "?")).strip() for r in recs)
    d_desc = "、".join(f"{k} {v} 题" for k, v in
                       sorted(diff.items(), key=lambda kv: "易中难".find(kv[0][:1])))
    out.append(f"难度：{d_desc}。")
    out.append("")
    if ch:
        ans = Counter(effective_answer(r) for r in ch)
        ans = Counter({k: v for k, v in ans.items() if k in "ABCD"})
        a_desc = "、".join(f"{k} {v} 题" for k, v in sorted(ans.items()))
        seq = [effective_answer(r) for r in ch]
        runs, cur, start = [], 1, 0
        for i in range(1, len(seq)):
            if seq[i] and seq[i] == seq[i - 1]:
                cur += 1
            else:
                runs.append((start + 1, i, seq[start], cur))
                cur, start = 1, i
        runs.append((start + 1, len(seq), seq[start], cur))
        longest = max((x[3] for x in runs), default=1)
        tail = "；最长连续同项 " + "、".join(
            f"第{s}—{e}题 {a}（{n}连）" for s, e, a, n in runs if n == longest)
        long = [x for x in runs if x[3] >= 3 and x[3] != longest]
        if long:
            tail += "，另有 " + "、".join(f"第{s}—{e}题 {a}（{n}连）" for s, e, a, n in long)
        out.append(f"答案分布：{a_desc}{tail}。")
        if len(ch) >= 8 and (max(ans.values()) - min(ans.values())) > max(1, len(ch) * 0.15):
            out.append("")
            out.append("⚠ 分布偏斜，建议用 `--均衡选项` 生成「选项序」后写回清单，再重出本说明。")
        out.append("")

    # 5．重复规避（有「排除卷」信息才出）
    excl = man.get("排除卷") or man.get("去重卷")
    if excl:
        out.append("**5．与已用卷的重复规避**")
        out.append("")
        out.append(f"组卷前已比对：{excl}；本卷题目与该卷无一重复。")
        out.append("")

    # ------------------------------ 二、选择题命题细目
    if ch:
        out.append(f"### 二、选择题命题细目（第 1—{len(ch)} 题）")
        out.append("")
        out.append("| 题号 | 考查课 | 考查角度 | 关键能力 | 难度 | 题目来源 |")
        out.append("|---|---|---|---|---|---|")
        for i, r in enumerate(ch, 1):
            out.append(f"| {i} | {lesson_cell(r)} | {angle_cell(r)} | {ability_cells(r)} | "
                       f"{str(r['fm'].get('难度', '')).strip()} | {clean_src(r)} |")
        out.append("")
        moved = [i for i, r in enumerate(ch, 1) if r.get("选项序")]
        if moved:
            out.append(f"注：第 {'、'.join(map(str, moved))} 题选项顺序已重排"
                       f"（见第五部分）；「考查角度」前列为该题的核心落点。")
        else:
            out.append("注：「考查角度」前列为该题的核心落点。")
        out.append("")

    # ------------------------------ 三、非选择题命题细目
    if nc:
        base = len(ch)
        out.append(f"### 三、非选择题命题细目（第 {base + 1}—{len(recs)} 题）")
        out.append("")
        out.append("| 题号 | 考查范围 | 考查角度 | 设问与分值 | 关键能力 | 难度 | 题目来源 |")
        out.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(nc, base + 1):
            subs = sub_questions(r["body"], r["分值"], r.get("小问分值"))
            desc = " ".join(
                f"({a}) {b}（{c}分）" if c else f"({a}) {b}" for a, b, c in subs) or "—"
            rng = scope_cell(r)
            out.append(f"| {i} | {rng} | {angle_cell(r)} | {desc} | {ability_cells(r)} | "
                       f"{str(r['fm'].get('难度', '')).strip()} | {clean_src(r)} |")
        out.append("")

    # ------------------------------ 四、参考答案与评分标准
    out.append("### 四、参考答案与评分标准")
    out.append("")
    if ch:
        per = Counter(str(r["分值"]).strip() for r in ch).most_common(1)[0][0]
        cap = f"（每小题 {per} 分，共 {ch_tot} 分）" if per.isdigit() else f"（共 {ch_tot} 分）"
        out.append(f"**1．选择题**{cap}")
        out.append("")
        ans = [f"{i}．{effective_answer(r)}" for i, r in enumerate(ch, 1)]
        for k in range(0, len(ans), 10):
            out.append("\u3000".join(ans[k:k + 10]))
        out.append("")
    if nc:
        out.append(f"**2．非选择题**（共 {nc_tot} 分）")
        out.append("")
        for i, r in enumerate(nc, len(ch) + 1):
            a = str(r["fm"].get("答案", "") or "").strip()
            a = re.sub(r"\n{2,}", "\n", a)
            a = a.replace("\n", " ")
            out.append(f"{i}．{a}")
            out.append("")

    # ------------------------------ 五、组卷处理说明与待办
    out.append("### 五、组卷处理说明与待办")
    out.append("")
    items = ["试卷不标注题目来源（按命题要求）；材料出处「——摘编自……」属材料本身的"
             "引用信息，予以保留。题目来源见本说明第二、三部分。"]
    changed = [(i, r) for i, r in enumerate(recs, 1)
               if str(r["分值"]).strip().isdigit()
               and str(r["fm"].get("分值", "")).strip().isdigit()
               and int(r["分值"]) != int(r["fm"]["分值"])]
    if changed:
        items.append("分值调整：第 " + "、".join(str(i) for i, _ in changed)
                     + " 题的卷面分值与原题不同（按本卷总分结构统一赋分），设问文字未作改动。")
    moved = [(i, r) for i, r in enumerate(recs, 1) if r.get("选项序")]
    if moved:
        parts = []
        for i, r in moved:
            old = str(r["fm"].get("答案", "")).strip()
            seq = [int(x) - 1 for x in r["选项序"]]
            pos = "ABCD"[seq.index("ABCD".index(old))] if old in "ABCD" else "?"
            parts.append(f"第 {i} 题正确项由 {old} 位移至 {pos} 位")
        items.append(f"选项重排：为均衡全卷答案分布，对 {len(moved)} 道题作了选项顺序调整——"
                     + "，".join(parts)
                     + "。均为同题内部选项内容互换，题干与干扰项文字未变；"
                       "学生卷与参考答案使用同一版本。")
    mismatch = []
    for i, r in enumerate(nc, len(ch) + 1):
        a = str(r["fm"].get("答案", "") or "")
        got = [int(x) for x in re.findall(r"[（(]\s*(\d+)\s*分", a)]
        if got and str(r["分值"]).strip().isdigit() and sum(got) != int(r["分值"]):
            mismatch.append(i)
    if mismatch:
        items.append("答案分值核对：第 " + "、".join(map(str, mismatch))
                     + " 题题库答案文本自带的分值与本卷赋分值不同，"
                       "请按卷面分值核对评分标准文字。")
    pics = [i for i, r in enumerate(recs, 1) if iter_img_refs(r["body"])]
    if pics:
        items.append(f"图片处理：第 {'、'.join(map(str, pics))} 题含图，"
                     "图片取用题库内原卷图，统一放输出目录的 `images/{卷名}/`。"
                     "卷面撤图或换图的，请以卷面实际为准。")
    noans = [i for i, r in enumerate(recs, 1) if not str(r["fm"].get("答案", "")).strip()]
    if noans:
        items.append(f"待办：第 {'、'.join(map(str, noans))} 题题库未标答案，请补录后再用。")
    items.append("版式：正文宋体小四、固定行距 24 磅，辅文五号，符合 GB 40070-2021 "
                 "字号与行空要求；成卷后跑 `verify_paper_layout.py --docx` 复核。")
    for k, it in enumerate(items, 1):
        out.append(f"{k}．{it}")
        out.append("")
    return out
