# -*- coding: utf-8 -*-
"""scan_lesson.py — 按「课」取题并统计（流程四·单课考查方向分析专用）

用法：
  python scan_lesson.py --lesson "第5课 古代希腊" [--口径 主考|兼涉|全涉] [--body] [--json]

口径（v1.5.0「定位三字段多值优先」）：
  主考 = 命中「课」**首项**（该课被直接考查，命题落点）
  兼涉 = 命中「课」**第 2 项起**（该课作为背景/参照/结论落点被调用）
  全涉 = 任一命中（默认，与旧版行为一致，用于防漏题）

输出：口径分布 + 各字段统计 + 逐题明细（--body 附题干全文）。
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


def hit_scope(lessons, lesson: str, scope: str) -> bool:
    """判断该题是否命中目标课；返回是否命中（scope=主考/兼涉/全涉）"""
    if not lessons:
        return False
    if scope == "主考":
        return lesson in lessons[0]
    if scope == "兼涉":
        return any(lesson in x for x in lessons[1:])
    return any(lesson in x for x in lessons)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lesson", required=True, help="课名（子串匹配，如「第5课 古代希腊」）")
    ap.add_argument("--口径", dest="scope", default="全涉", choices=["主考", "兼涉", "全涉"])
    ap.add_argument("--body", action="store_true", help="输出题干全文")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    a = ap.parse_args()

    rows = []
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err or not isinstance(fm, dict):
                continue
            lessons = [str(x) for x in as_list(fm.get("课"))]
            if not hit_scope(lessons, a.lesson, a.scope):
                continue
            texts = [ln.strip() for ln in body.splitlines() if ln.strip()]
            rows.append({
                "id": (as_list(fm.get("id")) or [""])[0],
                "文件": str(p),
                "题型": (as_list(fm.get("题型")) or [""])[0],
                "课": lessons,
                "定位": "主考" if lessons and a.lesson in lessons[0] else "兼涉",
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

    if a.json:
        print(json.dumps({"lesson": a.lesson, "scope": a.scope, "total": len(rows),
                          "主考": len(pairs), "兼涉": len(cross), "rows": rows},
                         ensure_ascii=False, indent=2))
        return

    print(f"### {a.lesson}｜口径 {a.scope}｜命中 {len(rows)} 题"
          f"（其中主考 {len(pairs)}、兼涉 {len(cross)}）\n")
    if a.scope == "全涉" and rows:
        print("=== 定位分布（该课在题中的角色）===")
        print(f"  主考（直接考查）  {len(pairs):>3}   "
              f"{'、'.join(r['id'] for r in pairs[:12])}{' …' if len(pairs) > 12 else ''}")
        print(f"  兼涉（作背景/参照）{len(cross):>3}   "
              f"{'、'.join(r['id'] for r in cross[:12])}{' …' if len(cross) > 12 else ''}")
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
