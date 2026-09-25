# -*- coding: utf-8 -*-
"""scan_volume.py — 册别/卷别聚合统计（流程五·考试范围分析专用）

用法：
  python scan_volume.py --册 9上                            # 该册全部题（跨卷累计）
  python scan_volume.py --卷 "洛阳市2025-2026学年9上期末"     # 单卷/卷名子串
  python scan_volume.py --册 9上 --卷 "洛阳"                 # 组合筛选
  python scan_volume.py --册 9上 --json                      # 机器可读

输出四段：
  1. 卷面结构（按卷 × 题型：题量 / 分值）
  2. 难度分布（题量 / 分值）
  3. 课次热度（**主考计满分 / 兼涉单独计数**，不再跨课均摊）+ 三梯队分层
  4. 关键能力 / 核心素养 / 素养层次水平分布 + 非选择题知识点清单
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from qb_schema import (VAULT, as_list, load_lesson_units,      # noqa: E402
                       split_frontmatter, stage_roots)


def collect(book=None, vol=None):
    lesson2book = {}
    for (b, name) in load_lesson_units():
        lesson2book.setdefault(name, b)
    all_rows, rows = [], []
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err or not isinstance(fm, dict):
                continue
            rel = p.relative_to(Path(VAULT)).parts
            volname = rel[1] if len(rel) > 1 else ""
            r = {
                "id": (as_list(fm.get("id")) or [""])[0],
                "卷": volname,
                "卷别": rel[0],
                "册别": [str(x) for x in as_list(fm.get("册别"))],
                "课": [str(x) for x in as_list(fm.get("课"))],
                "题型": str((as_list(fm.get("题型")) or [""])[0]),
                "难度": str((as_list(fm.get("难度")) or [""])[0]),
                "分值": _num((as_list(fm.get("分值")) or [0])[0]),
                "核心素养": [str(x) for x in as_list(fm.get("核心素养"))],
                "素养层次水平": [str(x) for x in as_list(fm.get("素养层次水平"))],
                "关键能力": [str(x) for x in as_list(fm.get("关键能力"))],
                "知识点": [str(x) for x in as_list(fm.get("知识点"))],
                "title": next((ln.strip() for ln in body.splitlines() if ln.strip()), "")[:80],
            }
            all_rows.append(r)
            if book and book not in r["册别"]:
                continue
            if vol and vol not in volname:
                continue
            rows.append(r)
    return rows, all_rows, lesson2book


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--册", help="册别筛选（如 9上；多值时命中即计入）")
    ap.add_argument("--卷", help="卷名子串筛选（如 洛阳市2025-2026学年9上期末）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows, all_rows, lesson2book = collect(a.册, a.卷)
    if not rows:
        print("未命中任何题（检查 --册 / --卷 拼写）")
        return

    # 0) 范围判定：命中卷里属其他册别的题（活动范围超出本册的关键证据）
    scope_notes = []
    if a.册:
        for v in sorted({r["卷"] for r in rows}):
            same = [r for r in all_rows if r["卷"] == v]
            out = [r for r in same if a.册 not in r["册别"]]
            if out:
                scope_notes.append((v, out, len(same)))

    # 1) 卷面结构
    struct = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0]))
    for r in rows:
        cell = struct[r["卷"]][r["题型"]]
        cell[0] += 1
        cell[1] += r["分值"]
    # 2) 难度
    diff = collections.defaultdict(lambda: [0, 0.0])
    for r in rows:
        diff[r["难度"]][0] += 1
        diff[r["难度"]][1] += r["分值"]
    # 3) 课次热度：主考计满分、兼涉单独计数
    heat = collections.defaultdict(lambda: {"主考题量": 0, "主考分值": 0.0, "兼涉题量": 0})
    for r in rows:
        for i, k in enumerate(r["课"]):
            if not k:
                continue
            b = lesson2book.get(k, r["册别"][0] if r["册别"] else "")
            key = f"{b}·{k}" if b else k
            if a.册 and b and b != a.册:
                continue
            if i == 0:
                heat[key]["主考题量"] += 1
                heat[key]["主考分值"] += r["分值"]
            else:
                heat[key]["兼涉题量"] += 1
    # 三梯队（按主考分值降序累计 80% / 95%）
    ranked = sorted(heat.items(), key=lambda kv: (-kv[1]["主考分值"], -kv[1]["兼涉题量"]))
    total_score = sum(v["主考分值"] for _k, v in ranked) or 1.0
    acc, tier = 0.0, {}
    for k, v in ranked:
        acc += v["主考分值"]
        tier[k] = "第一梯队" if acc <= total_score * 0.8 else ("第二梯队" if acc <= total_score * 0.95 else "第三梯队")
    # 4) 素养/能力
    def dist(field):
        c = collections.Counter()
        for r in rows:
            for x in r[field]:
                c[x] += 1
        return c
    ks = collections.Counter()
    for r in rows:
        for x in r["关键能力"]:
            ks[x[:2]] += 1

    if a.json:
        print(json.dumps({
            "筛选": {"册": a.册, "卷": a.卷}, "题量": len(rows),
            "总分值": round(sum(r["分值"] for r in rows), 1),
            "卷面结构": {k: {t: v for t, v in d.items()} for k, d in struct.items()},
            "难度": {k: v for k, v in diff.items()},
            "热度": [{"课": k, "梯队": tier[k], **v} for k, v in ranked],
            "关键能力": dict(ks), "核心素养": dict(dist("核心素养")),
            "素养层次水平": dict(dist("素养层次水平")),
            "学科能力明细": dict(dist("关键能力")),
        }, ensure_ascii=False, indent=2))
        return

    title = f"{a.册 or '全部册别'}" + (f"｜卷含「{a.卷}」" if a.卷 else "")
    print(f"═══ 范围统计：{title} ═══")
    print(f"题量 {len(rows)}｜分值合计 {sum(r['分值'] for r in rows):.0f}｜卷数 {len(struct)}\n")

    if scope_notes:
        print("【0】范围判定：同卷中属其他册别的题（关键——考试范围常超出本册）")
        for v, out, tot in scope_notes:
            bu = collections.Counter(b for r in out for b in r["册别"])
            sc = sum(r["分值"] for r in out)
            print(f"  {v}：全卷 {tot} 题，其中 **{len(out)} 题**属 "
                  f"{'、'.join(f'{b}×{n}' for b, n in bu.items())}（{sc:.0f} 分）")
            print(f"      {'、'.join(r['id'] for r in out[:12])}")
        print()

    print("【1】卷面结构（卷 × 题型：题量/分值）")
    for v in sorted(struct):
        parts = "｜".join(f"{t} {n}题/{s:.0f}分" for t, (n, s) in sorted(struct[v].items()))
        print(f"  {v}\n      {parts}")
    print()

    print("【2】难度分布")
    for k in ("易", "中", "难"):
        n, s = diff.get(k, [0, 0.0])
        pct = 100 * s / (sum(r['分值'] for r in rows) or 1)
        print(f"  {k}  {n:>3} 题  {s:>5.0f} 分（{pct:.0f}%）")
    print()

    print("【3】课次热度（主考计满分 / 兼涉单独计数）")
    print(f"  {'梯队':<6}{'课':<52}{'主考':>10}{'兼涉':>8}")
    for k, v in ranked:
        print(f"  {tier[k]:<6}{k[:50]:<52}{v['主考题量']:>3}题/{v['主考分值']:>4.0f}分{v['兼涉题量']:>7}题")
    zero = [k for (b, k) in load_lesson_units()
            if (not a.册 or b == a.册) and k not in {x.split("·", 1)[1] if "·" in x else x for x in heat}]
    print(f"\n  未命中课 {len(zero)} 门（样本未涉及，≠ 不重要；注意教材版本差异与样本量）")
    if zero:
        print(f"      {'、'.join(k.replace('第', '').replace('课 ', '') for k in zero[:14])}"
              f"{' …' if len(zero) > 14 else ''}")
    print()

    print("【4】能力与素养分布")
    print("  关键能力（按层次）：", dict(sorted(ks.items())))
    print("  关键能力明细：")
    for k, v in dist("关键能力").most_common():
        print(f"      {v:>3}  {k}")
    print("  核心素养 TOP10：")
    for k, v in dist("核心素养").most_common(10):
        print(f"      {v:>3}  {k}")
    print("  素养层次水平 TOP8：")
    for k, v in dist("素养层次水平").most_common(8):
        print(f"      {v:>3}  {k}")
    print()

    print("【5】非选择题知识点清单（归纳主线用）")
    for r in rows:
        if r["题型"] in ("材料解析题", "论述题", "综合探究题"):
            print(f"  [{r['id']}] {'、'.join(r['知识点'])}\n      {r['title']}")


if __name__ == "__main__":
    main()
