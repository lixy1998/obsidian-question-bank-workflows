# -*- coding: utf-8 -*-
"""paperkit.paths — 题库索引与卷名/题号规范化。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import re
from pathlib import Path

from qb_bootstrap import require_vault
from qb_schema import as_list, is_question_file, split_frontmatter, stage_roots


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
        rel = path.relative_to(require_vault()).parts
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
