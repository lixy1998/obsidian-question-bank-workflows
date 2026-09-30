# -*- coding: utf-8 -*-
"""paperkit.sections — 题型判定、分值、大题标题与分节标记。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
`rtype_of` / `is_choice` 是卷面各处（md / Word / 答题卡 / 体检）共用的唯一实现。
"""
import re
from collections import Counter


def rtype_of(r: dict) -> str:
    return str(r["fm"].get("题型", "")).strip()


def is_choice(r: dict) -> bool:
    return rtype_of(r) == "选择题"


def sub_scores(body: str, total, n: int) -> list:
    """各小问分值：优先取设问末尾的「（X分）」；取不齐则把整题分值均分。"""
    sc = [int(x) for x in re.findall(r"[（(]\s*(\d+)\s*分[^）)]*[）)]", body)]
    if len(sc) == n:                                 # 只在数量吻合时采信，避免误抓材料里的分数
        return sc
    if isinstance(total, int) and total > 0 and n > 0:
        base, rest = divmod(total, n)
        return [base + (1 if i < rest else 0) for i in range(n)]
    return [None] * n


def blanks_for(score, fixed: int = 0) -> int:
    """答题卡：某小问的留白行数。

    fixed > 0 → 用固定行数（命令行 `--答题卡行数`）；
    否则**按分值算**：约 2 分/行、下限 2 行（对齐参考卡「12 分题 ÷ 3 小问 = 每问 2 行」的实测密度）。
    """
    if fixed and fixed > 0:
        return fixed
    if not score or score <= 0:
        return 2
    return max(2, round(score / 2))


def big_title(recs: list, choice: bool) -> str:
    """大题标题（《试卷排版规范》§二）：选择题给「每小题X分，共Y分」，非选择题给总分。"""
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
