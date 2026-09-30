# -*- coding: utf-8 -*-
"""paperkit.checkup — 卷面体检（结构、答案分布、覆盖度告警）。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
from collections import Counter

from qb_schema import as_list

from paperkit.balance import print_balance
from paperkit.markup import extract_images, split_stem_opts
from paperkit.sections import is_choice
from paperkit.slots import find_answer_slot
from paperkit.spec import effective_answer


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
