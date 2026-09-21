#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""笔记引用体检 —— 检测 / 修复研究笔记里指向题库题目的 wikilink 断链。

为什么需要（2026-09-21 定案）
----------------------------
题目被「一题多卷」合并后，别名题文件移入 `{vault}/_归档/重复题/`，它在原卷目录
下的路径随之失效。此前引用过该题的 `3-试题研究/` 报告会**静默断链**：

  · Obsidian 不报错，只把链接显示成「未创建的笔记」，纯靠肉眼很难发现；
  · 更糟的是题库里存在大量同名文件（`25.md` 全库十余个），Obsidian 的模糊匹配
    可能把链接带到**另一道无关题**上 —— 看起来还能点，实际指错了。

本脚本按**方案一**处置：合并后把失效引用改指**主卷**（权威版本），与「一题多卷」
的合并意图一致 —— 旧题的「身份」并未消失，它作为 id 被吸收进主卷的 id 多值数组，
所以正确做法不是把引用引到归档区，而是改指吸收它的那个主卷文件。

用法
----
  python check_note_links.py                        # 巡检 3-试题研究/（只读，不改盘）
  python check_note_links.py --dir 3-试题研究 --dir 1-MoC-索引
  python check_note_links.py --file <笔记>           # 直接指定笔记（绝对或相对 vault）
  python check_note_links.py --fix                  # 只输出修复计划，仍不改盘
  python check_note_links.py --fix --apply          # 执行修复（写日志，可回滚）
  python check_note_links.py --cite <题目id>         # 合并前预检：谁引用了这道题
  python check_note_links.py --rollback <日志.json>  # 按日志回滚一次修复

退出码：0 = 无问题 / 仅提示；1 = 存在断链（`--apply` 修复成功后仍返回 1，
方便在流水线中察觉「本次修过东西」）；2 = 用法或环境错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qb_schema import TYPES, VAULT, is_question_file, split_frontmatter  # noqa: E402

ARCHIVE_DIR = "_归档"
NOTE_DIR_DEFAULT = "3-试题研究"
LOG_DIR = ".linkfix"

LINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
VOL_SUFFIX_RE = re.compile(r"_(" + "|".join(map(re.escape, TYPES)) + r")$")


# ---------------------------------------------------------------- 基础工具
def split_link(inner: str):
    """拆 wikilink 内部串 → (path, alias, sep)。

    表格单元格内须写作 `[[path\\|alias]]`（管道符转义，否则被表格吃掉），
    正文写 `[[path|alias]]`。`sep` 原样返回（'' / '|' / '\\\\|'），写回时照原风格。
    """
    m = re.search(r"(\\?\|)", inner)
    if not m:
        return inner.strip(), None, ""
    return inner[: m.start()].strip(), inner[m.end():], m.group(1)


def join_link(path: str, alias, sep: str) -> str:
    if alias is None:
        return "[[%s]]" % path
    return "[[%s%s%s]]" % (path, sep or "|", alias)


def is_archived(rel: str) -> bool:
    return rel == ARCHIVE_DIR or rel.startswith(ARCHIVE_DIR + "/")


def read_text_raw(p: Path) -> str:
    """按原始换行读入（避免 universal-newlines 往返改写 LF/CRLF）。"""
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text_raw(p: Path, text: str) -> None:
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ---------------------------------------------------------------- 索引
def iter_question_files():
    """全库题目文件：在库（初中-*/高中-*）＋ 归档区（_归档/**）。"""
    vault = Path(VAULT)
    out = []
    for root in sorted(p for p in vault.iterdir() if p.is_dir() and re.match(r"^(初中|高中)-", p.name)):
        out.extend(p for p in root.rglob("*.md") if is_question_file(p))
    arch = vault / ARCHIVE_DIR
    if arch.is_dir():
        out.extend(p for p in arch.rglob("*.md") if is_question_file(p))
    return out


def build_index():
    """扫全库题目 → (id2path, rel2ids)。

    id2path: 题目 id → 相对 vault 的路径（不含 .md）。同一 id 同时存在于归档件与
             主卷时，**非归档者优先**（这正是合并后该 id 的归属），同级按路径排序取稳定者。
    rel2ids: 相对路径 → 该文件 frontmatter 的 id 列表。
    """
    hits = defaultdict(list)   # id -> [(archived, rel), ...]
    rel2ids = {}
    for p in iter_question_files():
        rel = p.relative_to(Path(VAULT)).with_suffix("").as_posix()
        fm, _body, err = split_frontmatter(p.read_text(encoding="utf-8"))
        if err:
            print("[警告] %s：%s" % (rel, err))
        ids = fm.get("id") or []
        if isinstance(ids, str):
            ids = [ids]
        ids = [str(x).strip() for x in ids if str(x).strip()]
        rel2ids[rel] = ids
        arch = is_archived(rel)
        for i in ids:
            hits[i].append((arch, rel))

    id2path = {}
    for i, lst in hits.items():
        lst.sort(key=lambda t: (t[0], len(t[1]), t[1]))   # 非归档优先 → 短路径 → 字典序
        id2path[i] = lst[0][1]
    return id2path, rel2ids, hits


def derive_old_ids(path_part: str) -> list:
    """从失效链接路径反推候选旧 id。

    链接结构：`{类别}/{卷名}/{卷名}_{题型}/{序号}`
    题目 id 结构：`{卷名}-{序号}`
    """
    parts = [x for x in path_part.split("/") if x]
    if len(parts) < 2:
        return []
    seq = parts[-1]
    cands = []
    if len(parts) >= 3:
        cands.append("%s-%s" % (parts[-3], seq))
    seg = parts[-2]
    vol = VOL_SUFFIX_RE.sub("", seg)
    if vol != seg or len(parts) == 2:
        cands.append("%s-%s" % (vol, seq))
    out = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


# ---------------------------------------------------------------- 扫描
def collect_notes(args):
    vault = Path(VAULT)
    files, seen = [], set()

    def add(p: Path):
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            files.append(p)

    if args.file:
        for f in args.file:
            p = Path(f)
            if not p.is_absolute():
                p = vault / p
            if p.is_file():
                add(p)
            else:
                print("[警告] 笔记不存在，跳过：%s" % f)
    dirs = args.dir or ([] if args.file else [NOTE_DIR_DEFAULT])
    for d in dirs:
        p = Path(d)
        if not p.is_absolute():
            p = vault / p
        if p.is_dir():
            for f in sorted(p.rglob("*.md")):
                add(f)
        else:
            print("[警告] 目录不存在，跳过：%s" % d)
    return files


def classify(path_part: str, id2path: dict, rel2ids: dict, alias_to_theme: bool):
    """判定一处链接 → (kind, 新路径, 新别名)。

    kind ∈ {OK, ARCHIVED, FIX, BROKEN}
      OK        目标存在且在库，无需处理
      ARCHIVED  目标存在但位于 _归档/，应改指主卷
      FIX       目标不存在，可由 id 索引定位到主卷
      BROKEN    目标不存在且无法定位（需人工查）
    """
    vault = Path(VAULT)
    rel = path_part[:-3] if path_part.endswith(".md") else path_part

    if (vault / (rel + ".md")).exists():
        if is_archived(rel):
            ids = rel2ids.get(rel, [])
            for i in ids:
                tgt = id2path.get(i)
                if tgt and tgt != rel and not is_archived(tgt):
                    return "ARCHIVED", tgt, (i if alias_to_theme else None)
            return "ARCHIVED", None, None
        return "OK", None, None

    for i in derive_old_ids(rel):
        tgt = id2path.get(i)
        if tgt and tgt != rel:
            return "FIX", tgt, (i if alias_to_theme else None)
    return "BROKEN", None, None


# ---------------------------------------------------------------- 主流程
def run_check(files, id2path, rel2ids, args):
    rows, stat = [], defaultdict(int)
    for f in files:
        try:
            text = read_text_raw(f)
        except Exception as e:
            print("[警告] 读取失败 %s：%s" % (f, e))
            continue
        relf = f.relative_to(Path(VAULT)).as_posix() if Path(VAULT) in f.parents else str(f)
        for ln, line in enumerate(text.splitlines(), 1):
            for m in LINK_RE.finditer(line):
                path, alias, sep = split_link(m.group(1))
                if not path:
                    continue
                kind, tgt, new_alias = classify(path, id2path, rel2ids, args.alias == "theme")
                stat[kind] += 1
                if kind == "OK":
                    continue
                new_alias = alias if new_alias is None else new_alias
                new_link = join_link(tgt, new_alias, sep) if tgt else None
                rows.append({
                    "file": relf, "line": ln, "kind": kind,
                    "old_link": m.group(0), "new_link": new_link,
                    "path": path, "alias": alias, "sep": sep, "alias_to_theme": args.alias == "theme",
                })
    return rows, stat


def render(rows, stat, nfiles):
    total = sum(stat.values())
    print("扫描笔记 %d 份 · 链接 %d 处 · 正常 %d · 归档 %d · 可自动修复 %d · 无法定位 %d"
          % (nfiles, total, stat.get("OK", 0), stat.get("ARCHIVED", 0),
             stat.get("FIX", 0), stat.get("BROKEN", 0)))
    if not rows:
        print("✓ 无断链，全部链接解析正常")
        return
    print()
    print("%-4s %-46s %s" % ("行", "文件", "情形 → 处理"))
    print("-" * 108)
    for r in rows:
        tag = {"ARCHIVED": "归档区引用", "FIX": "断链可修", "BROKEN": "断链待查"}[r["kind"]]
        print("%-4d %-46s %s" % (r["line"], r["file"][-46:], tag))
        print("     %s" % r["old_link"])
        if r["new_link"]:
            print("  → %s" % r["new_link"])
        else:
            print("  → 未能定位主卷：请在库内检索该题素材，人工确认后修改")
        print()


def apply_fix(rows, args):
    fixable = [r for r in rows if r["new_link"]]
    if not fixable:
        print("无可自动修复项，未改动任何文件")
        return 0
    byfile = defaultdict(list)
    for r in fixable:
        byfile[r["file"]].append(r)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    logdir = Path(VAULT) / LOG_DIR
    logdir.mkdir(exist_ok=True)
    logfile = logdir / ("%s.json" % ts)
    records, done = [], 0

    for relf, items in byfile.items():
        p = Path(VAULT) / relf
        text = read_text_raw(p)
        backup = text
        for r in items:
            if r["old_link"] in text:
                text = text.replace(r["old_link"], r["new_link"])
                records.append({"file": relf, "old": r["old_link"], "new": r["new_link"], "line": r["line"]})
                done += 1
            else:
                print("[警告] 未找到待替换串（文件可能已变动），跳过：%s 行 %d" % (relf, r["line"]))
        if text != backup:
            write_text_raw(p, text)

    logfile.write_text(json.dumps({
        "time": ts, "mode": "note-links", "count": done, "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✓ 已修复 %d 处，涉及 %d 份笔记" % (done, len(byfile)))
    for r in records:
        print("  %s 行 %d" % (r["file"], r["line"]))
    print("日志（可回滚）：%s" % logfile.relative_to(Path(VAULT)).as_posix())
    print("回滚命令：python check_note_links.py --rollback %s" % logfile.name)
    return done


def cmd_rollback(args):
    logdir = Path(VAULT) / LOG_DIR
    p = Path(args.rollback)
    if not p.is_absolute():
        p = logdir / args.rollback
    if not p.is_file():
        print("✗ 日志不存在：%s" % p)
        return 2
    data = json.loads(p.read_text(encoding="utf-8"))
    byfile = defaultdict(list)
    for r in data.get("records", []):
        byfile[r["file"]].append(r)
    n = 0
    for relf, items in byfile.items():
        fp = Path(VAULT) / relf
        text = read_text_raw(fp)
        for r in items:
            if r["new"] in text:
                text = text.replace(r["new"], r["old"])
                n += 1
        write_text_raw(fp, text)
    print("✓ 已回滚 %d 处（日志 %s）" % (n, p.name))
    return 0


def cmd_cite(args):
    """合并前预检：列出引用了该题（含其任一变体 id）的笔记链接。"""
    id2path, rel2ids, hits = build_index()
    target = args.cite.strip()
    want_ids = set()
    if target in hits:
        want_ids.add(target)
        for _arch, rel in hits[target]:
            for i in rel2ids.get(rel, []):
                want_ids.add(i)
    else:
        want_ids.add(target)
    want_paths = {rel for _a, rel in hits.get(target, [])}
    for i in list(want_ids):
        if i in id2path:
            want_paths.add(id2path[i])

    print("预检题目：%s" % target)
    print("涉及 id：%s" % "、".join(sorted(want_ids)))
    print("涉及文件：%s" % ("、".join(sorted(want_paths)) or "（库内未找到）"))
    print()

    files = collect_notes(args)
    n = 0
    for f in files:
        text = read_text_raw(f)
        relf = f.relative_to(Path(VAULT)).as_posix() if Path(VAULT) in f.parents else str(f)
        for ln, line in enumerate(text.splitlines(), 1):
            for m in LINK_RE.finditer(line):
                path, _alias, _sep = split_link(m.group(1))
                rel = path[:-3] if path.endswith(".md") else path
                # 命中判据三条任一条成立：
                #   ① 链接路径本身就是该题现在的所在文件；
                #   ② 链接路径反推出的 id 命中该题任一 id（覆盖「引用原卷旧位置」）；
                #   ③ 链接路径所在文件登记过该题 id（覆盖「引用归档件」与别名关系）。
                cand = set(derive_old_ids(rel)) | set(rel2ids.get(rel, []))
                if rel in want_paths or (cand & want_ids):
                    print("  %s 行 %d：%s" % (relf, ln, m.group(0)))
                    n += 1
    if n == 0:
        print("✓ 无笔记引用该题，合并不会造成引用断链")
    else:
        print("\n⚠ 共 %d 处引用。合并归档该题后，这些链接需改指主卷："
              "\n  python check_note_links.py --fix --apply" % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description="笔记引用体检：检测/修复研究笔记里的题目链接断链")
    ap.add_argument("--dir", action="append", help="扫描目录（相对 vault，可多次；默认 3-试题研究）")
    ap.add_argument("--file", action="append", help="直接指定笔记文件（绝对或相对 vault，可多次）")
    ap.add_argument("--fix", action="store_true", help="输出修复计划（仍不改盘，须加 --apply 才写盘）")
    ap.add_argument("--apply", action="store_true", help="实际执行修复（写日志，可 --rollback）")
    ap.add_argument("--alias", choices=["keep", "theme"], default="keep",
                    help="别名处理：keep=保留原别名（默认）；theme=改用主卷 id 作别名")
    ap.add_argument("--cite", metavar="题目ID", help="合并前预检：列出引用该题的笔记链接")
    ap.add_argument("--rollback", metavar="日志", help="按 .linkfix/<日志>.json 回滚")
    args = ap.parse_args()

    if args.rollback:
        sys.exit(cmd_rollback(args))

    print("vault：%s" % VAULT)
    id2path, rel2ids, _hits = build_index()
    print("题目索引：%d 个 id / %d 个文件" % (len(id2path), len(rel2ids)))
    print()

    if args.cite:
        sys.exit(cmd_cite(args))

    files = collect_notes(args)
    if not files:
        print("✗ 未找到任何笔记文件")
        sys.exit(2)

    rows, stat = run_check(files, id2path, rel2ids, args)
    render(rows, stat, len(files))

    if args.apply:
        if not args.fix:
            print("✗ --apply 须与 --fix 同用")
            sys.exit(2)
        apply_fix(rows, args)
    elif args.fix:
        print("（--fix 计划模式：未改动任何文件；加 --apply 执行）")

    sys.exit(1 if rows else 0)


if __name__ == "__main__":
    main()
