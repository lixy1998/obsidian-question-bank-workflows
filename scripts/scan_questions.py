# -*- coding: utf-8 -*-
"""scan_questions.py — 题库健康检查 + 统计。

- 扫描 {vault}/初中-*/高中-* 目录下全部单题 md
- 校验：必填字段、id 唯一性（多值逐项）、受控枚举（册别/题型/难度）、正文非空
- v1.3.0 新增：一题多卷交叉校验（别名 id 不应再作为独立题存在）
- v1.5.0 新增：定位字段提示（课的所属单元/册别是否写进本题 单元/册别 字段，提示级）
- v1.8.2 新增：关键能力**要素名白名单**校验（编号格式合法但要素名非郑林 9 要素 → 问题级；
  徐奉先 3×3 等旧格式仍为提示级）——修补「A2-理解/C3-感悟」类标注逃过校验的盲区
- **考区取值相似检测**（提示级）：找出疑似同义异写/错写的取值对
  （如「洛阳」vs「洛阳市」、「郑州」vs「郑洲」）——考区字段此前只有必填校验、无值域校验，此项补该缺口
- 输出：总量统计 + 按册别/题型/难度/考区分布 + 问题清单

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

from qb_bootstrap import bootstrap, require_vault
from qb_schema import (BOOKS, DIFFICULTIES, REQUIRED_FIELDS, TYPES,
                       TYPE_ALIASES,
                       SchemaError, as_list, is_question_file,
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
# 主词表白名单（唯一权威源：references/素养能力标签体系.md 第三章「郑林三层次九要素」）
# 校验策略分流：编号格式不符（徐奉先旧标注等）→ 提示级；编号合法但要素名不在白名单 → 问题级
ZHENG_SET = {"A1-识记", "A2-说明", "A3-概括", "B1-比较", "B2-分析", "B3-评价",
             "C1-建构", "C2-考证", "C3-探究"}


def _lev_le1(a: str, b: str) -> bool:
    """两串编辑距离是否 ≤1（中文短值场景，够用即可）。相同串返回 False。"""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) < len(b):
        a, b = b, a
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    for i in range(len(a)):                      # 长度差 1：长串删一个字符即短串
        if a[:i] + a[i + 1:] == b:
            return True
    return False


# 已知合法考区取值（省级行政区 + 河南各地市 + 无地域属性的规范值）。
# 用途：区分「合法近邻」与「同义异写」——两个**都**在此集合内且字形相近者是合法近邻
# （河南/海南、湖南/湖北、广东/广西、安阳/南阳…），不得报；只要有一方在集合外，
# 就说明出现了集合外的变体写法（洛阳市、郑洲、洛阳涧西…），才提示人工确认。
KNOWN_AREAS = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
    "台湾", "香港", "澳门",
    "郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作",
    "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店", "济源",
    "全国", "原创",
}


def area_similar_hits(area_counter: Counter):
    """考区取值相似检测（提示级，不做阻断）。

    只报两类高置信情形：
      ① 包含关系：一值是另一值的子串（如「洛阳」⊂「洛阳市」「洛阳涧西」）——加尾缀/区级未归市；
      ② 字形近似：编辑距离 ≤1，且**至少一方不在 KNOWN_AREAS 内**——如「郑州」vs「郑洲」。
    「河南／海南」「湖南／湖北」这类双方都是已知合法考区的近邻**不报**（避免海量假阳性）；
    判据用合法值集合而非频次——整卷写错时错值频次同样很高，按频次过滤会漏。
    返回 [(值A, 次数A, 值B, 次数B, 原因), ...]，按较小频次升序。
    """
    vals = [(str(k).strip(), c) for k, c in area_counter.items() if str(k).strip()]
    hits = []
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            a, ca = vals[i]
            b, cb = vals[j]
            if a in KNOWN_AREAS and b in KNOWN_AREAS:
                continue                      # 双方均为已知合法考区 → 合法近邻
            if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
                hits.append((a, ca, b, cb, "包含关系（疑同义异写）"))
            elif _lev_le1(a, b):
                hits.append((a, ca, b, cb, "字形近似（疑笔误）"))
    hits.sort(key=lambda h: min(h[1], h[3]))
    return hits


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
            rel = p.relative_to(require_vault()).parts
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
    area_counter = Counter()  # 考区取值计数（供相似值检测）
    primary_ids = {}  # 主卷 id -> rel（用于别名交叉校验）
    locator_hits = []  # 定位字段提示：[rel, 缺单元 set, 缺册别 set]（提示级，不计入 problems）
    pair_hits = []     # 多值配对提示：[rel, 无课承接的单元 set, 册别重复项 set]（提示级）
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
            rel = p.relative_to(require_vault())
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
            if fm.get("题型"):
                t = TYPE_ALIASES.get(fm["题型"], fm["题型"])  # 旧值「小论文」归一后校验
                if t not in TYPES:
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
            # 多值定位字段配对提示（v1.23.0）：三项「同序平铺」的机器可校验部分
            #   ① 单元项数 > 课项数 —— 多出的单元没有任何课承接
            #   ② 册别有重复项 —— 同册重复声明
            # 说明：单元列表**允许**多于课（单元卷/跨册综合题常把上级单元一并写入），
            # 也允许单元顺序与课不同序；这两类写法自由**不报**，避免海量假阳性。
            _book_list = [str(x) for x in as_list(fm.get("册别"))]
            _unit_list = [str(x) for x in as_list(fm.get("单元"))]
            _lesson_list = [str(x) for x in as_list(fm.get("课"))]
            pair_u, pair_b = set(), set()
            if len(_unit_list) > len(_lesson_list) and len(_unit_list) > 1:
                seen_u = []
                _unresolved = False
                for _k in _lesson_list:
                    if str(_k).startswith(METHOD_PREFIX):
                        continue          # 学史方法类目不占单元，不计入
                    _locs = lesson2loc.get(str(_k), [])
                    if not _locs:
                        _unresolved = True    # 词表查不到该课 → 判据不可靠，本项不报
                        break
                    for _b, _u in _locs:
                        if _u not in seen_u:
                            seen_u.append(_u)
                spare = [u for u in _unit_list if u not in seen_u]
                # 有单元没有任何课承接（spare 非空）→ 多值三项已无法对齐
                if (not _unresolved) and spare:
                    pair_u.add("、".join(dict.fromkeys(spare)))
            if len(_book_list) != len(set(_book_list)):
                pair_b.add("册别有重复项：" + "、".join(
                    b for b, c in Counter(_book_list).items() if c > 1))
            if pair_u or pair_b:
                pair_hits.append((str(rel), pair_u, pair_b))
            # 关键能力校验（v1.8.2）：
            #   ① 编号格式不符（徐奉先旧格式/无标注）→ 提示级，题级计数
            #   ② 编号合法但要素名不在郑林 9 要素白名单（如 A2-理解、C3-感悟）→ 问题级
            ks = [str(x) for x in as_list(fm.get("关键能力"))]
            if ks and any(not ZHENG_RE.match(x) for x in ks):
                old_ks_count += 1
            for x in ks:
                if ZHENG_RE.match(x) and x not in ZHENG_SET:
                    problems.append(
                        f"[关键能力非主词表 {x}] {rel}（郑林 9 要素见 references/素养能力标签体系.md 第三章）")
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
            for s in as_list(fm.get("考区")):
                stats[f"考区::{s}"] += 1
                area_counter[str(s).strip()] += 1
    # 一题多卷交叉校验（v1.3.0）：别名 id 若仍作为独立题的主卷 id 存在 → 该别名题尚未归档
    for rel, pid, aliases in merged:
        for a in aliases:
            if a in primary_ids:
                problems.append(
                    f"[别名未归档 {a}] {rel}（该 id 仍作为独立题存在于 {primary_ids[a]}，"
                    f"应跑 dedupe_questions.py --merge 归档别名题）")
    stats["一题多卷::多值id题数"] = len(merged)
    area_hits = area_similar_hits(area_counter)
    return total, stats, problems, old_ks_count, merged, locator_hits, pair_hits, area_hits


def main():
    bootstrap()
    ap = argparse.ArgumentParser(description="历史题库健康检查")
    ap.add_argument("--卷", help="只看指定卷")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--dump", action="store_true", help="输出全量题目元数据（JSON 数组）")
    args = ap.parse_args()

    roots = stage_roots()
    if not roots:
        print(f"[失败] 未找到题库目录：{require_vault()} 根下应存在 初中-* / 高中-*", file=sys.stderr)
        sys.exit(1)

    if args.dump:
        dump(roots, args.卷)
        return

    total, stats, problems, old_ks_count, merged, locator_hits, pair_hits, area_hits = scan(roots, args.卷)

    if args.json:
        print(json.dumps({"total": total, "stats": dict(stats),
                          "problems": problems, "old_ks_count": old_ks_count,
                          "locator_hits": [{"文件": r, "缺单元": sorted(u), "缺册别": sorted(b)}
                                           for r, u, b in locator_hits],
                          "pair_hits": [{"文件": r, "无课承接的单元": sorted(u), "册别异常": sorted(b)}
                                        for r, u, b in pair_hits],
                          "area_hits": [{"值A": a, "次数A": ca, "值B": b, "次数B": cb, "原因": reason}
                                        for a, ca, b, cb, reason in area_hits],
                          "merged": [{"文件": str(r), "主卷id": pid, "别名id": al}
                                     for r, pid, al in merged]},
                         ensure_ascii=False, indent=2))
        return

    print(f"═══ 历史题库健康检查 ═══")
    print(f"题库路径：{require_vault()}（{' '.join(r.name for r in roots)}）")
    print(f"题目总数：{total}")
    if merged:
        print(f"一题多卷：{len(merged)} 题含别名 id（多卷收录同一题，已归档别名题）")
    for k in ("册别", "题型", "难度", "考区", "来源", "核心素养", "素养层次水平", "关键能力"):
        items = [(suffix, c) for key, c in stats.items() if key.startswith(f"{k}::")
                 for suffix in [key.split("::", 1)[1]]]
        if items:
            print(f"\n按{k}分布：")
            for name, c in sorted(items, key=lambda x: -x[1]):
                print(f"  {name:<10} {c}")
    if old_ks_count:
        print(f"\nℹ 关键能力旧格式提示：{old_ks_count} 题仍为徐奉先 3×3 旧标注"
              f"（郑林 A1-C3 迁移未完成，未计入问题；迁移映射见 references/素养能力标签体系.md 第三章）")
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
    if pair_hits:
        print(f"\nℹ 多值配对提示：{len(pair_hits)} 题的「册别/单元/课」三项未对齐"
              f"（两处机器可判的硬伤：单元项数多于课、册别有重复项；提示级、未计入问题）")
        for rel, spare_u, dup_b in pair_hits[:8]:
            parts = []
            if dup_b:
                parts.append("；".join(sorted(dup_b)))
            if spare_u:
                parts.append("无课承接的单元 " + "、".join(sorted(spare_u)))
            print(f"  {rel}：{'；'.join(parts)}")
        if len(pair_hits) > 8:
            print(f"  …另有 {len(pair_hits) - 8} 题（--json 可看全量）")
    if area_hits:
        print(f"\nℹ 考区取值相似提示：{len(area_hits)} 对疑似同义异写/错写"
              f"（提示级、未计入问题；考区标注规范见 references/fields.md §考区标注规范）")
        for a, ca, b, cb, reason in area_hits[:10]:
            print(f"  {a}（{ca} 题）↔ {b}（{cb} 题）：{reason}")
        if len(area_hits) > 10:
            print(f"  …另有 {len(area_hits) - 10} 对（--json 可看全量）")
    if problems:
        print(f"\n⚠ 问题 {len(problems)} 项：")
        for p in problems:
            print(f"  {p}")
        sys.exit(1)
    print("\n✓ 无问题")


if __name__ == "__main__":
    main()
