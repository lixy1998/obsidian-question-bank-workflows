# -*- coding: utf-8 -*-
"""scan_lesson.py — 按「课 / 单元」取题并统计（流程四·多维度考查方向分析专用）

用法：
  python scan_lesson.py --lesson "第5课 古代希腊" [--口径 主考|兼涉|全涉] [--body] [--json]
  python scan_lesson.py --lesson "第2课 古代两河流域,第3课 古代埃及,纲要下第1课 …"  # 逗号分隔多课＝专题聚合
  python scan_lesson.py --单元 "古代欧洲文明" [...]                              # 单元维度（按「单元」字段匹配）
  python scan_lesson.py --单元 "史前时期" --卷 "洛阳市2024-2025学年7上期末,洛阳市2025-2026学年7上期末"  # 限定卷范围（v2.1）

参数：
  --lesson  课名，子串匹配；逗号分隔＝多课聚合（专题维度）
  --单元    单元名，子串匹配「单元」字段；逗号分隔多单元
  --卷      卷名筛选（v2.1）：子串匹配卷目录名（{vault}/{类别}/{卷名}/ 第 2 层），
            逗号分隔多卷，命中任一即保留；范围收窄器，不改变口径逻辑
  两者至少给一个，可同给（并集口径）

口径（v1.5.0「定位三字段多值优先」，v2.0 扩展到单元/多课组）：
  主考 = 目标任一项命中字段**首项**（命题落点）
  兼涉 = 目标任一项命中字段**第 2 项起**（背景/参照/结论落点）
  全涉 = 任一位置命中（默认，与旧版行为一致，用于防漏题）

输出：口径分布 + 定位分布 + 课分布/册别分布（多课/单元模式自动加）+ 各字段统计 + 逐题明细。
单课模式行为与 v1.x 完全兼容。
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from qb_schema import VAULT, as_list, split_frontmatter, stage_roots  # noqa: E402

STAT_FIELDS = ["题型", "小节", "知识点", "核心素养", "素养层次水平", "关键能力",
               "难度", "年份", "考区", "来源"]


def hit_any(values, targets, scope: str) -> bool:
    """字段值列表是否命中目标组中任一项；scope=主考/兼涉/全涉"""
    if not values or not targets:
        return False
    if scope == "主考":
        return any(t in values[0] for t in targets)
    if scope == "兼涉":
        return any(t in x for x in values[1:] for t in targets)
    return any(any(t in x for t in targets) for x in values)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lesson", default="",
                    help="课名（子串匹配，逗号分隔多课＝专题聚合，如「第2课 古代两河流域,第3课 古代埃及」）")
    ap.add_argument("--单元", dest="units", default="",
                    help="单元名（子串匹配「单元」字段，逗号分隔多单元）")
    ap.add_argument("--卷", dest="volumes", default="",
                    help="卷名筛选（子串匹配卷目录名，逗号分隔多卷，如「洛阳市2024-2025学年7上期末,洛阳市2025-2026学年7上期末」）")
    ap.add_argument("--口径", dest="scope", default="全涉", choices=["主考", "兼涉", "全涉"])
    ap.add_argument("--body", action="store_true", help="输出题干全文")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    a = ap.parse_args()

    targets_lesson = [s.strip() for s in a.lesson.split(",") if s.strip()]
    targets_unit = [s.strip() for s in a.units.split(",") if s.strip()]
    targets_vol = [s.strip() for s in a.volumes.split(",") if s.strip()]
    if not targets_lesson and not targets_unit:
        ap.error("至少给一个 --lesson 或 --单元")
    multi = (len(targets_lesson) + len(targets_unit)) > 1
    if targets_lesson and targets_unit:
        mode = "课+单元混合"
    elif targets_unit:
        mode = "单元"
    else:
        mode = "单课" if len(targets_lesson) == 1 else "多课聚合"

    rows = []
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            if targets_vol:  # 卷范围收窄（v2.1）：卷名 = 路径第 2 层目录名
                parts = p.relative_to(Path(VAULT)).parts
                volname = parts[1] if len(parts) > 1 else ""
                if not any(v in volname for v in targets_vol):
                    continue
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err or not isinstance(fm, dict):
                continue
            lessons = [str(x) for x in as_list(fm.get("课"))]
            units = [str(x) for x in as_list(fm.get("单元"))]
            hit_l = hit_any(lessons, targets_lesson, a.scope)
            hit_u = hit_any(units, targets_unit, a.scope)
            if not (hit_l or hit_u):
                continue
            is_main = hit_any(lessons, targets_lesson, "主考") or hit_any(units, targets_unit, "主考")
            texts = [ln.strip() for ln in body.splitlines() if ln.strip()]
            rows.append({
                "id": (as_list(fm.get("id")) or [""])[0],
                "文件": str(p),
                "题型": (as_list(fm.get("题型")) or [""])[0],
                "册别": [str(x) for x in as_list(fm.get("册别"))],
                "单元": units,
                "课": lessons,
                "定位": "主考" if is_main else "兼涉",
                "小节": as_list(fm.get("小节")),
                "知识点": as_list(fm.get("知识点")),
                "核心素养": as_list(fm.get("核心素养")),
                "素养层次水平": as_list(fm.get("素养层次水平")),
                "关键能力": as_list(fm.get("关键能力")),
                "难度": (as_list(fm.get("难度")) or [""])[0],
                "分值": (as_list(fm.get("分值")) or [""])[0],
                "年份": (as_list(fm.get("年份")) or [""])[0],
                "考区": (as_list(fm.get("考区")) or [""])[0],
                "来源": as_list(fm.get("来源")),
                "答案": str((as_list(fm.get("答案")) or [""])[0])[:120],
                "解析": str((as_list(fm.get("解析")) or [""])[0])[:200],
                "题干": "\n".join(texts)[:900],
                "首行": texts[0][:200] if texts else "",
            })

    rows.sort(key=lambda r: (str(r["年份"]), r["id"]))
    pairs = [r for r in rows if r["定位"] == "主考"]
    cross = [r for r in rows if r["定位"] == "兼涉"]

    all_targets = targets_lesson + targets_unit
    mode_note = "（" + "；".join(
        ([f"课×{len(targets_lesson)}"] if targets_lesson else []) +
        ([f"单元×{len(targets_unit)}"] if targets_unit else [])) + "）" if multi else ""
    if a.json:
        print(json.dumps({"lesson": a.lesson, "units": a.units, "volumes": a.volumes, "mode": mode,
                          "targets": all_targets, "scope": a.scope, "total": len(rows),
                          "主考": len(pairs), "兼涉": len(cross), "rows": rows},
                         ensure_ascii=False, indent=2))
        return

    target_desc = "＋".join(all_targets) if not multi else f"{mode}{mode_note}"
    vol_note = f"｜卷×{len(targets_vol)}" if targets_vol else ""
    print(f"### {target_desc}｜模式 {mode}｜口径 {a.scope}{vol_note}｜命中 {len(rows)} 题"
          f"（其中主考 {len(pairs)}、兼涉 {len(cross)}）\n")

    if a.scope == "全涉" and rows:
        print("=== 定位分布（目标在题中的角色）===")
        print(f"  主考（直接考查）  {len(pairs):>3}   "
              f"{'、'.join(r['id'] for r in pairs[:12])}{' …' if len(pairs) > 12 else ''}")
        print(f"  兼涉（作背景/参照）{len(cross):>3}   "
              f"{'、'.join(r['id'] for r in cross[:12])}{' …' if len(cross) > 12 else ''}")
        print()

    # —— 课分布 / 册别分布（多课、单元或混合模式自动输出，供专题/单元维度归纳用）——
    if multi or mode == "单元":
        main_l = collections.Counter()
        cross_l = collections.Counter()
        for r in rows:
            vals = r["课"] if targets_lesson else r["单元"]
            for i, x in enumerate(vals):
                if any(t in x for t in all_targets):
                    (main_l if i == 0 else cross_l)[x] += 1
        print("=== 命中分布（首项=命题落点）===")
        for label, c in (("主考落点", main_l), ("兼涉调用", cross_l)):
            if c:
                print(f"  【{label}】")
                for k, v in c.most_common(30):
                    print(f"    {v:>3}  {k}")
        vol = collections.Counter(r["册别"][0] if r["册别"] else "（未标）" for r in rows)
        print(f"  【册别分布】" + "  ".join(f"{k}×{v}" for k, v in vol.most_common()))
        print()

    def stat(field):
        c = collections.Counter()
        for r in rows:
            v = r[field]
            for x in (v if isinstance(v, list) else [v]):
                if x:
                    c[x] += 1
        return c

    for f in STAT_FIELDS:
        c = stat(f)
        if not c:
            continue
        print(f"=== {f} ===")
        for k, v in c.most_common(30):
            print(f"  {v:>3}  {k}")
        print()

    print("=== 明细 ===")
    for r in rows:
        print(f"[{r['id']}] {r['题型']}｜{r['定位']}｜{r['年份']} {r['考区']}｜"
              f"难度{r['难度']}｜{r['分值']}分｜课:{'；'.join(r['课'])}")
        print(f"    小节: {'、'.join(r['小节'])} | 知识点: {'、'.join(r['知识点'])}")
        print(f"    素养: {'；'.join(r['核心素养'])}")
        print(f"    层次: {'；'.join(r['素养层次水平'])}")
        print(f"    能力: {','.join(r['关键能力'])}")
        print(f"    答案: {r['答案']}")
        print(f"    解析: {r['解析']}")
        print(f"    题干: {r['首行']}")
        if a.body:
            print("    ----全文----")
            for line in r["题干"].splitlines():
                print("    " + line)
        print()


if __name__ == "__main__":
    main()
