# -*- coding: utf-8 -*-
"""paperkit.balance — 选择题答案分布诊断与自动均衡方案。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
from paperkit.sections import is_choice
from paperkit.spec import effective_answer


def balance_options(recs: list) -> list:
    """诊断选择题答案分布并给出重排方案。

    → [(卷面题号, 现答案, 新答案, 选项序)]；已带「选项序」的题视为人工已定，不再改动。
    目标：各字母计数尽量相等（余数补给当前较少的字母），且尽量避免三连及以上同项。
    """
    ch = [(i, r) for i, r in enumerate(recs, 1) if is_choice(r)]
    if len(ch) < 6:
        return []
    seq = [effective_answer(r) for _, r in ch]
    fixed = [bool(r.get("选项序")) for _, r in ch]
    n = len(ch)

    quota = {c: n // 4 for c in "ABCD"}
    for c in sorted("ABCD", key=lambda x: seq.count(x))[:n % 4]:
        quota[c] += 1
    for i, f in enumerate(fixed):
        if f and seq[i] in quota:
            quota[seq[i]] = max(0, quota[seq[i]] - 1)

    target = list(seq)
    for i in range(n):
        if fixed[i]:
            continue
        cand = [c for c in "ABCD" if quota[c] > 0] or list("ABCD")
        pick = None
        if seq[i] in cand and not (i >= 2 and target[i - 1] == seq[i] and target[i - 2] == seq[i]):
            pick = seq[i]
        if pick is None:
            def penalty(c, i=i):
                p = 1 if (i >= 1 and target[i - 1] == c) else 0
                if i >= 2 and target[i - 2] == c:
                    p += 3
                if i + 1 < n and target[i + 1] == c:
                    p += 1
                return p
            pick = min(cand, key=penalty)
        target[i] = pick
        quota[pick] -= 1

    plan = []
    for i, (no, _r) in enumerate(ch):
        if fixed[i] or target[i] == seq[i]:
            continue
        a, b = "ABCD".index(seq[i]), "ABCD".index(target[i])
        perm = [1, 2, 3, 4]
        perm[a], perm[b] = perm[b], perm[a]
        plan.append((no, seq[i], target[i], perm))
    return plan


def print_balance(recs: list, ch_offset: int = 0) -> None:
    """打印均衡方案（供写回清单「选项序」）。"""
    plan = balance_options(recs)
    if not plan:
        return
    print(f"     → 均衡方案：{len(plan)} 题需重排（在清单对应项下加「选项序」即可，答案自动换算）")
    for no, old, new, perm in plan:
        print(f"        第 {no} 题　{old} → {new}　选项序: [{' , '.join(map(str, perm))}]")


def balance_options(recs: list) -> list:
    """诊断选择题答案分布并给出重排方案。

    → [(卷面题号, 现答案, 新答案, 选项序)]；已带「选项序」的题视为人工已定，不再改动。
    目标：各字母计数尽量相等（余数补给当前较少的字母），且尽量避免三连及以上同项。
    """
    ch = [(i, r) for i, r in enumerate(recs, 1) if is_choice(r)]
    if len(ch) < 6:
        return []
    seq = [effective_answer(r) for _, r in ch]
    fixed = [bool(r.get("选项序")) for _, r in ch]
    n = len(ch)

    quota = {c: n // 4 for c in "ABCD"}
    for c in sorted("ABCD", key=lambda x: seq.count(x))[:n % 4]:
        quota[c] += 1
    for i, f in enumerate(fixed):
        if f and seq[i] in quota:
            quota[seq[i]] = max(0, quota[seq[i]] - 1)

    target = list(seq)
    for i in range(n):
        if fixed[i]:
            continue
        cand = [c for c in "ABCD" if quota[c] > 0] or list("ABCD")
        pick = None
        if seq[i] in cand and not (i >= 2 and target[i - 1] == seq[i] and target[i - 2] == seq[i]):
            pick = seq[i]
        if pick is None:
            def penalty(c, i=i):
                p = 1 if (i >= 1 and target[i - 1] == c) else 0
                if i >= 2 and target[i - 2] == c:
                    p += 3
                if i + 1 < n and target[i + 1] == c:
                    p += 1
                return p
            pick = min(cand, key=penalty)
        target[i] = pick
        quota[pick] -= 1

    plan = []
    for i, (no, _r) in enumerate(ch):
        if fixed[i] or target[i] == seq[i]:
            continue
        a, b = "ABCD".index(seq[i]), "ABCD".index(target[i])
        perm = [1, 2, 3, 4]
        perm[a], perm[b] = perm[b], perm[a]
        plan.append((no, seq[i], target[i], perm))
    return plan


def print_balance(recs: list, ch_offset: int = 0) -> None:
    """打印均衡方案（供写回清单「选项序」）。"""
    plan = balance_options(recs)
    if not plan:
        return
    print(f"     → 均衡方案：{len(plan)} 题需重排（在清单对应项下加「选项序」即可，答案自动换算）")
    for no, old, new, perm in plan:
        print(f"        第 {no} 题　{old} → {new}　选项序: [{' , '.join(map(str, perm))}]")
