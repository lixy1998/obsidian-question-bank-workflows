# -*- coding: utf-8 -*-
"""scan_questions.py — 题库健康检查 + 统计。

- 扫描 {vault}/初中-*/高中-* 目录下全部单题 md
- 校验：必填字段、id 唯一性（多值逐项）、受控枚举（册别/题型/难度）、正文非空
- v1.3.0 新增：一题多卷交叉校验（别名 id 不应再作为独立题存在）
- v1.5.0 新增：定位字段提示（课的所属单元/册别是否写进本题 单元/册别 字段，提示级）
- v1.8.2 新增：关键能力**要素名白名单**校验（编号格式合法但要素名非郑林 9 要素 → 问题级；
  徐奉先 3×3 等旧格式仍为提示级）——修补「A2-理解/C3-感悟」类标注逃过校验的盲区
- 输出：总量统计 + 按册别/题型/难度分布 + 问题清单

用法：
  python scan_questions.py
  python scan_questions.py --卷 2026河南中考     # 只看某卷
  python scan_questions.py --json                # 输出机器可读统计
  python scan_questions.py --dump                # 输出全量题目元数据（JSON 数组，供命令行检索过滤）
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from qb_schema import (BOOKS, DIFFICULTIES, REQUIRED_FIELDS, TYPES,
                       VAULT, SchemaError, as_list, is_question_file,
                       split_frontmatter, stage_roots, validate_lesson,
                       load_lesson_units, METHOD_PREFIX)

try:
    import yaml
except ImportError:
    yaml = None


def _empty(v) -> bool:
    """字段为空判断：None / 空串 / 空 list。"""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, list):
        return not v
    return False


# 郑林 9 要素编号格式（A1-C3）：关键能力主词表；徐奉先 3×3 为旧标注（存量迁移未完成，仅提示）
ZHENG_RE = re.compile(r"^[ABC][1-3]-")
# 主词表白名单（唯一权威源：references/素养能力词表.md 第三章「郑林三层次九要素」）
# 校验策略分流：编号格式不符（徐奉先旧标注等）→ 提示级；编号合法但要素名不在白名单 → 问题级
ZHENG_SET = {"A1-识记", "A2-说明", "A3-概括", "B1-比较", "B2-分析", "B3-评价",
             "C1-建构", "C2-考证", "C3-探究"}


def dump(roots, only_vol: str = None):
    """输出全量题目元数据（JSON 数组），供命令行检索/过滤（对应 SKILL.md 流程二）。

    id 多值（一题多卷）时分列：`id` 原样、`题号`=首项（主卷 id）、`别名`=其余收录卷 id。
    """
    out = []
    for root in roots:
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            if only_vol and only_vol not in p.parts:
                continue
            fm, _, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            rel = p.relative_to(Path(VAULT)).parts
            ids = as_list(fm.get("id"))
            out.append({
                "id": fm.get("id"),
                "题号": ids[0] if ids else None,
                "别名": ids[1:],
                "册别": fm.get("册别"), "课": fm.get("课"),
                "题型": fm.get("题型"), "难度": fm.get("难度"),
                "来源": fm.get("来源"), "年份": fm.get("年份"),
                "卷名": rel[1] if len(rel) > 1 else None,
                "文件": str(p),
            })
    print(json.dumps(out, ensure_ascii=False, indent=2))


def scan(roots, only_vol: str = None):
    problems, stats = [], Counter()
    seen_ids, total = {}, 0
    old_ks_count = 0  # 关键能力仍为徐奉先旧格式的题数（提示级，不计入 problems）
    merged = []       # [(rel, 主卷 id, [别名 id, ...]), ...] 一题多卷题
    primary_ids = {}  # 主卷 id -> rel（用于别名交叉校验）
    locator_hits = []  # 定位字段提示：[rel, 缺单元 set, 缺册别 set]（提示级，不计入 problems）
    # 课名 -> [(册别, 单元全称), ...]（多值录入兜底：课的所属单元/册别应写进本题对应字段）
    lesson2loc = {}
    for (b, name), u in load_lesson_units().items():
        lesson2loc.setdefault(name, []).append((b, u))
    for root in roots:
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            if only_vol and only_vol not in p.parts:
                continue  # --卷 过滤：只保留路径中含该卷名的文件
            total += 1
            rel = p.relative_to(Path(VAULT))
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err:
                problems.append(f"[{err}] {rel}")
            # 必填字段
            for f in REQUIRED_FIELDS:
                if _empty(fm.get(f)):
                    problems.append(f"[缺字段 {f}] {rel}")
            # id 唯一（多值逐项；id 列表内部重复也算问题）
            ids, inner_dup = [], set()
            for qid in as_list(fm.get("id")):
                if qid in ids:
                    inner_dup.add(qid)
                ids.append(qid)
                if qid in seen_ids:
                    problems.append(f"[id重复 {qid}] {rel}（先见于 {seen_ids[qid]}）")
                else:
                    seen_ids[qid] = rel
            for qid in inner_dup:
                problems.append(f"[id列表内重复 {qid}] {rel}")
            if len(ids) > 1:
                merged.append((rel, ids[0], ids[1:]))
            if ids:
                primary_ids.setdefault(ids[0], rel)
            # 枚举（册别支持多值，逐项校验）
            for b in as_list(fm.get("册别")):
                if b not in BOOKS:
                    problems.append(f"[册别非法 {b}] {rel}")
            if fm.get("题型") and fm["题型"] not in TYPES:
                problems.append(f"[题型非法 {fm['题型']}] {rel}")
            if fm.get("难度") and fm["难度"] not in DIFFICULTIES:
                problems.append(f"[难度非法 {fm['难度']}] {rel}")
            # 课名 vs BOOK_INDEX（按册别映射版本节词表）
            try:
                validate_lesson(fm)
            except SchemaError as e:
                problems.append(f"[课名词表] {e}（{rel}）")
            # 定位字段提示（v1.5.0）：课的所属单元/册别是否写进本题的 单元/册别 字段
            # （多值录入兜底；提示级，不计入 problems）
            _books, _units = set(as_list(fm.get("册别"))), set(as_list(fm.get("单元")))
            miss_u, miss_b = set(), set()
            for _k in as_list(fm.get("课")):
                if str(_k).startswith(METHOD_PREFIX):
                    continue
                cands = lesson2loc.get(str(_k))
                if not cands:
                    continue  # 词表未收录（如高中自定义/新增课）→ 跳过，不误报
                pick = next(((b, u) for b, u in cands if b in _books), cands[0])
                if pick[1] not in _units:
                    miss_u.add(pick[1])
                if pick[0] not in _books:
                    miss_b.add(pick[0])
            if miss_u or miss_b:
                locator_hits.append((str(rel), miss_u, miss_b))
            # 关键能力校验（v1.8.2）：
            #   ① 编号格式不符（徐奉先旧格式/无标注）→ 提示级，题级计数
            #   ② 编号合法但要素名不在郑林 9 要素白名单（如 A2-理解、C3-感悟）→ 问题级
            ks = [str(x) for x in as_list(fm.get("关键能力"))]
            if ks and any(not ZHENG_RE.match(x) for x in ks):
                old_ks_count += 1
            for x in ks:
                if ZHENG_RE.match(x) and x not in ZHENG_SET:
                    problems.append(
                        f"[关键能力非主词表 {x}] {rel}（郑林 9 要素见 references/素养能力词表.md 第三章）")
            # 正文非空
            if not body.strip():
                problems.append(f"[正文为空] {rel}")
            # 统计（册别/来源/核心素养/关键能力均支持多值标签）
            stats["总数"] += 1
            for b in as_list(fm.get("册别")):
                stats[f"册别::{b}"] += 1
            stats[f"题型::{fm.get('题型')}"] += 1
            stats[f"难度::{fm.get('难度')}"] += 1
            for s in as_list(fm.get("来源")):
                stats[f"来源::{s}"] += 1
            for s in as_list(fm.get("核心素养")):
                stats[f"核心素养::{s}"] += 1
            for s in as_list(fm.get("素养层次水平")):
                stats[f"素养层次水平::{s}"] += 1
            for s in as_list(fm.get("关键能力")):
                stats[f"关键能力::{s}"] += 1
    # 一题多卷交叉校验（v1.3.0）：别名 id 若仍作为独立题的主卷 id 存在 → 该别名题尚未归档
    for rel, pid, aliases in merged:
        for a in aliases:
            if a in primary_ids:
                problems.append(
                    f"[别名未归档 {a}] {rel}（该 id 仍作为独立题存在于 {primary_ids[a]}，"
                    f"应跑 dedupe_questions.py --merge 归档别名题）")
    stats["一题多卷::多值id题数"] = len(merged)
    return total, stats, problems, old_ks_count, merged, locator_hits


def main():
    ap = argparse.ArgumentParser(description="历史题库健康检查")
    ap.add_argument("--卷", help="只看指定卷")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--dump", action="store_true", help="输出全量题目元数据（JSON 数组）")
    args = ap.parse_args()

    roots = stage_roots()
    if not roots:
        print(f"[失败] 未找到题库目录：{VAULT} 根下应存在 初中-* / 高中-*", file=sys.stderr)
        sys.exit(1)

    if args.dump:
        dump(roots, args.卷)
        return

    total, stats, problems, old_ks_count, merged, locator_hits = scan(roots, args.卷)

    if args.json:
        print(json.dumps({"total": total, "stats": dict(stats),
                          "problems": problems, "old_ks_count": old_ks_count,
                          "locator_hits": [{"文件": r, "缺单元": sorted(u), "缺册别": sorted(b)}
                                           for r, u, b in locator_hits],
                          "merged": [{"文件": str(r), "主卷id": pid, "别名id": al}
                                     for r, pid, al in merged]},
                         ensure_ascii=False, indent=2))
        return

    print(f"═══ 历史题库健康检查 ═══")
    print(f"题库路径：{VAULT}（{' '.join(r.name for r in roots)}）")
    print(f"题目总数：{total}")
    if merged:
        print(f"一题多卷：{len(merged)} 题含别名 id（多卷收录同一题，已归档别名题）")
    for k in ("册别", "题型", "难度", "来源", "核心素养", "素养层次水平", "关键能力"):
        items = [(suffix, c) for key, c in stats.items() if key.startswith(f"{k}::")
                 for suffix in [key.split("::", 1)[1]]]
        if items:
            print(f"\n按{k}分布：")
            for name, c in sorted(items, key=lambda x: -x[1]):
                print(f"  {name:<10} {c}")
    if old_ks_count:
        print(f"\nℹ 关键能力旧格式提示：{old_ks_count} 题仍为徐奉先 3×3 旧标注"
              f"（郑林 A1-C3 迁移未完成，未计入问题；迁移映射见 references/素养能力词表.md 第三章）")
    if locator_hits:
        print(f"\nℹ 定位字段提示：{len(locator_hits)} 题的「课的所属单元/册别」未出现在本题 单元/册别 字段"
              f"（多值录入兜底，提示级、未计入问题）")
        for rel, miss_u, miss_b in locator_hits[:8]:
            parts = []
            if miss_b:
                parts.append("缺册别 " + "、".join(sorted(miss_b)))
            if miss_u:
                parts.append("缺单元 " + "、".join(sorted(miss_u)))
            print(f"  {rel}：{'；'.join(parts)}")
        if len(locator_hits) > 8:
            print(f"  …另有 {len(locator_hits) - 8} 题（--json 可看全量）")
    if problems:
        print(f"\n⚠ 问题 {len(problems)} 项：")
        for p in problems:
            print(f"  {p}")
    else:
        print("\n✓ 无问题")


if __name__ == "__main__":
    main()
