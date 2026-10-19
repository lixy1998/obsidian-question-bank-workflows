# -*- coding: utf-8 -*-
"""gen_problem.py — 批量生成历史单题文件（每道题一个 md）。

用法：
  python gen_problem.py <data.json>         # JSON：数组或单对象
  python gen_problem.py --csv <data.csv>    # CSV：列名与 JSON 字段一致，
                                            # 小节/知识点用分号分隔，题干/解析用引号包裹

JSON 字段（必填）：学段/类别/卷名/序号/册别/单元/课/题型/难度/分值/来源/年份/考区/答案
可选：小节/知识点（列表）、解析、题干、选项（列表，选择题用）
id 自动生成：{来源首标签}-{序号}（来源为 list 取第一项；缺来源时回退卷名）

输出：{vault}/{学段}-{类别}/{卷名}/{卷名}_{题型}/{序号}.md（序号＝原卷面题号，两位补零）
"""
import argparse
import csv
import json
import sys
from pathlib import Path

from qb_bootstrap import bootstrap
from qb_schema import (SchemaError, as_list, build_body, copy_images,
                       dump_yaml, question_dir, question_path,
                       split_frontmatter, validate_lesson, validate_record)

LIST_FIELDS = ["id", "册别", "单元", "课", "小节", "知识点", "核心素养",
               "素养层次水平", "关键能力", "选项", "来源"]
# 教材定位字段（册别/单元/课）支持多值：单值 str 或分号分隔字符串自动转 list，
# 与 小节/知识点 一致，用于跨册别/跨单元/跨课的综合题标注。
# id 亦为多值（v1.3.0 起）：首项 = 主卷 id，其后为「收录本题的其他卷-题号」（一题多卷），
# 单值字符串会自动转为 [单值] 列表，与既有存量数据（单值 str）兼容。


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
    # 序号＝原卷面题号（v1.29.0 起，此前写作「强制两位 01~25」）：
    # 两位补零仅为排序与路径稳定；合卷题号跳跃（文综合 24~35、41~47）直接照抄，不重编、不补空号；
    # ≥100 时 zfill 不截断，按实际位数落盘。规则见 references/fields.md「目录规则」。
    try:
        rec["序号"] = str(int(str(rec["序号"]))).zfill(2)
    except ValueError:
        raise SchemaError(
            f"序号须为原卷面题号（数字）：'{rec['序号']}'——合卷跳号可直接跳跃，但须为数字")
    for f in LIST_FIELDS:
        if f in rec:
            rec[f] = parse_list(rec[f])
    if not rec.get("id"):
        src = rec.get("来源") or []
        first = src[0] if isinstance(src, list) and src else (src or rec["卷名"])
        # 多值（v1.3.0）：首项 = 主卷 id；其后由 dedupe_questions.py 合并时追加别名 id
        rec["id"] = [f"{first}-{rec['序号']}"]
    return rec


def render_md(rec: dict) -> str:
    body = build_body(rec)
    yaml_block = dump_yaml(rec)
    return f"{yaml_block}\n\n{body}\n"


# 重跑 --force 时须原样保留的字段：id（别名 id 列表）／来源（含别名卷名，成对）／解析（一题多卷备注）
PROTECT_MERGED_FIELDS = ("id", "来源", "解析")


def merged_state(path: Path) -> tuple:
    """既有题文件处于「一题多卷」合并态时返回 (fm, ids)，否则 (None, [])。

    判据：id 字段是多值列表（首项为主卷 id，其后为别名卷 id）。
    """
    if not path.exists():
        return None, []
    try:
        fm, _body, err = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None, []
    if err or not fm:
        return None, []
    ids = as_list(fm.get("id"))
    return (fm, ids) if len(ids) > 1 else (None, [])


def keep_merged(rec: dict, old_fm: dict, old_ids: list, path: Path) -> dict:
    """把既有文件的 id／来源／解析 回填进新记录（覆盖合并态前的保护动作）。"""
    for f in PROTECT_MERGED_FIELDS:
        v = old_fm.get(f)
        if v is None or v == "" or v == []:
            continue
        rec[f] = as_list(v) if f in ("id", "来源") else v
    print(f"[合并态保护] {path.name}：保留 {'/'.join(PROTECT_MERGED_FIELDS)}"
          f"（id={old_ids}）；仅刷新其余字段与正文。要整体重建加 --allow-merged")
    return rec


def write_one(rec: dict, dry_run: bool = False, force: bool = False,
              allow_merged: bool = False) -> Path:
    rec = normalize(rec)
    validate_record(rec)
    try:
        validate_lesson(rec)  # 课名与 BOOK_INDEX 比对（警告不阻断）
    except SchemaError as e:
        print(f"[警告] {e}")
    path = question_path(rec)
    if dry_run:
        old_fm, old_ids = merged_state(path)
        tag = "（既有文件处于合并态，将保留 id／来源／解析）" if old_ids else ""
        print(f"[dry-run] {path}{tag}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    old_fm, old_ids = merged_state(path)
    if path.exists() and not force:
        print(f"[跳过] 已存在：{path}"
              f"{'（合并态，含别名 id）' if old_ids else '（--force 可覆盖）'}")
        return path
    if old_ids and not allow_merged:
        rec = keep_merged(rec, old_fm, old_ids, path)
    elif old_ids and allow_merged:
        print(f"[警告] --allow-merged：{path.name} 的合并态将被丢弃"
              f"（{old_ids} → {as_list(rec.get('id'))}），需重跑 dedupe_questions.py --merge")
    existed = path.exists()
    path.write_text(render_md(rec), encoding="utf-8")
    copy_images(rec, path.parent)
    print(f"[{'覆盖' if existed else '生成'}] {path}")
    return path


def main():
    bootstrap()
    ap = argparse.ArgumentParser(description="历史题库单题生成器")
    ap.add_argument("data", help="JSON 文件路径（数组或单对象）")
    ap.add_argument("--csv", action="store_true", help="输入为 CSV")
    ap.add_argument("--dry-run", action="store_true", help="只打印将生成的路径，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的题目文件")
    ap.add_argument("--allow-merged", action="store_true",
                    help="--force 时连同「一题多卷」合并态一并丢弃（默认保留 id/来源/解析）")
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
            write_one(rec, dry_run=args.dry_run, force=args.force,
                      allow_merged=args.allow_merged)
            ok += 1
        except SchemaError as e:
            print(f"[失败] {e}", file=sys.stderr)
            fail += 1
    print(f"\n完成：成功 {ok}，失败 {fail}")


if __name__ == "__main__":
    main()
