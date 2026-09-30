# -*- coding: utf-8 -*-
"""paperkit.md_render — md 学生卷 / 教师卷渲染。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import re

from paperkit.images import (MD_IMG_ANY_RE, WK_IMG_ANY_RE, _is_img_ref, _repl_std,
                            iter_img_refs, rewrite_img)
from paperkit.sections import is_choice, section_marks
from paperkit.slots import find_answer_slot, opts_md, reorder_opts


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
        if mode == "教师":      # v4：命题说明已独立成文档，本分支不再触达（保留供复用）
            fm = r["fm"]
            tail = [f"**答案**：{r['_ans']}"]
            jx = str(fm.get("解析", "") or "").strip()
            if jx:
                # 解析里可能内嵌图（实测 2024河南中考-01、2025河南中考-01）：
                # 与题干重复的直接去掉（否则教师卷重出一遍），独有的保留并改写命名空间
                # （v1.22.0：多形态识别，按 basename 比对；保留项统一改写标准 md 形态）
                same = {ref.split("/")[-1] for ref, _ in iter_img_refs(r["body"])}

                def _drop_md(m):
                    p = m.group(1).strip().replace("\\", "/")
                    if not _is_img_ref(p):
                        return m.group(0)
                    return "" if p.split("/")[-1] in same else _repl_std(p)

                def _drop_wk(m):
                    p = m.group(1).strip().replace("\\", "/")
                    if not _is_img_ref(p):
                        return m.group(0)
                    return "" if p.split("/")[-1] in same else _repl_std(p)

                jx = MD_IMG_ANY_RE.sub(_drop_md, jx)
                jx = WK_IMG_ANY_RE.sub(_drop_wk, jx)
                jx = jx.strip()
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
