# -*- coding: utf-8 -*-
"""scan_missed.py — 关键词反查：正文含关键词、但「课」字段未标该课（漏标/跨课判断）

流程四·单课考查方向分析的配套排查工具（独立于 scan 的定位字段提示：
本工具查的是**正文内容**，定位字段提示查的是**单元/册别漏写**）。

用法：
  python scan_missed.py "第5课 古代埃及" "金字塔|法老|尼罗河|象形文字" [--口径 主考|兼涉|全涉]

参数：
  目标课名（子串匹配）  关键词（用 | 分隔，命中任一即列出）
  --口径 指定「已标为该课」的判定口径（默认全涉）；用「主考」可专查
         「只把它当背景标过、正文却在讲它」的题
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from qb_bootstrap import bootstrap
from qb_schema import as_list, split_frontmatter, stage_roots  # noqa: E402


def main():
    bootstrap()
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="目标课名（子串匹配）")
    ap.add_argument("keywords", help="关键词，用 | 分隔")
    ap.add_argument("--口径", dest="scope", default="全涉", choices=["主考", "兼涉", "全涉"])
    a = ap.parse_args()
    kws = [k for k in a.keywords.split("|") if k]

    hits = 0
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err or not isinstance(fm, dict):
                continue
            lessons = [str(x) for x in as_list(fm.get("课"))]
            if a.scope == "主考":
                marked = bool(lessons) and a.target in lessons[0]
            elif a.scope == "兼涉":
                marked = any(a.target in x for x in lessons[1:])
            else:
                marked = any(a.target in x for x in lessons)
            if marked:
                continue
            if not any(k in body for k in kws):
                continue
            hits += 1
            first = next((ln.strip()[:130] for ln in body.splitlines() if ln.strip()), "")
            print(f"[{(as_list(fm.get('id')) or [''])[0]}] 课={lessons} "
                  f"题型={(as_list(fm.get('题型')) or [''])[0]} "
                  f"年份={(as_list(fm.get('年份')) or [''])[0]} "
                  f"分值={(as_list(fm.get('分值')) or [''])[0]}")
            print(f"    {first}")
    print(f"\n共 {hits} 题正文命中关键词但未标为「{a.target}」（口径 {a.scope}）"
          f"——逐题判断属跨课综合还是漏标")


if __name__ == "__main__":
    main()
