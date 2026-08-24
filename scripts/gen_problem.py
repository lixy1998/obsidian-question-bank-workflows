# -*- coding: utf-8 -*-
"""gen_problem.py — 批量生成历史单题文件（每道题一个 md）。

用法：
  python gen_problem.py <data.json>         # JSON：数组或单对象
  python gen_problem.py --csv <data.csv>    # CSV：列名与 JSON 字段一致，
                                            # 小节/知识点用分号分隔，题干/解析用引号包裹

JSON 字段（必填）：学段/类别/卷名/序号/册别/单元/课/题型/难度/分值/来源/年份/考区/答案
可选：小节/知识点（列表）、解析、题干、选项（列表，选择题用）
id 自动生成：{来源首标签}-{序号}（来源为 list 取第一项；缺来源时回退卷名）

输出：{vault}/{学段}-{类别}/{卷名}/{卷名}_{题型}/{序号}.md
"""
import argparse
import csv
import json
import sys
from pathlib import Path

from qb_schema import (SchemaError, build_body, copy_images, dump_yaml,
                       question_dir, question_path, validate_lesson,
                       validate_record)

LIST_FIELDS = ["册别", "单元", "课", "小节", "知识点", "核心素养",
               "素养层次水平", "关键能力", "选项", "来源"]
# 教材定位字段（册别/单元/课）支持多值：单值 str 或分号分隔字符串自动转 list，
# 与 小节/知识点 一致，用于跨册别/跨单元/跨课的综合题标注。


def parse_list(value):
    if value is None:
        return None
    if isinstance(value, list):
        return value
    return [x.strip() for x in str(value).split(";") if x.strip()]


def normalize(rec: dict) -> dict:
    rec = {k: v for k, v in rec.items() if v is not None and v != ""}
    # 路径字段预检：缺任一项无法定位文件，提前报 SchemaError（避免 KeyError 中断整批）
    for f in ("学段", "类别", "卷名", "序号"):
        if f not in rec or not str(rec[f]).strip():
            raise SchemaError(f"缺少路径字段 [{f}]，无法定位题目文件")
    # 序号强制两位（01~25），与 fields.md 目录约定一致
    try:
        rec["序号"] = str(int(str(rec["序号"]))).zfill(2)
    except ValueError:
        raise SchemaError(f"序号必须为数字：'{rec['序号']}'")
    for f in LIST_FIELDS:
        if f in rec:
            rec[f] = parse_list(rec[f])
    if not rec.get("id"):
        src = rec.get("来源") or []
        first = src[0] if isinstance(src, list) and src else (src or rec["卷名"])
        rec["id"] = f"{first}-{rec['序号']}"
    return rec


def render_md(rec: dict) -> str:
    body = build_body(rec)
    yaml_block = dump_yaml(rec)
    return f"{yaml_block}\n\n{body}\n"


def write_one(rec: dict, dry_run: bool = False, force: bool = False) -> Path:
    rec = normalize(rec)
    validate_record(rec)
    try:
        validate_lesson(rec)  # 课名与 BOOK_INDEX 比对（警告不阻断）
    except SchemaError as e:
        print(f"[警告] {e}")
    path = question_path(rec)
    if dry_run:
        print(f"[dry-run] {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        print(f"[跳过] 已存在：{path}（--force 可覆盖）")
        return path
    path.write_text(render_md(rec), encoding="utf-8")
    copy_images(rec, path.parent)
    print(f"[{'覆盖' if path.exists() and force else '生成'}] {path}")
    return path


def main():
    ap = argparse.ArgumentParser(description="历史题库单题生成器")
    ap.add_argument("data", help="JSON 文件路径（数组或单对象）")
    ap.add_argument("--csv", action="store_true", help="输入为 CSV")
    ap.add_argument("--dry-run", action="store_true", help="只打印将生成的路径，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的题目文件")
    args = ap.parse_args()

    if args.csv:
        with open(args.data, encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))
    else:
        raw = json.loads(Path(args.data).read_text(encoding="utf-8"))
        records = raw if isinstance(raw, list) else [raw]

    ok = fail = 0
    for rec in records:
        try:
            write_one(rec, dry_run=args.dry_run, force=args.force)
            ok += 1
        except SchemaError as e:
            print(f"[失败] {e}", file=sys.stderr)
            fail += 1
    print(f"\n完成：成功 {ok}，失败 {fail}")


if __name__ == "__main__":
    main()
