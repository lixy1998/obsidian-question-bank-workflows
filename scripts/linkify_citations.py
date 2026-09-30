#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告补链 —— 把研究笔记正文里的「出处表述」批量转成库内 wikilink。

场景：流程三/四产出的研究报告，正文大量出现「2024 年河南中考 第 25 题」这类
出处表述。这些是纯文本，读者无法点击查看原题。本脚本把它们原地转成
`[[类别/卷名/卷名_题型/序号|卷名 第 N 题]]`，正文与表格内一并处理。

用法：
  python linkify_citations.py                     # 扫描 3-试题研究/ 全部笔记（只读）
  python linkify_citations.py --files a.md b.md   # 指定笔记
  python linkify_citations.py --apply             # 执行（写前请先备份）

只处理**可完整识别**的出处（含年份或完整学年 + 卷别 + 第N题）。缺卷别的指代
（如「例如 2026 年第 24 题」「同卷第 9 题」）**不猜测**，单独列出交人工。

关键实现要点（踩坑记录，改脚本前必读）
--------------------------------------
1. **文件名补零**：库内题目文件名补零（`06.md`、`01.md`），报告写「第 6 题」——
   `find_question` 必须同时尝试 `6.md` 与 `06.md`，否则大面积误报「卷内无此号」。
2. **A/B 卷硬判据**：`2016 河南中考` 会同时命中 `2016河南中考` 与 `2016河南中考B卷`，
   必须按「表述与目录的 B卷 属性一致」淘汰。
3. **区级硬判据**：`2025 年洛阳市一模` 会撞上 `2025洛阳涧西区一模`——
   表述无区名则排除区级卷目录（DISTRICTS 词表）。
4. **表格单元格内管道符必须转义** `\\|`，否则链接被表格当列分隔符吞掉。
5. **只替换「卷别 第N题」这一段**，前缀（`**【真题 1】`、`> `、`| `）留在链接外，
   否则会破坏 `**…**` 加粗配对。
6. 替换按**行号倒序**执行，避免位置漂移。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qb_schema  # noqa: E402
from qb_bootstrap import bootstrap, require_vault  # noqa: E402


def vault_dir() -> Path:
    """vault 根目录（惰性求值：模块导入期不要求已配置，未配置时 `--help` 仍可用）。"""
    return require_vault()


def note_dir() -> Path:
    """报告笔记目录：{vault}/3-试题研究"""
    return vault_dir() / "3-试题研究"


ROOTS = ["初中-1中考真题", "初中-2中考模拟题", "初中-3阶段测试",
         "高中-1高考真题", "高中-2高考模拟题", "高中-3阶段测试"]


# ---- 出处表述正则（严格式：必须含年份或完整学年）----
VOL_PAT = (
    r"(?:"
    # ① 2024 年河南中考 / 2024 河南中考B卷 / 2026 年广东中考
    r"(?:19|20)\d{2}\s*年?\s*[\u4e00-\u9fa5A-Za-z]{2,10}?(?:中考|高考)(?:B卷|A卷|卷)?"
    # ② 2024-2025 学年洛阳市9上期末 / 2025-2026 学年洛阳市9上期末
    r"|(?:19|20)\d{2}\s*[-—~]\s*\d{4}\s*学年\s*[\u4e00-\u9fa5]{2,8}\d?[上下]?(?:期末|期中|一模|二模|月考)"
    # ③ 洛阳市 2023-2024 学年期末（地名前置）
    r"|[\u4e00-\u9fa5]{2,8}\s*(?:19|20)\d{2}\s*[-—~]\s*\d{4}\s*学年\s*(?:\d[上下])?(?:期末|期中|一模|二模)"
    # ④ 2025 年洛阳市一模 / 2026涧西区一模 / 2026洛阳洛龙区一模（「年」可省，
    #    ⚠ 2026-09-21 实测：`2026涧西区一模第11题` 无「年」字，旧式漏识）
    r"|(?:19|20)\d{2}\s*年?\s*[\u4e00-\u9fa5]{2,12}?(?:一模|二模|期末|期中|月考)"
    # ⑤ 河南《解析与检测》2026 下册·综合检测卷 4
    r"|[\u4e00-\u9fa5]{0,6}《?解析与检测》?\s*(?:19|20)\d{2}\s*[上下]册[·・\.]?\s*综合检测卷\s*\d{1,2}"
    r")"
)
CITE = re.compile(r"(?P<vol>" + VOL_PAT + r")\s*第\s*(?P<seq>\d{1,2})\s*题")

# 「第N题」全部出现（用于统计未处理项）
ANY_SEQ = re.compile(r"第\s*(\d{1,2})\s*题")

# ---- 卷目录索引 ----
PLACES = ["洛阳", "郑州", "南阳", "信阳", "开封", "安阳", "新乡", "许昌", "漯河",
          "三门峡", "商丘", "周口", "驻马店", "平顶山", "焦作", "濮阳", "鹤壁", "济源",
          "广东", "山东", "江西", "湖北", "河北", "湖南", "安徽", "山西", "陕西", "全国",
          "河南", "实验中学", "二七区", "经开区", "涧西区", "洛龙区", "西工区",
          "中原区", "金水区", "驿城区"]

# 区级限定词：表述与目录必须"同有同无"，否则「洛阳市一模」会撞上「洛阳涧西区一模」
DISTRICTS = ["涧西区", "洛龙区", "西工区", "老城区", "瀍河区", "二七区", "经开区",
             "中原区", "金水区", "驿城区", "川汇区", "宛城区", "卧龙区", "实验中学"]


def build_volume_index():
    """卷目录索引。"""
    vols = []
    vault = vault_dir()
    for root in ROOTS:
        d = vault / root
        if not d.is_dir():
            continue
        for vol in sorted(d.iterdir()):
            if not vol.is_dir():
                continue
            vols.append({
                "rel": vol.relative_to(vault).as_posix(),
                "path": vol,
                "name": vol.name,
                "years": re.findall(r"(?:19|20)\d{2}", vol.name),
            })
    return vols


def vol_features(t: str):
    """抽取卷别文本的要素：年份 / 考试类型 / 册别 / 地名 / 检测卷序号。"""
    years = re.findall(r"(?:19|20)\d{2}", t)
    kinds = [k for k in ["中考", "高考", "期末", "期中", "一模", "二模", "月考"] if k in t]
    books = re.findall(r"([789][上下]|[上下]册|纲要[上下]|选必[123])", t)
    places = [p for p in PLACES if p in t]
    det = re.search(r"综合检测卷\s*(\d{1,2})", t)
    det_any = ("检测卷" in t) or ("解析与检测" in t)
    return years, kinds, books, places, det, det_any


def find_volume(vol_text: str, vols, report_book: str = ""):
    """要素匹配：年份硬约束 → 类型硬约束 → 册别约束 → 检测卷序号 → 地名/册别加分。

    返回同分候选（>1 视为歧义，交人工）。
    """
    years, kinds, books, places, det, det_any = vol_features(vol_text)
    vt = re.sub(r"\s", "", vol_text)
    want_b = "B卷" in vt
    want_dist = [d for d in DISTRICTS if d in vt]
    scored = []
    for v in vols:
        n = v["name"]
        vy = re.findall(r"(?:19|20)\d{2}", n)
        s = 0
        # A/B 卷硬判据：表述与目录的「B卷」属性必须一致，否则淘汰
        if want_b != ("B卷" in n):
            continue
        # 区级硬判据：表述有区名则目录须含该区名；表述无区名则排除区级卷
        n_dist = [d for d in DISTRICTS if d in n]
        if want_dist:
            if not all(d in n for d in want_dist):
                continue
        elif n_dist:
            continue
        if years:
            if set(years) == set(vy):
                s += 4
            elif set(years) <= set(vy):
                s += 2
            else:
                continue
        if kinds:
            if all(k in n for k in kinds):
                s += 4
            else:
                continue
        if books:
            if all(b in n for b in books):
                s += 3
            else:
                continue
        if det:
            if re.search(r"综合检测卷\s*%s(?![0-9])" % re.escape(det.group(1)), n):
                s += 5
            else:
                continue
        elif det_any:
            if "检测" not in n:
                continue
            s += 2
        for p in places:
            if p in n:
                s += 2
        if report_book and report_book in n:
            s += 1
        scored.append((s, v))
    if not scored:
        return []
    scored.sort(key=lambda kv: -kv[0])
    top = scored[0][0]
    return [v for s, v in scored if s == top]


def find_question(vol_dir: Path, seq: str):
    """在卷目录下找题目文件，返回 (Path, 候选数)。

    ⚠ 库内选择题/材料题文件名**补零**（`06.md`、`01.md`），而报告写「第 6 题」——
    必须同时尝试 `6.md` 与 `06.md` 两种形态，否则大面积误报「卷内无此号」。
    """
    names = ["%s.md" % seq]
    if len(seq) == 1:
        names.append("0%s.md" % seq)
    hits = []
    for nm in names:
        hits.extend(vol_dir.rglob(nm))
    hits = sorted(set(hits))
    if len(hits) == 1:
        return hits[0], 1
    if not hits:
        return None, 0
    # 多命中：优先材料解析题 > 综合探究/论述 > 填空 > 选择题（报告主体为非选择题）
    # 注意：匹配对象是目录名后缀，存量论述题目录名仍为 {卷名}_小论文，故保留该别名
    def pref(p):
        for i, k in enumerate(["材料解析题", "综合探究题", "论述题", "小论文", "填空题", "选择题"]):
            if k in p.parent.name:
                return i
        return 99
    hits.sort(key=pref)
    return hits[0], len(hits)


def alias_of(rel_path: str, seq: str) -> str:
    """由相对路径生成显示名：{卷名} 第 {N} 题。"""
    parts = rel_path.split("/")
    vol = parts[-2].rsplit("_", 1)[0] if len(parts) >= 3 else parts[-1]
    return "%s 第 %s 题" % (vol, int(seq))


def main():
    bootstrap()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--files", nargs="*", default=None,
                    help="指定笔记文件名（默认扫描 3-试题研究/ 全部 .md）")
    args = ap.parse_args()

    vols = build_volume_index()
    print("卷目录索引：%d 个" % len(vols))
    print()

    total_plan, total_unres, total_other = 0, 0, 0
    all_unres = []
    all_other = []

    fnames = args.files or sorted(p.name for p in note_dir().glob("*.md"))
    for fname in fnames:
        p = note_dir() / fname
        if not p.is_file():
            print("[跳过] 不存在：%s" % fname)
            continue
        text = p.read_text(encoding="utf-8")
        # 读报告自身册别，作为卷匹配的加分项
        report_book = ""
        mb = re.search(r"^册别:\s*(.+)$", text, re.M)
        if mb:
            report_book = re.findall(r"[789][上下]", mb.group(1))[0] if re.findall(r"[789][上下]", mb.group(1)) else ""
        lines = text.split("\n")
        # ⚠ frontmatter 必须跳过：属性字段里出现 `[[` 会被 YAML 当成列表语法，
        # 导致属性面板解析失败（2026-09-21 实测：把「核心结论」的值改成了 [[…]]）。
        fm_end = 0
        if lines and lines[0].strip() == "---":
            for _i in range(1, len(lines)):
                if lines[_i].strip() == "---":
                    fm_end = _i + 1
                    break
        plans = []
        unres = []
        covered = []          # 已匹配区间，用于统计"未处理"

        for li, line in enumerate(lines):
            if li < fm_end:          # frontmatter 区（含起止 --- 两行）不处理
                continue
            in_tbl = line.lstrip().startswith("|")
            # 幂等保护：已存在的 wikilink（含其别名文本）内的内容不再处理，
            # 否则别名「2022 河南中考 第 22 题」会被再次包一层链接。
            protected = [(pm.start(), pm.end()) for pm in re.finditer(r"\[\[[^\[\]]*\]\]", line)]
            for m in CITE.finditer(line):
                if any(m.start() >= a and m.end() <= b for a, b in protected):
                    continue
                vol_text, seq = m.group("vol").strip(), m.group("seq")
                covered.append((li, m.start(), m.end()))
                cands = find_volume(vol_text, vols, report_book)
                if not cands:
                    unres.append((li + 1, m.group(0), "卷目录未找到"))
                    continue
                if len(cands) > 1:
                    # 多个卷目录：优先年份完全一致者
                    yrs = set(re.findall(r"(?:19|20)\d{2}", vol_text))
                    narrow = [c for c in cands if yrs and yrs <= set(c["years"])]
                    if len(narrow) == 1:
                        cands = narrow
                    else:
                        unres.append((li + 1, m.group(0), "卷目录歧义 %d 个" % len(cands)))
                        continue
                vd = cands[0]
                q, nhi = find_question(vd["path"], seq)
                if q is None:
                    unres.append((li + 1, m.group(0), "卷内无此号（%s）" % vd["name"]))
                    continue
                rel = q.relative_to(vault_dir()).with_suffix("").as_posix()
                alias = alias_of(rel, seq)
                sep = "\\|" if in_tbl else "|"
                plans.append({
                    "line": li + 1, "old": m.group(0), "seq": seq,
                    "new": "[[%s%s%s]]" % (rel, sep, alias),
                    "vol": vd["name"], "nhi": nhi, "tbl": in_tbl,
                })
            # 统计未被 CITE 覆盖的「第N题」
            for m in ANY_SEQ.finditer(line):
                if any(m.start() >= a and m.end() <= b for a, b in protected):
                    continue
                if not any(li == cl and m.start() >= cs and m.end() <= ce for cl, cs, ce in covered):
                    all_other.append((fname, li + 1, line[max(0, m.start() - 25):m.end()].strip()))

        print("=" * 96)
        print("## %s" % fname)
        print("  计划建链 %d 处" % len(plans))
        byq = defaultdict(list)
        for r in plans:
            byq[r["seq"]].append(r)
        t = Counter(r["tbl"] for r in plans)
        print("    表格内 %d / 正文 %d ；卷内多候选需择优 %d" % (t[True], t[False], sum(1 for r in plans if r["nhi"] > 1)))
        print("  未能定位 %d 处" % len(unres))
        for ln, txt, why in unres[:10]:
            print("     行%-4d %-34s %s" % (ln, txt[:34], why))
        if len(unres) > 10:
            print("     …另 %d 处" % (len(unres) - 10))
        print()
        print("  —— 计划样例（前 8）——")
        for r in plans[:8]:
            print("   行%-4d %s" % (r["line"], r["old"]))
            print("        → %s" % r["new"])
        total_plan += len(plans)
        total_unres += len(unres)
        all_unres.extend([(fname,) + u for u in unres])

        if args.apply:
            new_lines = list(lines)
            # 同文件内按行号倒序替换，避免位置漂移
            for r in sorted(plans, key=lambda x: -x["line"]):
                ln = r["line"] - 1
                cur = new_lines[ln]
                if r["old"] in cur:
                    new_lines[ln] = cur.replace(r["old"], r["new"])
                    # 同步 covered 位置（此处简化：仅替换一次出现）
            p.write_text("\n".join(new_lines), encoding="utf-8")
            print("  >>> 已写盘：%d 处" % len(plans))

    print("=" * 96)
    print("合计：计划建链 %d 处；未能定位 %d 处；未纳入处理的「第N题」表述 %d 处"
          % (total_plan, total_unres, len(all_other)))
    if all_other:
        print("\n未纳入处理的表述（缺卷别/非题目指代，需人工确认）样例：")
        seen = set()
        for f, ln, ctx in all_other:
            k = ctx[-18:]
            if k in seen:
                continue
            seen.add(k)
            print("   [%s] 行%-4d …%s" % (f[:16], ln, ctx))
            if len(seen) >= 25:
                break
    if not args.apply:
        print("\n（分析模式：未改动任何文件；加 --apply 执行）")


if __name__ == "__main__":
    main()
