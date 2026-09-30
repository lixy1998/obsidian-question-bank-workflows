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

合并规则（用户 2026-09-13 裁定）：
  · 主题 = 权威序最高者：中/高考真题 > 中/高考模拟 > 阶段测试；同级按**年份最早**者
  · 主题 id 列表追加别名 id（幂等）；来源**成对追加**（卷名, 类型）
  · 册别/单元/课/小节/知识点/核心素养/素养层次水平/关键能力 取并集（主题在前）
  · 年份/考区/难度/分值 保留主题值，差异写入 YAML「解析」备注（正文保持纯净，学生卷安全）
  · 别名题移入 {vault}/.trash/<原相对路径>（库内 Obsidian 回收站，可在面板还原到原位）；别名卷目录追加 `_重复题清单.md`
  · 日志写入 {vault}/.dedupe/YYYY-MM-DD.json（含主题 frontmatter 快照，可 --rollback）
"""
import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha1
from pathlib import Path

from qb_bootstrap import bootstrap, require_vault
from qb_schema import (ARCHIVE_ROOT, DEDUPE_LOG_DIR,
                       LEGACY_ARCHIVE_ROOTS, _yaml_scalar, as_list,
                       authority_key, dump_yaml, is_question_file,
                       norm_for_dedupe, source_pairs, split_frontmatter,
                       stage_roots, strip_comments)

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
        members.sort(key=lambda it: authority_key({**it["fm"], "卷名": it["fm"].get("卷名") or it["rel"].parts[1],
                                                   "年份": it["fm"].get("年份")},
                                                  it["path"]))
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


def cmd_merge(args) -> None:
    theme_path = Path(args.merge)
    alias_paths = [Path(s.strip()) for s in (args.with_ or "").split(",") if s.strip()]
    if not theme_path.exists():
        sys.exit(f"[失败] 主题文件不存在：{theme_path}")
    for ap in alias_paths:
        if not ap.exists():
            sys.exit(f"[失败] 别名文件不存在：{ap}")
    theme_fm_raw, theme_fm, theme_body = load_md(theme_path)
    theme_ids = [str(x) for x in as_list(theme_fm.get("id"))]
    seq = theme_path.stem
    new_ids, new_src, union_new, notes, img_refs = (list(theme_ids),
                                                    list(as_list(theme_fm.get("来源"))), {}, [], [])
    used_img_names = set()  # 批内已分配的新图名（避免同一批多条别名撞名）
    entries = []
    for ap in alias_paths:
        a_fm_raw, a_fm, a_body = load_md(ap)
        a_ids = [str(x) for x in as_list(a_fm.get("id"))]
        if not a_ids:
            sys.exit(f"[失败] 别名题缺 id：{ap}")
        a_primary = a_ids[0]
        # 权威序校验：主题应不劣于别名
        tk = authority_key({**theme_fm, "序号": seq}, theme_path)
        ak = authority_key({**a_fm, "序号": ap.stem}, ap)
        if ak < tk:
            print(f"[警告] {ap.name} 的权威序高于主题（{ak} < {tk}），"
                  f"建议改用该文件作主题（可用 --scan 的建议主题）")
        if a_primary in theme_ids:
            print(f"[提示] 别名 id 已在主题 id 列表中：{a_primary}（幂等，跳过 YAML 变更）")
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
        # 图片：从别名正文取引用 → 复制到主题 images/ 并重命名
        a_imgs = IMG_RE.findall(a_body)
        local_refs = []
        for name in a_imgs:
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
                        "yaml_note": f"> 【一题多卷】另见《{a_primary}》（差异：{note}）"})

    # 主题新 frontmatter
    fm_new = theme_fm_raw
    fm_new = replace_field(fm_new, "id", new_ids)
    if len(new_src) != len(as_list(theme_fm.get("来源"))):
        fm_new = replace_field(fm_new, "来源", new_src)
    for f, v in union_new.items():
        fm_new = replace_field(fm_new, f, v)
    old_jx = str(theme_fm.get("解析") or "").strip()
    new_jx = "\n".join(([old_jx] if old_jx else []) + [e["yaml_note"] for e in entries])
    fm_new = replace_field(fm_new, "解析", new_jx)

    print(f"═══ 合并预览（{'执行' if args.apply else 'dry-run，未写盘'}）═══")
    print(f"主题：{theme_path}")
    print(f"  id：{theme_ids} → {new_ids}")
    print(f"  来源：{as_list(theme_fm.get('来源'))} → {new_src}")
    for f, v in union_new.items():
        print(f"  {f}：追加 {[x for x in v if x not in as_list(theme_fm.get(f))]}")
    print(f"  解析：追加 {len(entries)} 行「一题多卷」备注：")
    for e in entries:
        print(f"    {e['yaml_note']}")
    print(f"  正文：追加图片引用 {img_refs if img_refs else '（无）'}")
    for e in entries:
        print(f"别名：{e['file']}")
        print(f"  → 归档至 {e['archived_to']}")
    if not args.apply:
        print("\n[dry-run] 加 --apply 执行；执行前请确认权威序与差异说明无误。")
        return

    # 写主题 + 立即自检 frontmatter（防静默破坏；此时尚未归档，修正后可直接重试）
    theme_path.write_text(f"---\n{fm_new}\n---\n\n"
                          f"{append_body_images(theme_body, img_refs).strip()}\n", encoding="utf-8")
    chk_fm, _, chk_err = split_frontmatter(theme_path.read_text(encoding="utf-8"))
    if chk_err or not chk_fm.get("id"):
        print(f"❌ 主题 frontmatter 校验失败：{chk_err or '缺少 id 字段'}\n"
              f"   主题 YAML 已改动、别名尚未归档；请手工修正该文件后重试", file=sys.stderr)
        sys.exit(1)
    print(f"[自检] 主题 frontmatter 解析正常：id={chk_fm.get('id')}")
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
    for e in entries:
        src_p = Path(e["file"])
        dst_p = Path(e["archived_to"])
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        # 同题号前缀的图片一并复制到归档目录（保留原图，供还原原卷）
        img_dir = src_p.parent / "images"
        if img_dir.exists():
            arch_img = dst_p.parent / "images"
            for f in img_dir.glob(f"{src_p.stem}_*"):
                arch_img.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, arch_img / f.name)
        print(f"[归档] {src_p.name} → {dst_p}")
        # 卷内清单
        vol_dir = src_p.parent.parent
        ck = vol_dir / "_重复题清单.md"
        line = f"| {src_p.stem} | {e['id']} | {new_ids[0]} | {today} |"
        if not ck.exists():
            ck.write_text(f"# 重复题清单 · {vol_dir.name}\n\n"
                          f"> 本卷以下题目与题库内权威版本重复，按「一题多卷」规则归档（位置 "
                          f"`{ARCHIVE_ROOT}/{dst_p.parent.relative_to(require_vault() / ARCHIVE_ROOT).as_posix()}`，"
                          f"即库内 Obsidian 回收站，可在回收站面板还原到原位）。\n"
                          f"> 题库不重复存题；需要还原本卷时按「主题 id」取题。\n\n"
                          f"| 本卷题号 | 本卷 id | 主题 id | 合并日期 |\n|---|---|---|---|\n",
                          encoding="utf-8")
        with ck.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        e["checklist_file"] = str(ck)
        e["checklist_line"] = line
    log["entries"].append({"theme_file": str(theme_path), "theme_id": theme_ids[0] if theme_ids else "",
                           "theme_frontmatter_before": theme_fm_raw,
                           "body_appended_refs": img_refs, "aliases": entries})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[日志] {log_path}（--rollback 可回滚）")
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
                txt = Path(ck).read_text(encoding="utf-8").replace(line + "\n", "")
                Path(ck).write_text(txt, encoding="utf-8")
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
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
