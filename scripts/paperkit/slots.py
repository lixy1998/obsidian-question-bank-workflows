# -*- coding: utf-8 -*-
"""paperkit.slots — 占位符（图片/表格行）与作答空位、选项重排。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import re

from paperkit.markup import TBL_SLOT_RE, is_tbl_slot  # noqa: F401  (对外再导出)

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
    ⚠ **图片占位行与表格占位行都不参与判定**：
      · 图片占位行不排除 → 括号补到图片位置上（实测踩坑）；
      · 表格占位行不排除 → 题干末行是表格时，括号被塞进表格内（2026-09-26 用户实测：
        多图题题干末行为表格，卷面出现「| 图注 |（　　）」）→ 现按「表格前最后一个文本行」补。
    """
    if any("（　　）" in l for l in lines):
        return -1
    if mode == "无":
        return -2
    rows = [i for i, l in enumerate(lines)
            if not is_img_slot(l) and not is_tbl_slot(l)]
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
