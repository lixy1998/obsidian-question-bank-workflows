#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组卷使用登记 —— 把「本校考试/练习卷」登记为题库中的一张卷。

思路（2026-10-24 用户裁定）
--------------------------
本校卷**不另存题目**，而是沿用「一题多卷」既有语义：
  · 卷面每道题的 `id` 追加一项 `{卷名}-{卷面题号}`（首项仍是主卷，永不动）
  · `来源` 成对追加 `(卷名, 类型标签)`（与 dedupe --merge 同一约定）
  · 在 `{vault}/{学段}-3阶段测试/{卷名}/` 建卷目录，只放 `_组卷清单.md`
    （题目表 + wikilink，与 `_重复题清单.md` 同形；`_` 前缀故不会被当题目）

于是三个方向都通：
  · 题目 → 被哪次考试用过：看 `id` 多值 / `来源`（也可在 Obsidian 反链面板看）
  · 某次考试 → 用了哪些题：看卷目录下 `_组卷清单.md`
  · 出卷去重：`--已用 <学年>` 直接排出已用题目

用法
----
  python record_usage.py --清单 组卷清单.yaml --卷 2026-2027-7上月考1 \
                         [--日期 2026-10-11] [--范围 "7上第1-2单元"] \
                         [--满分 100] [--时长 60] [--学段 初中] [--仅id] [--apply]
  python record_usage.py --卷 2026-2027-7上月考1 --查题目 [--json]
  python record_usage.py --卷 2026-2027-7上月考1 --撤销 [--apply]
  python record_usage.py --已用 2026-2027 [--json]
  python record_usage.py --体检 [--json]

铁律
----
  · 默认 dry-run，`--apply` 才写盘；写盘前逐文件备份到系统临时目录
  · 卷名必须是 `{学年}-{卷名}`，且卷名内须含类型词（月考/期中/期末/课堂练习…）
  · `id` 只追加、绝不动首项（首项＝主卷＝题目文件所在卷）
  · `来源` 必须成对；原值不成对时该题跳过并告警，**不猜**
  · 幂等：重复跑不重复追加；档案已存在时保留原文件（`--覆盖` 才重写）
  · 只写 `id`/`来源` 两行与卷目录文件，正文与其余字段一字不动
  · 行尾风格原样保留（库内 md 混有 CRLF 与 LF，2026-10-24 实测教训）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

from qb_bootstrap import bootstrap, require_vault
from qb_schema import (ARCHIVE_ROOT, as_list, is_question_file, source_pairs,
                       split_frontmatter, stage_roots)
from paperkit.manifest import load_manifest, norm_items
from paperkit.paths import build_index
from edit_fields import insert_field, parse_field, render

# ---------------- 常量 ----------------

CHECKLIST = "_组卷清单.md"
LOG_DIR = ".usage"
VOL_RE = re.compile(r"^(\d{4})-(\d{4})-(.+)$")

# 卷名类型词 → 来源类型标签（受控值，见 qb_schema.SOURCE_TAG_HINTS）
TAG_RULES = [("课堂练习", "练习卷"), ("练习", "练习卷"), ("单元测", "单元卷"),
             ("周测", "周测卷"), ("月考", "月考卷"), ("期中", "期中卷"),
             ("期末", "期末卷"), ("专题", "专题卷"), ("模拟", "模拟卷")]

STAGE_DIR = {"初中": "初中-3阶段测试", "高中": "高中-3阶段测试"}
GAOZHONG_HINT = re.compile(r"纲要|选必|选修")

FM_RE = re.compile(r"^(---\r?\n)(.*?)(\r?\n---\r?\n?)", re.S)


# ---------------- 卷名与路径 ----------------

def parse_vol(name: str) -> str:
    """校验卷名并返回来源类型标签；不合法则 SystemExit（参数级报错）。"""
    if not name:
        raise SystemExit("[失败] 请给 --卷 <卷名>，格式 {学年}-{卷名}，"
                         "如 2026-2027-7上月考1")
    m = VOL_RE.match(name)
    if not m:
        raise SystemExit(f"[失败] 卷名「{name}」不合格式：须为 {{学年}}-{{卷名}}，"
                         f"学年写两个四位年份（如 2026-2027）")
    tag = next((t for k, t in TAG_RULES if k in name), None)
    if tag is None:
        raise SystemExit(
            f"[失败] 卷名「{name}」里没有可识别的卷类型词，无法确定来源标签。\n"
            f"       可用类型词：{'、'.join(k for k, _ in TAG_RULES)}\n"
            f"       例：2026-2027-7上月考1 / 2026-2027-9上期中 / 2026-2027-7上课堂练习3")
    return tag


def resolve_stage(vol: str, stage: str | None) -> str:
    if stage:
        return stage
    return "高中" if GAOZHONG_HINT.search(vol) else "初中"


def vol_dir(vol: str, stage: str) -> Path:
    return require_vault() / STAGE_DIR[stage] / vol


# ---------------- md 读写（保正文、保行尾、保 BOM）----------------

def read_md(p: Path) -> tuple:
    """→ (原文(已 LF 化), 是否原本 CRLF, 是否带 BOM)。"""
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), crlf, bom


def write_md(p: Path, text: str, crlf: bool, bom: bool) -> None:
    out = text.replace("\n", "\r\n") if crlf else text
    data = out.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    p.write_bytes(data)


def _set_field(fm: str, key: str, vals: list) -> str:
    """把 frontmatter 里的 key 设为 vals（空列表＝删除该字段）；保持原写法风格。"""
    style, _old, span = parse_field(fm, key)
    if span is None:
        return insert_field(fm, key, vals) if vals else fm
    if not vals:
        end = span[1]
        while end < len(fm) and fm[end] in " \t":
            end += 1
        if end < len(fm) and fm[end] == "\n":
            end += 1
        return fm[:span[0]] + fm[end:]
    use = "inline" if style == "scalar" else style
    return fm[:span[0]] + render(key, use, vals) + fm[span[1]:]


def rewrite_fields(p: Path, changes: dict) -> bool:
    """按 {字段: 新值列表} 重写题目文件的指定字段，正文与其余字段不动。"""
    text, crlf, bom = read_md(p)
    m = FM_RE.match(text)
    if not m:
        raise SystemExit(f"[失败] {p} 没有 frontmatter，不能登记")
    fm = m.group(2)
    new_fm = fm
    for key, vals in changes.items():
        new_fm = _set_field(new_fm, key, vals)
    if new_fm == fm:
        return False
    write_md(p, m.group(1) + new_fm + m.group(3) + text[m.end():], crlf, bom)
    return True


def fm_of(p: Path) -> dict:
    text, _crlf, _bom = read_md(p)
    fm, _body, err = split_frontmatter(text)
    if err:
        raise SystemExit(f"[失败] {p} frontmatter 解析失败：{err}")
    return fm


# ---------------- 扫描题库 ----------------

def scan_all() -> list:
    """全库题目快照：[{path, fm, ids, primary, vol}]。"""
    out = []
    vault = require_vault()
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            fm = fm_of(p)
            ids = [str(x).strip() for x in as_list(fm.get("id"))]
            if not ids:
                continue
            rel = p.relative_to(vault).parts
            out.append({"path": p, "fm": fm, "ids": ids, "primary": ids[0],
                        "vol": rel[1] if len(rel) > 1 else p.parent.name})
    return out


# ---------------- 主体：登记 ----------------

def build_rows(items: list, index: dict) -> tuple:
    """清单条目 → 行记录；返回 (rows, miss)。

    卷面题号：清单项自带 `序号`/`题号` 时以它为准（成品卷反查后常有未匹配题，
    按顺序编号会把后面全顶错）；否则按清单顺序两位补零。
    """
    rows, miss = [], []
    for i, it in enumerate(items, 1):
        qid = str(it.get("id") or "").strip()
        p = index.get(qid)
        if p is None:
            miss.append((i, qid))
            continue
        raw_no = it.get("序号", it.get("题号"))
        if raw_no in (None, ""):
            no = f"{i:02d}"
        else:
            s = str(raw_no).strip()
            no = s.zfill(2) if s.isdigit() else s
        fm = fm_of(p)
        ids = [str(x).strip() for x in as_list(fm.get("id"))]
        rows.append({"no": no, "path": p, "fm": fm, "ids": ids,
                     "primary": ids[0] if ids else qid, "清单id": qid,
                     "分值": it.get("分值") or fm.get("分值")})
    return rows, miss


def plan_rows(rows: list, vol: str, tag: str, only_id: bool) -> list:
    """逐题算出要写的 id/来源 新值；不合规的记入 skip 原因。"""
    plan = []
    for r in rows:
        new_id = f"{vol}-{r['no']}"
        acts = {}
        if new_id in r["ids"]:
            acts["id"] = "已含"
        else:
            acts["id"] = "追加"
            acts["_id"] = r["ids"] + [new_id]

        src = [str(x) for x in as_list(r["fm"].get("来源"))]
        if only_id:
            acts["来源"] = "跳过（--仅id）"
        elif vol in src:
            acts["来源"] = "已含"
        elif len(src) % 2 == 1:
            acts["来源"] = "跳过（原来源不成对）"
            if acts["id"] == "追加":   # 已在册的题不重复告警，避免幂等复跑噪音
                acts["_warn"] = (f"第{r['no']}题 {r['primary']}：来源 {src} 项数为奇数，"
                                 f"按规矩不猜类型，请先补齐类型标签")
        elif not src:
            acts["来源"] = "追加"
            acts["_src"] = [vol, tag]
        else:
            acts["来源"] = "追加"
            acts["_src"] = src + [vol, tag]
        r.update(acts)
        plan.append(r)
    return plan


def checklist_text(vol: str, rows: list, meta: dict, vault: Path) -> str:
    """生成 _组卷清单.md（题目表 + wikilink，表格内管道符转义）。"""
    def cell(v):
        return str(v if v not in (None, "") else "—").replace("|", r"\|")

    lines = ["---", "类型: 组卷清单", f"卷名: {vol}"]
    if meta.get("标题"):
        lines.append(f"标题: {meta['标题']}")
    for k in ("考试日期", "范围", "满分", "时长"):
        if meta.get(k):
            lines.append(f"{k}: {meta[k]}")
    lines.append(f"题量: {len(rows)}")
    lines.append("题目: [" + ", ".join(r["primary"] for r in rows) + "]")
    lines += ["---", "", f"# 组卷清单 · {vol}", "",
              f"> 本校「{vol}」的组卷记录。本卷**不重复存题**——题目存于其原始卷目录，"
              f"本表按 `id` 别名反查。",
              f"> 题目侧的 `id` 与 `来源` 已追加本卷（`{vol}-<卷面题号>`），"
              f"故打开任意题目可在 `id` 多值中看到本卷。", ""]
    if meta.get("标题"):
        lines.append(f"- 卷面标题：{meta['标题']}")
    for k in ("考试日期", "范围", "满分", "时长"):
        if meta.get(k):
            lines.append(f"- {k}：{meta[k]}")
    if meta.get("清单"):
        lines.append(f"- 组卷清单源文件：`{meta['清单']}`")
    nums = sorted(int(r["no"]) for r in rows if str(r["no"]).isdigit())
    gaps = [n for n in range(1, max(nums) + 1) if n not in nums] if nums else []
    if gaps:
        lines.append(f"- 题库外题目：第 {'、'.join(map(str, gaps))} 题"
                     f"（卷面有、未匹配到题库，未登记；如需统计请先录入）")
    lines += ["", "| 卷面题号 | 题目 | 题型 | 分值 | 主考课 | 难度 | 来源类型 |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        fm = r["fm"]
        rel = r["path"].relative_to(vault).with_suffix("").as_posix()
        link = f"[[{rel}\\|{r['primary']}]]"
        kec = as_list(fm.get("课"))
        pairs = source_pairs(fm)
        typ = pairs[0][1] if pairs else ""
        lines.append(f"| {cell(r['no'])} | {link} | {cell(fm.get('题型'))} | "
                     f"{cell(r['分值'])} | {cell(kec[0] if kec else '')} | "
                     f"{cell(fm.get('难度'))} | {cell(typ)} |")
    return "\n".join(lines).rstrip() + "\n"


def log_write(payload: dict) -> Path:
    d = require_vault() / LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date.today().isoformat()}.json"
    old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    old.append(payload)
    p.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def cmd_record(args) -> int:
    vol = args.卷
    tag = parse_vol(vol)
    stage = resolve_stage(vol, args.学段)
    vd = vol_dir(vol, stage)

    man = load_manifest(Path(args.清单))
    items = norm_items(man)
    if not items:
        raise SystemExit("[失败] 清单里没有任何题目")

    index = build_index(verbose=False)
    rows, miss = build_rows(items, index)
    if not rows:
        raise SystemExit("[失败] 全部条目均未定位到题目")
    if miss:
        print(f"[警告] {len(miss)} 条未定位，已跳过：")
        for i, qid in miss:
            print(f"   第{i}条 {qid}")

    plan = plan_rows(rows, vol, tag, args.仅id)

    print(f"[卷] {vol}　类型标签：{tag}　学段：{stage}")
    print(f"[落点] {vd.relative_to(require_vault()).as_posix()}/{CHECKLIST}")
    print(f"[清单] {len(plan)} 题已定位")
    for r in plan:
        flag = "＋" if r["id"] == "追加" else "·"
        print(f"   {flag} {r['no']}　{r['primary']}　id:{r['id']}　来源:{r['来源']}")
        if r.get("_warn"):
            print(f"      ⚠ {r['_warn']}")

    n_id = sum(1 for r in plan if r["id"] == "追加")
    n_src = sum(1 for r in plan if r["来源"] == "追加")
    print(f"[汇总] id 追加 {n_id} 题 / 来源追加 {n_src} 题 / 未定位 {len(miss)} 题")

    ck = vd / CHECKLIST
    if ck.exists() and not args.覆盖:
        print(f"[提示] {CHECKLIST} 已存在，保留原文件（--覆盖 可重写）")

    if not args.apply:
        print("[dry-run] 未写盘。加 --apply 执行。")
        return 0

    # 备份 + 写盘
    backup = Path(tempfile.gettempdir()) / "qb-usage" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    changed = []
    vault = require_vault()
    for r in plan:
        changes = {}
        if r["id"] == "追加":
            changes["id"] = r["_id"]
        if r["来源"] == "追加":
            changes["来源"] = r["_src"]
        if not changes:
            continue
        # 备份保留相对路径结构：只存文件名会「同题号互相覆盖」（多卷都用 18.md）
        dst = backup / r["path"].relative_to(vault)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(r["path"].read_bytes())
        if rewrite_fields(r["path"], changes):
            changed.append(r["primary"])

    meta = {"标题": str(man.get("标题") or "").strip(),
            "考试日期": args.日期, "范围": args.范围, "满分": args.满分,
            "时长": args.时长, "清单": Path(args.清单).name}
    if not ck.exists() or args.覆盖:
        vd.mkdir(parents=True, exist_ok=True)
        ck.write_text(checklist_text(vol, plan, meta, require_vault()), encoding="utf-8")
        print(f"[档案] 写入 {ck.relative_to(require_vault()).as_posix()}")
    else:
        print(f"[档案] 保留已存在的 {CHECKLIST}")

    log = log_write({"时间": datetime.now().isoformat(timespec="seconds"),
                     "动作": "登记", "卷名": vol, "类型标签": tag,
                     "清单": args.清单, "改写题数": len(changed),
                     "题目": changed, "备份": str(backup)})
    print(f"[写盘] 改写 {len(changed)} 个题目文件；备份 {backup}")
    print(f"[日志] {log.relative_to(require_vault()).as_posix()}")
    return 0


# ---------------- 查 / 撤 / 已用 / 体检 ----------------

def _no_of(q: dict, vol: str) -> str:
    """题目在指定卷里的卷面题号（取 id 里以 vol- 开头的项）。"""
    for x in q["ids"]:
        if x.startswith(vol + "-"):
            return x[len(vol) + 1:]
    return ""


def cmd_query(args) -> int:
    vol = args.卷
    parse_vol(vol)
    hits = [q for q in scan_all() if any(x.startswith(vol + "-") for x in q["ids"])]
    if args.json:
        print(json.dumps([{"主卷": q["primary"], "卷面题号": _no_of(q, vol),
                           "题型": q["fm"].get("题型"), "分值": q["fm"].get("分值"),
                           "文件": str(q["path"].relative_to(require_vault()))}
                          for q in hits], ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print(f"[空] 题库里没有任何题目登记到「{vol}」")
        return 0
    print(f"[卷] {vol}　收录 {len(hits)} 题\n")
    print("| 卷面题号 | 题目 | 题型 | 分值 |")
    print("|---|---|---|---|")
    for q in sorted(hits, key=lambda r: _no_of(r, vol)):
        print(f"| {_no_of(q, vol)} | {q['primary']} | {q['fm'].get('题型')} | "
              f"{q['fm'].get('分值')} |")
    return 0


def cmd_revoke(args) -> int:
    vol = args.卷
    parse_vol(vol)
    vault = require_vault()
    targets, primary_owner = [], []
    for q in scan_all():
        if q["primary"].startswith(vol + "-"):
            primary_owner.append(q["primary"])
            continue
        if any(x.startswith(vol + "-") for x in q["ids"][1:]):
            targets.append(q)

    if primary_owner:
        print(f"[阻断] {len(primary_owner)} 题的**主卷**就是本卷（题目文件在本卷目录内）：")
        for x in primary_owner:
            print(f"   {x}")
        print("       撤销会删掉主卷 id —— 须先迁移或删除这些题目文件，再重跑撤销。")
        return 1

    print(f"[撤销] {vol}：命中 {len(targets)} 题")
    if not targets:
        print("       题库中无该卷登记，无需撤销。")
        return 0
    for q in targets:
        print(f"   · {q['primary']}")

    if not args.apply:
        print("[dry-run] 未写盘。加 --apply 执行。")
        return 0

    backup = Path(tempfile.gettempdir()) / "qb-usage" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    changed = []
    for q in targets:
        changes = {}
        new_ids = [q["ids"][0]] + [x for x in q["ids"][1:] if not x.startswith(vol + "-")]
        if new_ids != q["ids"]:
            changes["id"] = new_ids
        src = [str(x) for x in as_list(q["fm"].get("来源"))]
        if vol in src:
            i = src.index(vol)
            drop = {i, i + 1} if i % 2 == 0 else {i}
            changes["来源"] = [x for j, x in enumerate(src) if j not in drop]
        if not changes:
            continue
        (backup / q["path"].relative_to(vault)).parent.mkdir(parents=True, exist_ok=True)
        (backup / q["path"].relative_to(vault)).write_bytes(q["path"].read_bytes())
        if rewrite_fields(q["path"], changes):
            changed.append(q["primary"])

    for stage in ("初中", "高中"):
        c = vault / STAGE_DIR[stage] / vol / CHECKLIST
        if c.exists():
            trash = vault / ARCHIVE_ROOT / c.relative_to(vault)
            trash.parent.mkdir(parents=True, exist_ok=True)
            c.replace(trash)
            print(f"[档案] {CHECKLIST} 移入库内回收站：{trash.relative_to(vault).as_posix()}")
    # 撤销后不留空壳目录
    for stage in ("初中", "高中"):
        d = vault / STAGE_DIR[stage] / vol
        if d.is_dir() and not any(p.is_file() for p in d.rglob("*")):
            shutil.rmtree(d)
            print(f"[清理] 已移除空卷目录 {d.relative_to(vault).as_posix()}")
    log_write({"时间": datetime.now().isoformat(timespec="seconds"),
               "动作": "撤销", "卷名": vol, "改写题数": len(changed),
               "题目": changed, "备份": str(backup)})
    print(f"[写盘] 改写 {len(changed)} 个题目文件；备份 {backup}")
    return 0


def cmd_used(args) -> int:
    prefix = args.已用
    if not re.match(r"^\d{4}-\d{4}$", prefix):
        raise SystemExit("[失败] --已用 须给学年前缀，如 2026-2027")
    rows = []
    for q in scan_all():
        vols = sorted({x.rsplit("-", 1)[0] for x in q["ids"][1:]
                       if VOL_RE.match(x.rsplit("-", 1)[0])
                       and x.startswith(prefix + "-")})
        if vols:
            rows.append({"主卷": q["primary"], "被用于": vols,
                         "文件": str(q["path"].relative_to(require_vault()))})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"[空] {prefix} 学年尚无已用题目")
        return 0
    print(f"[已用] {prefix} 学年共 {len(rows)} 题被本校卷选用\n")
    for r in rows:
        print(f"   {r['主卷']}　← {'、'.join(r['被用于'])}")
    print("\n提示：组卷时排除这些主卷 id 即可避免与已考卷重复。")
    return 0


def cmd_check(args) -> int:
    vault = require_vault()
    problems = []
    allq = scan_all()
    dirs = {}
    for stage in ("初中", "高中"):
        base = vault / STAGE_DIR[stage]
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and VOL_RE.match(d.name):
                dirs[d.name] = d

    for name, d in dirs.items():
        ck = d / CHECKLIST
        if not ck.exists():
            problems.append(f"[缺档案] {name}：卷目录存在但无 {CHECKLIST}")
            continue
        no_files = [p for p in d.rglob("*.md") if is_question_file(p)]
        if no_files:
            problems.append(f"[混装] {name}：卷目录内有 {len(no_files)} 个题目文件"
                            f"（本卷应只放 {CHECKLIST}，除非本卷也是某些题的主卷）")
        # 档案题目表 ↔ 题目侧 id 双向比对
        listed = [str(x) for x in as_list(fm_of(ck).get("题目"))]
        actual = [q["primary"] for q in allq
                  if any(x.startswith(name + "-") for x in q["ids"])]
        lost = [x for x in listed if x not in actual]
        extra = [x for x in actual if x not in listed]
        if lost:
            problems.append(f"[档案多记] {name}：{', '.join(lost)} 在题目侧已无本卷 id")
        if extra:
            problems.append(f"[档案漏记] {name}：{', '.join(extra)} 有本卷 id 但不在档案题目表")

    # 反向：题目 id 里出现本校卷名，但卷目录/档案缺失
    for q in allq:
        for x in q["ids"][1:]:
            v = x.rsplit("-", 1)[0]
            if VOL_RE.match(v) and v not in dirs:
                problems.append(f"[无卷目录] {q['primary']} 的 id 含 {v}，"
                                f"但 {{初中,高中}}-3阶段测试/{v}/ 均不存在")
                break

    if args.json:
        print(json.dumps({"问题": problems, "卷数": len(dirs)}, ensure_ascii=False, indent=2))
        return 1 if problems else 0
    print(f"[体检] 本校卷目录 {len(dirs)} 个")
    if not problems:
        print("✅ 未发现问题")
        return 0
    print(f"⚠ {len(problems)} 处：")
    for x in problems:
        print("   " + x)
    return 1


# ---------------- CLI ----------------

def main() -> int:
    bootstrap(("yaml",))
    ap = argparse.ArgumentParser(
        description="组卷使用登记：把本校考试/练习卷登记为题库中的一张卷"
                    "（题目 id 追加卷名、卷目录放 _组卷清单.md）")
    ap.add_argument("--清单", help="组卷清单（.yaml/.json/.txt，与 export_student_paper.py 同格式）")
    ap.add_argument("--卷", help="卷名，格式 {学年}-{卷名}，如 2026-2027-7上月考1")
    ap.add_argument("--学段", choices=["初中", "高中"], help="默认按卷名推断（纲要/选必/选修→高中）")
    ap.add_argument("--日期", help="考试日期，如 2026-10-11")
    ap.add_argument("--范围", help="考查范围，如「7上第1-2单元」")
    ap.add_argument("--满分", help="卷面满分")
    ap.add_argument("--时长", help="建议用时（分钟）")
    ap.add_argument("--仅id", action="store_true", help="只追加 id，不改「来源」")
    ap.add_argument("--覆盖", action="store_true", help="重写已存在的 _组卷清单.md")
    ap.add_argument("--查题目", action="store_true", help="列出本卷收录的全部题目")
    ap.add_argument("--撤销", action="store_true", help="从全库 id/来源 中移除本卷登记")
    ap.add_argument("--已用", metavar="学年", help="列出该学年被本校卷选用的题目（出卷去重）")
    ap.add_argument("--体检", action="store_true", help="卷目录 ↔ 题目 id 一致性检查")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--apply", action="store_true", help="执行写盘（默认 dry-run）")
    args = ap.parse_args()

    # ⚠ 参数校验必须先于 require_vault（否则未配置 vault 时参数报错会被 exit=2 盖掉）
    if args.已用:
        return cmd_used(args)
    if args.体检:
        return cmd_check(args)
    if not args.卷:
        raise SystemExit("[失败] 请给 --卷 <卷名>，或改用 --已用/--体检")
    parse_vol(args.卷)
    if args.查题目:
        return cmd_query(args)
    if args.撤销:
        return cmd_revoke(args)
    if not args.清单:
        raise SystemExit("[失败] 登记须给 --清单 <组卷清单文件>")
    return cmd_record(args)


if __name__ == "__main__":
    sys.exit(main())
