# -*- coding: utf-8 -*-
"""dedupe_questions.py — 题库查重与「一题多卷」合并（v1.3.0）。

判定流水线（规范见 references/fields.md「一题多卷与去重」）：
  L1  精确指纹     正文归一化后哈希（去图片引用/分值括注/题号/标点/空白）
  L1' 选项集合指纹 选项内容**排序后**拼接再哈希 —— 专治选项顺序被重排（实测有效）
  L2  近似相似度   3-gram Jaccard（长度分桶加速），阈值按题型区分
  L3  结构化预筛   同册别 + 课有交集 + 答案相同 → 提高置信
  （L4 AI 逐对裁定 / L5 用户裁定在清单基础上完成；脚本只负责"把候选缩小到可人工看"）

子命令：
  --scan                    全库查重，输出候选组（[--out 清单.md] [--json] [--阈值 X] [--卷 X]）
  --check <新题.json>       录入前预检：逐题返回库内疑似重复 Top-N（不写盘）
  --merge <主题.md> --with <别名.md>[,...] [--apply]
                            合并：主题追加别名 id/来源，别名题归档（默认 dry-run）
  --list-merged             列出已合并题（id 多值）
  --rollback <日志.json> [--apply]
                            按日志回滚（还原归档文件 + 恢复主题 frontmatter）
  --check-authority         全库体检：已合并题中「别名比主题更权威」的倒挂（只读）

合并规则（用户 2026-09-13 裁定）：
  · 主题 = 权威序最高者：中/高考真题 > 中/高考模拟 > 阶段测试；同级按**年份最早**者
  · **权威序倒挂 = 错误合并，脚本硬阻断**（2026-10-22 定；`--allow-inferior` 才放行）
  · 主题 id 列表追加别名 id（幂等）；来源**成对追加**（卷名, 类型）
  · 册别/单元/课/小节/知识点/核心素养/素养层次水平/关键能力 取并集（主题在前）
  · 年份/考区/难度/分值 保留主题值，差异写入 YAML「解析」备注（正文保持纯净，学生卷安全）
  · 别名题移入 {vault}/.trash/<原相对路径>（库内 Obsidian 回收站，可在面板还原到原位；⚠ 回收站可被清空，
    非可靠备份）；别名卷目录追加 `_重复题清单.md`（本卷题号/本卷 id/主题 id/合并日期/本卷分值——
    还原时**按本卷 id 取题**，索引对 id 多值每项都指向主卷文件）
  · 日志写入 {vault}/.dedupe/YYYY-MM-DD.json（含主题 frontmatter 快照，可 --rollback）
  · **不改任何其他笔记的链接**：引用别名题的笔记（卷目录下 `_组卷清单.md` 最常见）会静默断链，
    故预览阶段内置**引用预检**（列出受影响行，dry-run 也打印）；合并后跑
    `check_note_links.py --fix --apply` 改指主卷（2026-11-05 定）
"""
import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import date
from hashlib import sha1
from pathlib import Path

from qb_bootstrap import bootstrap, require_vault
from qb_schema import (ARCHIVE_ROOT, DEDUPE_LOG_DIR,
                       LEGACY_ARCHIVE_ROOTS, _yaml_scalar, as_list,
                       authority_inverted, authority_key, dump_yaml,
                       is_question_file, norm_for_dedupe, source_pairs,
                       split_frontmatter, stage_roots, strip_comments)

# 合并时取并集的字段（主题在前）
UNION_FIELDS = ["册别", "单元", "课", "小节", "知识点",
                "核心素养", "素养层次水平", "关键能力"]
# 保留主题值的字段（差异写入解析备注）
KEEP_FIELDS = ["年份", "考区", "难度", "分值"]
# 旧归档结构的中间层名（`<归档根>/重复题/<相对路径>`）——新落点已去掉该层，仅供历史日志回滚兼容
LEGACY_DUP_SUBDIR = "重复题"

OPT_MARK_RE = re.compile(r"[（(]\s*([A-F])\s*[)）]")
IMG_RE = re.compile(r"!\[\]\(images/([^)\s]+)\)")


# ---------------- 基础 ----------------

def load_md(path) -> tuple:
    """读取题目 md → (frontmatter 原文, frontmatter dict, 正文)。"""
    raw = Path(path).read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", raw, re.S)
    fm_raw, body = (m.group(1), m.group(2)) if m else ("", raw)
    fm, body2, err = split_frontmatter(raw)
    if err:
        print(f"[警告] {path}：{err}")
    return fm_raw, fm, body2 if err is None else body


def split_stem_options(fm: dict, body: str) -> tuple:
    """把题目拆成 (题干, 选项列表)。

    优先 YAML「选项」字段；无则从正文提取 `(A)…(B)…` 选项区（正文=题干+选项）。
    非选择题无选项标记 → 选项为 []，题干=全文。
    """
    text = strip_comments(body)
    opts = as_list(fm.get("选项"))
    if opts:
        # 正文中同样可能有选项（如手工录入），题干取「首个选项标记」之前的文本
        marks = list(OPT_MARK_RE.finditer(text))
        stem = text[:marks[0].start()].strip() if marks else text
        return stem, [str(o).strip() for o in opts]
    marks = list(OPT_MARK_RE.finditer(text))
    if len(marks) >= 2 and marks[0].group(1) == "A":
        stem = text[:marks[0].start()].strip()
        opts = []
        for i, mk in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            opts.append(re.sub(r"\s+", " ", text[mk.end():end]).strip())
        return stem, opts
    return text.strip(), []


def fingerprints(stem: str, opts: list, body: str) -> dict:
    """三层指纹：fp_full（含选项顺序）/ fp_optset（选项集合归一）/ fp_stem（仅题干）。"""
    ns = norm_for_dedupe(stem)
    no = [norm_for_dedupe(o) for o in opts]
    fp_full = sha1(norm_for_dedupe(body + "".join(opts)).encode()).hexdigest()
    fp_optset = sha1((ns + "|" + "|".join(sorted(no))).encode()).hexdigest()
    fp_stem = sha1(ns.encode()).hexdigest()
    return {"fp_full": fp_full, "fp_optset": fp_optset, "fp_stem": fp_stem}


def grams(text: str, n: int = 3) -> set:
    t = norm_for_dedupe(text)
    return {t[i:i + n] for i in range(len(t) - n + 1)} if len(t) >= n else set()


def sig_of(fm: dict, body: str) -> dict:
    """由题目 fm+正文生成比对签名（题干/选项/三层指纹），供 diff_note 等复用。"""
    stem, opts = split_stem_options(fm, body)
    return {"fm": fm, "stem": stem, "opts": opts, **fingerprints(stem, opts, body)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter else 0.0


def sim_threshold(fm: dict, override: float = None) -> float:
    """相似度阈值：选择题放宽（结构固定、易改字），材料/论述从严（套话多、易误报）。"""
    if override is not None:
        return override
    return 0.85 if fm.get("题型") == "选择题" else 0.72


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def iter_questions(only_vol: str = None) -> list:
    """读取全库题目 → [{path, rel, fm, body, stem, opts, fp*, g3}, ...]"""
    items = []
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            if only_vol and only_vol not in p.parts:
                continue
            fm_raw, fm, body = load_md(p)
            stem, opts = split_stem_options(fm, body)
            fp = fingerprints(stem, opts, body)
            rec = {
                "path": p, "rel": p.relative_to(require_vault()), "fm": fm,
                "body": body, "stem": stem, "opts": opts,
                "g3": grams(stem + "".join(opts)),
                "nlen": len(norm_for_dedupe(stem + "".join(opts))),
            }
            rec.update(fp)
            items.append(rec)
    return items


def diff_note(theme: dict, alias: dict) -> str:
    """生成主题 vs 别名 的差异描述（供写入主题「解析」备注）。"""
    notes = []
    t, a = theme["fm"], alias["fm"]
    if str(t.get("分值")) != str(a.get("分值")):
        notes.append(f"分值 {t.get('分值')}→{a.get('分值')}")
    if str(t.get("难度")) != str(a.get("难度")):
        notes.append(f"难度 {t.get('难度')}→{a.get('难度')}")
    ta, aa = str(t.get("答案") or "").strip(), str(a.get("答案") or "").strip()
    if ta != aa:
        short = f"（{ta} → {aa}）" if max(len(ta), len(aa)) <= 14 else ""
        notes.append(f"答案不同{short}")
    t_opts = sorted(norm_for_dedupe(o) for o in theme.get("opts") or [])
    a_opts = sorted(norm_for_dedupe(o) for o in alias.get("opts") or [])
    if theme["fp_full"] != alias["fp_full"]:
        if t_opts and t_opts == a_opts:
            notes.append("选项顺序不同")
        elif t_opts != a_opts:
            notes.append("选项有增删/替换")
    if theme["fp_stem"] != alias["fp_stem"]:
        notes.append("题干文字有差异")
    return "；".join(notes) if notes else "无"


# ---------------- 查重 ----------------

def find_groups(items: list, override: float = None) -> list:
    """找出全部疑似重复组（并查集归并 L1/L1'/L2 命中）。"""
    n = len(items)
    dsu = DSU(n)
    by_optset, by_stem = defaultdict(list), defaultdict(list)
    for i, it in enumerate(items):
        by_optset[it["fp_optset"]].append(i)
        by_stem[it["fp_stem"]].append(i)
    for bucket in (by_optset, by_stem):
        for _, idxs in bucket.items():
            for j in idxs[1:]:
                dsu.union(idxs[0], j)
    # L2 相似度（长度分桶，仅比较相邻桶）
    length_buckets = defaultdict(list)
    for i, it in enumerate(items):
        length_buckets[it["nlen"] // 40].append(i)
    for b, idxs in length_buckets.items():
        cand = [j for k in (b - 1, b, b + 1) for j in length_buckets.get(k, [])]
        for i in idxs:
            for j in cand:
                if j <= i:
                    continue
                th = max(sim_threshold(items[i]["fm"], override),
                         sim_threshold(items[j]["fm"], override))
                if jaccard(items[i]["g3"], items[j]["g3"]) >= th:
                    dsu.union(i, j)
    groups = defaultdict(list)
    for i in range(n):
        groups[dsu.find(i)].append(i)
    out = []
    for _, idxs in groups.items():
        if len(idxs) < 2:
            continue
        members = [items[i] for i in idxs]
        # 权威序排序键：卷名由 authority_key 内部按 path 兜底（frontmatter 无「卷名」字段），
        # 此处勿再手工补 `rec["卷名"]`——早期补丁只覆盖了本处，--merge 与 --check-authority 漏补，
        # 导致卷名一级恒空、权威序退化为按题号排（2026-10-31 修）。
        members.sort(key=lambda it: authority_key(it["fm"], it["path"]))
        # 判定级别（取组内最高强度）
        fp_full = {m["fp_full"] for m in members}
        if len(fp_full) == 1:
            level = "同题·精确"
        elif len({m["fp_optset"] for m in members}) == 1:
            level = "同题·选项序不同"
        elif len({m["fp_stem"] for m in members}) == 1:
            level = "疑似·题干同"
        else:
            level = "疑似·文本相似"
        sims = []
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                sims.append(jaccard(members[x]["g3"], members[y]["g3"]))
        out.append({"级别": level, "成员": members,
                    "相似度": max(sims) if sims else 1.0,
                    "需人工裁定": level.startswith("疑似") or (max(sims) if sims else 1.0) < 0.95})
    out.sort(key=lambda g: (-g["相似度"], g["成员"][0]["rel"].parts[1]))
    return out


def fmt_member(m: dict, with_diff: str = None) -> list:
    fm = m["fm"]
    ids = as_list(fm.get("id"))
    books = "、".join(as_list(fm.get("册别")))
    lessons = "、".join(as_list(fm.get("课")))
    ans = str(fm.get("答案") or "").replace("\n", " ")[:16]
    alias = f"（另收录 {len(ids) - 1} 处）" if len(ids) > 1 else ""
    lines = [f"- 文件：`{m['rel']}`",
             f"  id：{ids[0] if ids else '—'}{alias} ｜ {books} {lessons} ｜ {fm.get('题型')}"
             f" ｜ 难度{fm.get('难度')} ｜ {fm.get('分值')}分 ｜ 答案 {ans}"]
    if with_diff:
        lines.append(f"  差异：{with_diff}")
    return lines


def cmd_scan(args) -> None:
    items = iter_questions(args.卷)
    groups = find_groups(items, args.阈值)
    if args.json:
        print(json.dumps([{
            "级别": g["级别"], "相似度": round(g["相似度"], 3), "需人工裁定": g["需人工裁定"],
            "主题": {"文件": str(g["成员"][0]["rel"]),
                     "id": as_list(g["成员"][0]["fm"].get("id")),
                     "权威序": list(authority_key(g["成员"][0]["fm"], g["成员"][0]["path"]))},
            "成员": [{"文件": str(m["rel"]), "id": as_list(m["fm"].get("id")),
                      "差异（相对主题）": diff_note(g["成员"][0], m) if m is not g["成员"][0] else "主题"}
                     for m in g["成员"]],
        } for g in groups], ensure_ascii=False, indent=2))
        return
    L = [f"# 题库疑似重复清单 · {date.today().isoformat()}", "",
         f"扫描题目 {len(items)} 题，命中疑似重复 {len(groups)} 组。"
         f"（判定流水线 L1 精确 / L1' 选项集合 / L2 相似度；L4/L5 需人工裁定）", ""]
    for gi, g in enumerate(groups, 1):
        theme = g["成员"][0]
        tag = "⚠ 需人工裁定" if g["需人工裁定"] else "✅ 高置信同题"
        L.append(f"## 候选 {gi} · {g['级别']} · 相似度 {g['相似度']:.2f} · {tag}")
        L.append("")
        L.append("**建议主题（权威序最高）**：")
        L.extend(fmt_member(theme))
        for m in g["成员"][1:]:
            L.append("**别名**：")
            L.extend(fmt_member(m, diff_note(theme, m)))
        aliases = ",".join(f'"{m["path"]}"' for m in g["成员"][1:])
        L.append(f'**合并命令**：`python dedupe_questions.py --merge "{theme["path"]}" '
                 f'--with {aliases} --apply`')
        L.append("")
    text = "\n".join(L)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\n[清单] 已写入 {args.out}")


def cmd_check(args) -> None:
    """录入前预检：新题 JSON 逐题与库内比对，返回 Top-N 疑似。"""
    raw = json.loads(Path(args.check).read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else [raw]
    items = iter_questions(args.卷)
    hit_total = 0
    for k, rec in enumerate(records, 1):
        stem = str(rec.get("题干", "")).strip()
        opts = as_list(rec.get("选项"))
        fp = fingerprints(stem, opts, stem + "".join(str(o) for o in opts))
        g3 = grams(stem + "".join(str(o) for o in opts))
        scored = []
        for it in items:
            same_full = it["fp_full"] == fp["fp_full"]
            same_optset = it["fp_optset"] == fp["fp_optset"]
            same_stem = it["fp_stem"] == fp["fp_stem"]
            sim = jaccard(g3, it["g3"])
            th = max(sim_threshold(rec), sim_threshold(it["fm"], args.阈值))
            if not (same_full or same_optset or same_stem or sim >= th):
                continue
            lvl = ("同题·精确" if same_full else "同题·选项序不同" if same_optset
                   else "疑似·题干同" if same_stem else "疑似·文本相似")
            scored.append((sim, lvl, it))
        scored.sort(key=lambda x: -x[0])
        label = rec.get("id") or rec.get("序号") or f"新题{k}"
        if not scored:
            print(f"✓ [{label}] 库内无相似题，可入库")
            continue
        hit_total += 1
        print(f"⚠ [{label}] 命中 {len(scored)} 条疑似，Top{min(len(scored), args.top)}：")
        for sim, lvl, it in scored[:args.top]:
            print(f"   · {lvl}（相似度 {sim:.2f}）→ {it['rel']}"
                  f" ｜ {'、'.join(as_list(it['fm'].get('课')))} ｜ 答案 {it['fm'].get('答案')}")
        print(f"   → 若判为同题：跑 `--merge <库内主题.md> --with <本题入库后的 md> --apply`，本题不入库")
    print(f"\n预检完成：{len(records)} 题，{hit_total} 题命中疑似重复")


# ---------------- 权威序体检 ----------------

def iter_archived() -> list:
    """读取归档区（.trash）下的别名题 → [(path, fm), ...]。

    `stage_roots()` 天然不含 `.trash`，故体检须单独遍历；
    `is_question_file()` 对归档件仍返回 True（其保持题库内相对路径）。
    """
    root = require_vault() / ARCHIVE_ROOT
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*.md")):
        if not is_question_file(p):
            continue
        fm, _body, err = split_frontmatter(p.read_text(encoding="utf-8"))
        if err:
            print(f"[警告] 归档件 frontmatter 异常，跳过体检：{p}（{err}）")
            continue
        out.append((p, fm))
    return out


def find_inverted() -> list:
    """全库体检：主卷权威序**明确低于**其某个别名（别名更权威）→ 倒挂清单（只读）。

    判据用 `authority_inverted`（真题 > 模拟 > 阶段测试；同级年份早者优先）——
    **只比 (类别, 考试类型细分, 年份) 前三位**：同年同类不比卷名/题号，
    否则「两所学校的二模」这类无客观优劣的组合会被卷名字典序判出假倒挂
    （2026-10-31 修，实测 9/21 条此类噪声）。
    别名 id 在归档区找不到对应文件时跳过（无法判定其类别，不臆测）。
    """
    arch = {}
    for p, fm in iter_archived():
        for i in as_list(fm.get("id")):
            arch.setdefault(str(i), (p, fm))
    rows = []
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            fm, _body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err:
                continue
            ids = [str(x) for x in as_list(fm.get("id"))]
            if len(ids) < 2:
                continue
            tk = authority_key({**fm, "序号": p.stem}, p)
            for a in ids[1:]:
                hit = arch.get(a)
                if not hit:
                    continue
                ap, afm = hit
                ak = authority_key({**afm, "序号": ap.stem}, ap)
                if authority_inverted(ak, tk):
                    rows.append({"theme": p, "theme_key": tk, "alias_id": a,
                                 "alias_path": ap, "alias_key": ak})
    return rows


def cmd_check_authority(args) -> None:
    rows = find_inverted()
    if args.json:
        print(json.dumps([{
            "主题文件": str(r["theme"].relative_to(require_vault())),
            "主题权威序": list(r["theme_key"]),
            "别名": r["alias_id"],
            "别名文件（归档）": str(r["alias_path"].relative_to(require_vault())),
            "别名权威序": list(r["alias_key"]),
        } for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        print("✅ 权威序体检通过：全库无「别名比主卷更权威」的倒挂合并")
        return
    print(f"⚠ 权威序倒挂 {len(rows)} 处 —— 违反「真题 > 模拟 > 阶段测试，同级年份早者优先」"
          f"（判据只比 类别/考试类型/年份；同年同类不比卷名与题号）\n")
    for r in rows:
        tk, ak = r["theme_key"], r["alias_key"]
        print(f"· 主卷 {tk[:3]}（{tk[3] or '-'} 第 {tk[4]} 题）"
              f"  {r['theme'].relative_to(require_vault())}")
        print(f"  别名 {ak[:3]}（{ak[3] or '-'} 第 {ak[4]} 题）  {r['alias_id']}"
              f"（归档于 {r['alias_path'].relative_to(require_vault())}）")
        print(f"  → 应以别名为主卷。修正步骤：① 把主卷 frontmatter 还原为合并前快照"
              f"（见 .dedupe 日志 theme_frontmatter_before）；② 还原归档件到原位；")
        print(f"     ③ python dedupe_questions.py --merge \"{r['alias_path']}\" "
              f"--with \"{r['theme']}\" --apply")
    print("\n[提示] 倒挂多为「合并时手工指定了错误主题」所致；"
          "`--merge` 现已在写盘前硬阻断此类合并。")


# ---------------- 合并 ----------------

def render_field(key: str, value) -> list:
    """渲染单字段 YAML 行（复用 dump_yaml 的标量/列表/块渲染规则）→ [首行, 续行...]。"""
    block = dump_yaml({key: value}).splitlines()
    return [ln for ln in block if ln != "---"]


def replace_field(fm_raw: str, key: str, value) -> str:
    """在 frontmatter 原文中替换/追加某字段（保留其余字段与风格，改动最小）。

    ⚠ 存量题库的 list 字段有**块状**（`key:` 换行后顶格 `- 值`）与流式（`key: [值]`）两种写法：
    旧值清理必须同时吞掉「缩进行」与「顶格 `- ` 行」，否则残留行会破坏 YAML（2026-09-13 实测踩坑）。
    渲染时跟随原字段风格（原为块状则仍写块状），让 diff 最小。
    """
    lines = fm_raw.splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        if ln.startswith(key + ":"):
            start = i
            j = i + 1
            while j < len(lines) and (lines[j][:1] in (" ", "\t")
                                      or lines[j].lstrip().startswith("- ")):
                j += 1
            end = j
            break
    blocky = bool(start is not None and end and end > start + 1
                  and lines[start + 1].lstrip().startswith("- "))
    if blocky and isinstance(value, (list, tuple)):
        head, cont = f"{key}:", [f"- {_yaml_scalar(x)}" for x in value]
    else:
        rendered = render_field(key, value)
        head, cont = rendered[0], rendered[1:]
    if start is None:
        return "\n".join(lines + [head] + cont)
    return "\n".join(lines[:start] + [head] + cont + lines[end:])


def append_body_images(body: str, refs: list) -> str:
    if not refs:
        return body
    add = [r for r in refs if r not in body]
    return body.rstrip() + "\n\n" + "\n\n".join(add) + "\n" if add else body


def infer_alias_id(ap: Path) -> str:
    """按「卷名-题号」推断题目 id（与 gen_problem 的默认 id 规则一致）。"""
    return f"{ap.parent.parent.name}-{ap.stem}"


def file_digest(p: Path) -> str:
    """文件内容指纹（用于判定「归档目标已存在」是否为同一份内容）。"""
    return sha1(p.read_bytes()).hexdigest()


def unique_image_name(theme_dir: Path, theme_seq: str, src_name: str, taken: set = None) -> str:
    """为主题题生成不冲突的图片名 `{序号}_{n}{ext}`（避开磁盘已有 + 本批已分配）。"""
    ext = Path(src_name).suffix or ".jpg"
    img_dir = theme_dir / "images"
    existing = {p.name for p in img_dir.glob(f"{theme_seq}_*")} if img_dir.exists() else set()
    existing |= set(taken or ())
    n = 1
    while f"{theme_seq}_{n}{ext}" in existing:
        n += 1
    return f"{theme_seq}_{n}{ext}"


def preview_refs(theme_path: Path, alias_files, alias_ids) -> int:
    """合并前**引用影响预检**（2026-11-05 定）。

    别名题归档后，任何按原路径写的 wikilink 都会静默变灰 —— 最典型的是卷目录下的
    `_组卷清单.md`：它按 `[[{类别}/{卷名}/{卷名}_{题型}/{序号}|{id}]]` 写死路径，
    合并后路径失效、Obsidian 只把链接染灰，**不报错**（"静默断链"）。

    本函数只**报告**，不改盘；修链走 `check_note_links.py --fix --apply`（可 --rollback）。
    返回命中条数；预检本身不可用时返回 -1（不阻断合并）。
    """
    try:
        import check_note_links as cnl
    except Exception as e:                      # 索引依赖不可用不应阻断合并
        print(f"[引用预检] 跳过（{e}）")
        return -1
    vault = require_vault()
    try:
        id2path, rel2ids, hits = cnl.build_index()
        theme_rel = theme_path.relative_to(vault).with_suffix("").as_posix()
        archive_rels = set()
        for ap in alias_files:
            try:
                archive_rels.add(Path(ap).relative_to(vault).with_suffix("").as_posix())
            except ValueError:
                archive_rels.add(Path(ap).with_suffix("").as_posix())
        want_ids, want_paths = set(), set(archive_rels)
        for i in alias_ids:
            if not i:
                continue
            want_ids.add(i)
            for _arch, rel in hits.get(i, []):
                if rel != theme_rel:            # 指向主卷的引用不受影响，不报
                    want_paths.add(rel)
                want_ids.update(rel2ids.get(rel, []))
        want_ids.discard("")
        out = []
        for d in cnl.default_note_dirs():
            for f in sorted((vault / d).rglob("*.md")):
                try:
                    text = cnl.read_text_raw(f)
                except Exception:
                    continue
                relf = f.relative_to(vault).as_posix()
                for ln, line in enumerate(text.splitlines(), 1):
                    for m in cnl.LINK_RE.finditer(line):
                        path, _a, _s = cnl.split_link(m.group(1))
                        rel = path[:-3] if path.endswith(".md") else path
                        if rel == theme_rel:
                            continue                # 主卷链接不失效
                        cand = set(cnl.derive_old_ids(rel)) | set(rel2ids.get(rel, []))
                        if rel in want_paths or (cand & want_ids):
                            out.append((relf, ln, m.group(0)))
    except Exception as e:
        print(f"[引用预检] 跳过（{e}）")
        return -1
    if not out:
        print("[引用预检] ✓ 无笔记引用待归档题，合并不会产生断链")
        return 0
    print(f"[引用预检] ⚠ {len(out)} 处笔记引用待归档题，合并后这些链接会失效（Obsidian 只染灰、不报错）：")
    for relf, ln, link in out:
        print(f"  {relf} 行 {ln}：{link}")
    print("  → 合并后执行：python scripts/check_note_links.py --fix --apply（可 --rollback）")
    return len(out)


def cmd_merge(args) -> None:
    theme_path = Path(args.merge)
    alias_paths = [Path(s.strip()) for s in (args.with_ or "").split(",") if s.strip()]
    if not theme_path.exists():
        sys.exit(f"[失败] 主题文件不存在：{theme_path}")
    theme_fm_raw, theme_fm, theme_body = load_md(theme_path)
    theme_ids = [str(x) for x in as_list(theme_fm.get("id"))]
    # 别名校验放在读入主题之后：文件已不在（在 .trash）但 id 已在主题 id 列表中，
    # 属「已合并」稳态，跳过即可——否则 gen --force 重建后重跑 merge 会在这里误报失败。
    archived_now = []
    for ap in alias_paths:
        if ap.exists():
            continue
        guess = infer_alias_id(ap)
        if guess in theme_ids:
            archived_now.append((ap, guess))
        else:
            sys.exit(f"[失败] 别名文件不存在：{ap}（按路径推断 id={guess}，"
                     f"主题 id 列表中也无此项，无法判定为已合并）")
    for ap, gid in archived_now:
        print(f"[已合并] {ap.name}（id {gid} 已在主题 id 列表中，文件在归档区，跳过）")
    alias_paths = [ap for ap in alias_paths if ap.exists()]
    seq = theme_path.stem
    new_ids, new_src, union_new, notes, img_refs = (list(theme_ids),
                                                    list(as_list(theme_fm.get("来源"))), {}, [], [])
    used_img_names = set()  # 批内已分配的新图名（避免同一批多条别名撞名）
    entries = []
    idempotent = 0           # 别名 id 已在主题、本次只做归档清理的条数
    inverted = []            # 权威序倒挂：别名比主题更权威（硬阻断，除非 --allow-inferior）
    for ap in alias_paths:
        a_fm_raw, a_fm, a_body = load_md(ap)
        a_ids = [str(x) for x in as_list(a_fm.get("id"))]
        if not a_ids:
            sys.exit(f"[失败] 别名题缺 id：{ap}")
        a_primary = a_ids[0]
        # 权威序校验：主题应不劣于别名
        tk = authority_key({**theme_fm, "序号": seq}, theme_path)
        ak = authority_key({**a_fm, "序号": ap.stem}, ap)
        if authority_inverted(ak, tk) and a_primary not in theme_ids:
            # 仅对「要真正追加」的别名阻断；幂等清理项不改变合并方向，交给 --check-authority 报
            inverted.append((ap, a_primary, ak, tk))
        if a_primary in theme_ids:
            print(f"[提示] 别名 id 已在主题 id 列表中：{a_primary}"
                  f"（幂等：只做归档清理，不重复追加 id/来源/解析）")
            idempotent += 1
            note, local_refs = "", []
        else:
            new_ids.append(a_primary)
            for name, tag in source_pairs(a_fm):
                if name and name not in new_src:
                    new_src += [name, tag]
            for f in UNION_FIELDS:
                tv, av = as_list(theme_fm.get(f)), as_list(a_fm.get(f))
                add = [x for x in av if x not in tv]
                if add:
                    union_new[f] = tv + add
            note = diff_note(sig_of(theme_fm, theme_body), sig_of(a_fm, a_body))
            # 图片：从别名正文取引用 → 复制到主题 images/ 并重命名（幂等时不重复取图）
            local_refs = []
            for name in IMG_RE.findall(a_body):
                src = ap.parent / "images" / name
                if not src.exists():
                    print(f"[警告] 别名图片缺失：{src}")
                    continue
                new_name = unique_image_name(theme_path.parent, seq, name, used_img_names)
                used_img_names.add(new_name)
                local_refs.append((src, new_name))
                img_refs.append(f"![](images/{new_name})")
            notes.append(f"《{a_primary}》{note}")
        entries.append({"file": str(ap), "id": a_primary,
                        "archived_to": str(require_vault() / ARCHIVE_ROOT
                                           / ap.relative_to(require_vault())),
                        "images": [{"src": str(s), "name": nm} for s, nm in local_refs],
                        "yaml_note": (f"> 【一题多卷】另见《{a_primary}》（差异：{note}）"
                                      if note else "")})

    # 权威序倒挂 → 写盘前硬阻断（dry-run 同样拒绝，避免误以为「预览通过」）
    if inverted:
        print("\n[权威序倒挂] 以下别名比主题更权威 —— 违反"
              "「真题 > 模拟 > 阶段测试，同级年份早者优先」"
              "（判据只比 类别/考试类型/年份 三项）：")
        for ap, aid, ak, tk in inverted:
            print(f"  · 别名 {aid}  {ak[:3]} ＜ 主题 {tk[:3]}"
                  f"（卷名 {ak[3] or '-'}｜{tk[3] or '-'}，题号 {ak[4]}｜{tk[4]}）")
            print(f"    {ap}")
        # 多个倒挂别名时取权威序**最高**者（只比前三项）作建议主题，不取「第一个」
        best = min(inverted, key=lambda x: x[2][:3])[0]
        print("\n[正确做法] 应改用更权威者作主题"
              "（先把现主题 frontmatter 还原为合并前快照，再执行）：")
        print(f'  python dedupe_questions.py --merge "{best}" '
              f'--with "{theme_path}" --apply')
        if not args.allow_inferior:
            sys.exit("[失败] 已拒绝执行：合并方向与权威序相反。"
                     "确属特例请加 --allow-inferior 放行")
        print("[放行] 已按 --allow-inferior 继续；本次倒挂将写入日志")

    # 主题新 frontmatter
    if not entries:
        print("[无需合并] 别名题均已在归档区（合并态稳定），本次无动作。")
        return
    note_lines = [e["yaml_note"] for e in entries if e.get("yaml_note")]
    yaml_changed = bool(note_lines or union_new
                        or new_ids != theme_ids
                        or new_src != as_list(theme_fm.get("来源")))
    fm_new = theme_fm_raw
    if yaml_changed:
        fm_new = replace_field(fm_new, "id", new_ids)
        if len(new_src) != len(as_list(theme_fm.get("来源"))):
            fm_new = replace_field(fm_new, "来源", new_src)
        for f, v in union_new.items():
            fm_new = replace_field(fm_new, f, v)
        if note_lines:
            old_jx = str(theme_fm.get("解析") or "").strip()
            fm_new = replace_field(fm_new, "解析",
                                   "\n".join(([old_jx] if old_jx else []) + note_lines))

    print(f"═══ 合并预览（{'执行' if args.apply else 'dry-run，未写盘'}）═══")
    print(f"主题：{theme_path}")
    print(f"  id：{theme_ids} → {new_ids}")
    print(f"  来源：{as_list(theme_fm.get('来源'))} → {new_src}")
    for f, v in union_new.items():
        print(f"  {f}：追加 {[x for x in v if x not in as_list(theme_fm.get(f))]}")
    if note_lines:
        print(f"  解析：追加 {len(note_lines)} 行「一题多卷」备注：")
        for ln in note_lines:
            print(f"    {ln}")
    else:
        print("  解析：（无新增；别名均为已合并项）")
    print(f"  正文：追加图片引用 {img_refs if img_refs else '（无）'}")
    for e in entries:
        print(f"别名：{e['file']}")
        print(f"  → 归档至 {e['archived_to']}")
    preview_refs(theme_path, [Path(e["file"]) for e in entries], [e["id"] for e in entries])
    if not args.apply:
        print("\n[dry-run] 加 --apply 执行；执行前请确认权威序与差异说明无误。")
        return

    # 归档目标预检（赶在写主题之前）：目标已在归档区且内容不同 → 拒绝覆盖
    # .trash 是还原原卷的唯一留痕，静默覆盖会让留痕失真。
    conflicts = []
    for e in entries:
        src_p, dst_p = Path(e["file"]), Path(e["archived_to"])
        if not dst_p.exists() or src_p.stat().st_size == 0:
            continue          # 空壳（0 字节）按幂等清理删源，不当冲突拦下
        if file_digest(dst_p) != file_digest(src_p):
            conflicts.append((src_p, dst_p))
    if conflicts:
        for src_p, dst_p in conflicts:
            print(f"[冲突] 目标：{dst_p}\n        卷内：{src_p}", file=sys.stderr)
        sys.exit("[失败] 归档目标已存在且内容不同，拒绝覆盖；"
                 "请人工核对后删除其一（或先 --rollback）再重试")

    if yaml_changed:
        # 写主题 + 立即自检 frontmatter（防静默破坏；此时尚未归档，修正后可直接重试）
        theme_path.write_text(f"---\n{fm_new}\n---\n\n"
                              f"{append_body_images(theme_body, img_refs).strip()}\n",
                              encoding="utf-8")
        chk_fm, _, chk_err = split_frontmatter(theme_path.read_text(encoding="utf-8"))
        if chk_err or not chk_fm.get("id"):
            print(f"❌ 主题 frontmatter 校验失败：{chk_err or '缺少 id 字段'}\n"
                  f"   主题 YAML 已改动、别名尚未归档；请手工修正该文件后重试", file=sys.stderr)
            sys.exit(1)
        print(f"[自检] 主题 frontmatter 解析正常：id={chk_fm.get('id')}")
    else:
        print("[幂等] 主题 YAML 无变更，跳过写盘")
    # 复制图片
    for e in entries:
        for it in e["images"]:
            dst = theme_path.parent / "images" / it["name"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(it["src"], dst)
            print(f"[图片] {Path(it['src']).name} → {it['name']}")
    # 归档 + 卷内清单 + 日志
    today = date.today().isoformat()
    log_path = require_vault() / DEDUPE_LOG_DIR / f"{today}.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"date": today, "entries": []}
    real_archived = 0
    for e in entries:
        src_p = Path(e["file"])
        dst_p = Path(e["archived_to"])
        size = src_p.stat().st_size
        # 本卷分值：**必须在 move 之前读**（归档后文件已不在卷内）；读不到记「—」。
        # 主卷分值未必等于本卷分值（同题跨卷收录时题型/赋分可不同），故单列留存。
        try:
            fm_src, _, _ = split_frontmatter(src_p.read_text(encoding="utf-8"))
            score = str(fm_src.get("分值", "")).strip() or "—"
        except Exception:
            score = "—"
        if dst_p.exists():
            # 目标已在归档区且内容一致（幂等：gen --force 重跑后重跑 merge 的常见形态）
            # → 只删卷内重建件，不重复 move（避免 Windows 上 move 到已存在路径的异常态）
            src_p.unlink()
            e["idempotent"] = True
            print(f"[幂等] 归档区已有同内容文件，删除卷内重建件：{src_p.name}")
        else:
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_p), str(dst_p))
            if not dst_p.exists() or dst_p.stat().st_size != size:
                got = f"{dst_p.stat().st_size} 字节" if dst_p.exists() else "文件不存在"
                sys.exit(f"[失败] 归档校验不通过：{dst_p}（期望 {size} 字节，实得 {got}）")
            real_archived += 1
            print(f"[归档] {src_p.name} → {dst_p}")
            # 同题号前缀的图片一并复制到归档目录（保留原图，供还原原卷）
            img_dir = src_p.parent / "images"
            if img_dir.exists():
                arch_img = dst_p.parent / "images"
                for f in img_dir.glob(f"{src_p.stem}_*"):
                    arch_img.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, arch_img / f.name)
        # 卷内清单（已记过该题号则不再追加，防重复执行堆行）
        vol_dir = src_p.parent.parent
        ck = vol_dir / "_重复题清单.md"
        line = f"| {src_p.stem} | {e['id']} | {new_ids[0]} | {today} | {score} |"
        if not ck.exists():
            ck.write_text(f"# 重复题清单 · {vol_dir.name}\n\n"
                          f"> 本卷以下题目与题库内权威版本重复，按「一题多卷」规则归档（位置 "
                          f"`{ARCHIVE_ROOT}/{dst_p.parent.relative_to(require_vault() / ARCHIVE_ROOT).as_posix()}`，"
                          f"即库内 Obsidian 回收站，可在回收站面板还原到原位）。\n"
                          f"> 题库不重复存题；**还原本卷时按「本卷 id」取题**——索引对 `id` 多值的每一项"
                          f"都指向主卷文件，别名 id 可直接用，不必绕「主题 id」。\n"
                          f"> ⚠ 归档件在 `.trash`，**清空回收站即丢**；早期清单头部写的 `_归档/重复题/…` 路径已作废。\n\n"
                          f"| 本卷题号 | 本卷 id | 主题 id | 合并日期 | 本卷分值 |\n|---|---|---|---|---|\n",
                          encoding="utf-8")
        existed_text = ck.read_text(encoding="utf-8")
        # 旧四列清单（2026-10-30 前生成）→ 就地升级表头；旧行少一格渲染为空，前四列不错位。
        # ⚠ 不能按整串精确替换：Obsidian 会自动把表格重排成列宽对齐形态（实测 40 份里 3 份
        #   呈 `| ---- | -------- |` 变体），故按「表头行 + 紧随的分隔行」逐行改。
        if "本卷分值" not in existed_text:
            lines = existed_text.split("\n")
            for i, ln in enumerate(lines):
                if re.match(r"^\|\s*本卷题号\s*\|", ln):
                    lines[i] = "| 本卷题号 | 本卷 id | 主题 id | 合并日期 | 本卷分值 |"
                    if i + 1 < len(lines) and re.match(r"^\|[\s:|-]+$", lines[i + 1]):
                        lines[i + 1] = "|---|---|---|---|---|"
                    existed_text = "\n".join(lines)
                    ck.write_text(existed_text, encoding="utf-8")
                    print(f"[清单] 表头升级为五列（补「本卷分值」）：{ck}")
                    break
        if f"| {src_p.stem} |" not in existed_text:
            with ck.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            e["checklist_line"] = line
        e["checklist_file"] = str(ck)
    # 结束自检：卷内原路径不应残留；0 字节空壳直接清掉（成因不赌，按结果兜底）
    for e in entries:
        leftover = Path(e["file"])
        if leftover.exists():
            sz = leftover.stat().st_size
            if sz == 0:
                leftover.unlink()
                print(f"[清理] 卷内 0 字节空壳：{leftover}")
            else:
                print(f"[警告] 卷内仍有残留（{sz} 字节，未自动删除）：{leftover}")
    if yaml_changed or real_archived:
        log["entries"].append({"theme_file": str(theme_path), "theme_id": theme_ids[0] if theme_ids else "",
                               "theme_frontmatter_before": theme_fm_raw,
                               "body_appended_refs": img_refs, "aliases": entries,
                               "authority_inverted": [
                                   {"alias_id": aid, "alias_key": list(ak), "theme_key": list(tk)}
                                   for _, aid, ak, tk in inverted]})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[日志] {log_path}（--rollback 可回滚）")
    else:
        print("\n[日志] 本次无实质变更（全为幂等清理），不追加日志")
    if idempotent:
        print(f"[幂等] {idempotent} 条别名的 id 已在主题列表中，YAML 未重复追加")
    print("✅ 合并完成；请重跑 `python scan_questions.py` 复核")


def cmd_list_merged(args) -> None:
    items = iter_questions(args.卷)
    rows = [it for it in items if len(as_list(it["fm"].get("id"))) > 1]
    if args.json:
        print(json.dumps([{"文件": str(it["rel"]), "id": as_list(it["fm"].get("id")),
                           "来源": as_list(it["fm"].get("来源"))} for it in rows],
                         ensure_ascii=False, indent=2))
        return
    print(f"═══ 一题多卷题一览（{len(rows)} 题）═══")
    for it in rows:
        ids = as_list(it["fm"].get("id"))
        print(f"\n· 主题 {ids[0]}  ←  {it['rel']}")
        for a in ids[1:]:
            print(f"    别名 {a}")


def resolve_archived(raw_path) -> Path:
    """定位归档件，兼容改名前的历史日志。

    `.dedupe/*.json` 里的 `archived_to` 是**绝对路径快照**；归档落点先后历 `_归档` →
    `.Z-Archive` → 库内回收站 `.trash`，旧日志按原路径找不到文件。旧代码 `if src.exists()` 会静默跳过
    （既不报错也不还原），故此处按 LEGACY_ARCHIVE_ROOTS 把旧根名映射到现行
    ARCHIVE_ROOT，命中即返回；都找不到则返回原路径，交由调用方判存在性。
    """
    p = Path(raw_path)
    if p.exists():
        return p
    parts = list(p.parts)
    for i, seg in enumerate(parts):
        if seg not in LEGACY_ARCHIVE_ROOTS:
            continue
        tail = parts[i + 1:]
        cands = []
        # 旧结构 `<根>/重复题/<相对路径>` 有中间层；新落点（回收站，为保面板还原能力）**去掉了该层**，
        # 故先试「剥掉中间层」，再试「原样换根名」——两者都试，命中即返回。
        if tail and tail[0] == LEGACY_DUP_SUBDIR:
            cands.append(Path(*parts[:i], ARCHIVE_ROOT, *tail[1:]))
        cands.append(Path(*parts[:i], ARCHIVE_ROOT, *tail))
        for c in cands:
            if c.exists():
                return c
    return p


def _row_cells(line: str) -> list:
    """把一行 Markdown 表格拆成去空白的单元格列表（首尾竖线不计）。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _drop_checklist_line(ck, line: str, alias_id: str = "") -> bool:
    """从 `_重复题清单.md` 删掉一条记录行；命中返回 True，未命中返回 False（由调用方告警）。

    为什么不能整行字符串替换（2026-10-31 修）：旧实现 `txt.replace(line + "\\n", "")`
    对清单的**任何**后续变动都静默失配——
      ① 清单新增第五列「本卷分值」（2026-10-30）：旧日志记的 line 仍是四列，
         与现在的「四列＋分值」行不相等（实测全库 `.dedupe/*.json` 119 条，80 条因此失配）；
      ② Obsidian 打开表格会按列宽重排空格，行内空白与记录时不同；
      ③ 行尾 CRLF／LF 混用、末行无换行时，`line + "\\n"` 同样不成立。
    现改为**按单元格前缀比较**（目标行 cells 是文件行 cells 的前缀即算同一行，多出的分值列
    不影响），再退化为按「本卷 id」（第 2 列）定位；写回保持原文件换行风格与 BOM。
    """
    p = Path(ck)
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    rows = text.splitlines()
    want = _row_cells(line)
    idx = None
    if want:
        for i, row in enumerate(rows):
            got = _row_cells(row)
            if got and len(got) >= len(want) and got[:len(want)] == want:
                idx = i
                break
    if idx is None and alias_id:
        for i, row in enumerate(rows):
            got = _row_cells(row)
            if len(got) >= 2 and got[1] == alias_id:
                idx = i
                break
    if idx is None:
        return False
    del rows[idx]
    text = newline.join(rows) + (newline if text.endswith(("\n", "\r")) else "")
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return True


def cmd_rollback(args) -> None:
    log = json.loads(Path(args.rollback).read_text(encoding="utf-8"))
    entries = log.get("entries", [])
    print(f"═══ 回滚预览（{len(entries)} 条合并记录，"
          f"{'执行' if args.apply else 'dry-run'}）═══")
    for e in entries:
        t = Path(e["theme_file"])
        print(f"主题 {t}：恢复 frontmatter 快照；移除正文追加引用 {e.get('body_appended_refs')}")
        for a in e["aliases"]:
            print(f"  归档还原：{resolve_archived(a['archived_to'])} → {a['file']}")
            if a.get("checklist_file"):
                print(f"  清单删行：{a['checklist_file']} :: {a.get('checklist_line')}")
    if not args.apply:
        print("\n[dry-run] 加 --apply 执行回滚。")
        return
    for e in entries:
        t = Path(e["theme_file"])
        _, _, body = load_md(t)
        for ref in e.get("body_appended_refs", []):
            body = body.replace(ref, "").replace("\n\n\n", "\n\n")
        t.write_text(f"---\n{e['theme_frontmatter_before']}\n---\n\n{body.strip()}\n", encoding="utf-8")
        for a in e["aliases"]:
            src, dst = resolve_archived(a["archived_to"]), Path(a["file"])
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                print(f"[还原] {dst}")
            ck, line = a.get("checklist_file"), a.get("checklist_line")
            if ck and Path(ck).exists() and line:
                if _drop_checklist_line(ck, line, a.get("id", "")):
                    print(f"[清单] 已删记录行：{ck}")
                else:
                    print(f"[警告] 清单未找到对应行，需人工核对：{ck}\n        期望行：{line}")
    print("✅ 回滚完成；请重跑 scan_questions.py 复核")


def main():
    bootstrap()
    ap = argparse.ArgumentParser(description="历史题库查重与一题多卷合并")
    ap.add_argument("--scan", action="store_true", help="全库查重，输出候选组")
    ap.add_argument("--check", help="录入前预检：传入待入库 JSON（数组）")
    ap.add_argument("--merge", help="合并主题题文件（md 路径）")
    ap.add_argument("--with", dest="with_", help="别名题文件（逗号分隔）")
    ap.add_argument("--list-merged", action="store_true", help="列出一题多卷题")
    ap.add_argument("--rollback", help="按日志 JSON 回滚")
    ap.add_argument("--check-authority", action="store_true",
                    help="全库体检：已合并题中别名比主题更权威的倒挂")
    ap.add_argument("--allow-inferior", action="store_true",
                    help="放行权威序倒挂的合并（默认拒绝）")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--out", help="查重清单输出路径（md）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--阈值", type=float, default=None, help="相似度阈值覆盖（默认选择题0.85/其他0.72）")
    ap.add_argument("--卷", help="限定某卷")
    ap.add_argument("--top", type=int, default=3, help="--check 返回的疑似条数（默认3）")
    args = ap.parse_args()

    if args.scan:
        cmd_scan(args)
    elif args.check:
        cmd_check(args)
    elif args.merge:
        if not args.with_:
            sys.exit("[失败] --merge 需搭配 --with <别名.md>[,...]")
        cmd_merge(args)
    elif args.list_merged:
        cmd_list_merged(args)
    elif args.rollback:
        cmd_rollback(args)
    elif args.check_authority:
        cmd_check_authority(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
