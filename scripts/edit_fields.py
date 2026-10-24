# -*- coding: utf-8 -*-
"""edit_fields.py — 批量编辑题目 YAML 的定位/标注字段（删 / 加 / 去重 / 新增字段，保持原写法）。

用法：
  # 模式一 · 去重（扫库）
  python edit_fields.py --去重 册别 [--卷 <卷名>] [--apply]
  # 模式二 · 按操作清单执行
  python edit_fields.py --op <清单.json> [--apply]

字段白名单（均为 YAML list）：册别 / 单元 / 课 / 小节 / 知识点 / 核心素养 / 素养层次水平 / 关键能力
**保持原写法**——流式 `key: [a, b]` 与块状 `key:\\n- a` 分别按原风格渲染（不改动未被点名的字段）。
**新增字段**——`加` 点名的字段若整份 frontmatter 里不存在，则**按规范字段顺序**插到前一字段之后
（顺序 id→册别→单元→课→小节→知识点→核心素养→素养层次水平→关键能力→题型→…），风格沿用前驱字段
（标量前驱 → 用流式）。`删`/`去重` 仍要求字段已存在，找不到只报提示、不新建。

操作清单格式（数组；「文件」为**库内相对路径**）：
```
[
  {"文件": "初中-1中考真题/2026河南中考/2026河南中考_选择题/08.md",
   "删": {"单元": ["第7单元 人民解放战争"]},
   "加": {"课": ["第21课 人民解放战争的胜利"], "小节": ["三大战役和南京解放"]},
   "去重": ["册别"]}
]
```

安全线：
  · 默认 **dry-run** 只打印计划，`--apply` 才落盘；
  · `--apply` 前把原文件备份到 `--backup`（默认 `{vault}/.fieldbackup/<日期>`——点开头，不入 Obsidian 索引）；
  · 写回前用**真 YAML 解析器**复检受影响字段仍为字符串列表（防「块状渲染漏换行」这类事故）；
  · 行尾风格（LF / CRLF）按原文件保持，正文逐字节不动。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from qb_bootstrap import bootstrap, require_vault
from qb_schema import is_question_file

LIST_FIELDS = ["册别", "单元", "课", "小节", "知识点", "核心素养", "素养层次水平", "关键能力"]

# 规范字段顺序（用于「新增字段」定位插入点；比操作白名单宽，含标量字段）
FIELD_ORDER = ["id", "册别", "单元", "课", "小节", "知识点", "核心素养", "素养层次水平",
               "关键能力", "题型", "难度", "分值", "来源", "年份", "考区", "答案", "解析"]


def parse_field(fm: str, key: str):
    """返回 (风格, 值列表, 匹配区间)；风格 ∈ inline / block:<缩进> / scalar / None。"""
    k = re.escape(key)
    m = re.search(rf"^{k}:[ \t]*\[([^\]]*)\][ \t]*$", fm, re.M)
    if m:
        vals = [v.strip() for v in m.group(1).split(",") if v.strip()]
        return "inline", vals, (m.start(), m.end())
    m = re.search(rf"^{k}:[ \t]*\n((?:[ \t]*- [^\n]*\n)*)", fm, re.M)
    if m:
        lines = [l for l in m.group(1).split("\n") if l.strip()]
        vals = [l.split("- ", 1)[1].strip() for l in lines]
        indent = lines[0][:len(lines[0]) - len(lines[0].lstrip())] if lines else ""
        return f"block:{indent}", vals, (m.start(), m.end())
    m = re.search(rf"^{k}:[ \t]*(\S.*)$", fm, re.M)
    if m:
        return "scalar", [m.group(1).strip()], (m.start(), m.end())
    return None, [], None


def render(key: str, style: str, vals: list) -> str:
    if style == "inline":
        return f"{key}: [" + ", ".join(vals) + "]"
    if style == "scalar":
        return f"{key}: {vals[0]}" if len(vals) == 1 else f"{key}: [" + ", ".join(vals) + "]"
    indent = style.split(":", 1)[1]
    # ⚠ 末尾必须保留换行：匹配区间已含最后一个「- 值\n」，否则下一字段名会被吸附到末项
    return f"{key}:\n" + "".join(f"{indent}- {v}\n" for v in vals)


def insert_field(fm: str, key: str, vals: list) -> str:
    """把 frontmatter 里不存在的字段插到「规范顺序中的前一已存在字段」之后。

    风格沿用前驱字段（标量前驱 → 流式）；无可定位前驱时追加到末尾。
    调用方须保证 fm 已规范化为 LF 行尾。
    """
    idx = FIELD_ORDER.index(key)
    for k in reversed(FIELD_ORDER[:idx]):
        style, _v, span = parse_field(fm, k)
        if span is None:
            continue
        use = "inline" if style == "scalar" else style
        if use.startswith("block"):
            # 块状前驱：匹配区间已含最后一个「- 值\n」，新字段直接接在其后
            return fm[:span[1]] + render(key, use, vals) + fm[span[1]:]
        # 流式/标量前驱：区间止于行末，需补换行后插入
        return fm[:span[1]] + "\n" + render(key, use, vals) + fm[span[1]:]
    tail = "" if fm.endswith("\n") else "\n"
    return fm + tail + render(key, "inline", vals)


def edit_frontmatter(fm: str, ops: dict):
    """按 ops（{'删':{}, '加':{}, '去重':[]}）改写 frontmatter，返回 (新 fm, 说明列表, 是否有改动)。

    `加` 点名的字段若不存在 → 按规范顺序新增（仅「加」单独出现时）；`删`/`去重` 仍要求字段已存在。
    """
    notes, changed = [], False
    for key in LIST_FIELDS:
        dels = list(ops.get("删", {}).get(key) or [])
        adds = list(ops.get("加", {}).get(key) or [])
        dedup = key in (ops.get("去重") or [])
        if not (dels or adds or dedup):
            continue
        style, vals, span = parse_field(fm, key)
        if span is None:
            if adds and not dels and not dedup:
                fm = insert_field(fm, key, adds)
                changed = True
                notes.append(f"{key}: 新增字段 → {adds}")
            else:
                notes.append(f"✗ 未找到「{key}」字段（新增须只给「加」、不同时给「删/去重」）")
            continue
        origin = list(vals)
        new = [v for v in vals if v not in dels] if dels else list(vals)
        if dedup:
            new = list(dict.fromkeys(new))
        added = []
        for v in adds:
            if v not in new:
                new.append(v)
                added.append(v)
        if not new:
            notes.append(f"✗「{key}」会被删空，已跳过")
            continue
        if new == origin:
            continue
        removed = [v for v in origin if v not in new]
        missing = [v for v in dels if v not in origin]
        fm = fm[:span[0]] + render(key, style, new) + fm[span[1]:]
        changed = True
        notes.append(f"{key}: 删 {removed or '—'}｜加 {added or '—'}｜去重 {dedup}"
                     + (f"（未匹配：{missing}）" if missing else ""))
    return fm, notes, changed


def check_fm(fm: str, keys) -> str:
    """真 YAML 解析复检：点名过的字段必须是字符串列表。返回错误信息（空即通过）。"""
    try:
        d = yaml.safe_load(fm) or {}
    except yaml.YAMLError as e:
        return f"YAML 解析失败：{e}"
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            return f"「{k}」不是字符串列表：{v!r}"
    return ""


def process(path: Path, ops: dict, apply: bool, backup_dir: Path, vault: Path) -> bool:
    """处理单个文件；返回是否成功（含无变化）。保持原行尾风格，正文不动。"""
    text = path.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        print(f"  ✗ 无 frontmatter：{path}")
        return False
    # 内部一律以 LF 处理，写回时还原原行尾
    fm, body = m.group(1).replace("\r\n", "\n"), text[m.end():]
    new_fm, notes, changed = edit_frontmatter(fm, ops)
    rel = path.relative_to(vault)
    if not changed:
        return True
    err = check_fm(new_fm, LIST_FIELDS)
    if err:
        print(f"  ✗ {rel}\n      {err}")
        return False
    print(f"  · {rel}")
    for n in notes:
        print(f"      {n}")
    if apply:
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        path.write_text("---" + nl + new_fm.replace("\n", nl) + nl + "---" + nl + body, encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="批量编辑题目 YAML 的定位/标注字段")
    ap.add_argument("--去重", nargs="*", default=[], help="要整列去重的字段（如 册别）")
    ap.add_argument("--op", help="操作清单 JSON 路径（与 --去重 二选一）")
    ap.add_argument("--卷", help="仅处理卷名含该串的题目（配合 --去重）")
    ap.add_argument("--apply", action="store_true", help="落盘（默认 dry-run）")
    ap.add_argument("--backup", help="备份目录（默认 {vault}/.fieldbackup/<日期>，点开头不入 Obsidian 索引）")
    ap.add_argument("--vault", help="题库库根（默认取 qb_schema 解析结果）")
    args = ap.parse_args()

    bootstrap()
    if not args.去重 and not args.op:
        ap.error("请指定 --去重 <字段…> 或 --op <清单.json>")

    bad = [k for k in (args.去重 or []) if k not in LIST_FIELDS]
    if bad:
        ap.error(f"--去重 字段不在白名单：{bad}；白名单＝{LIST_FIELDS}")

    vault = require_vault(args.vault)
    # 备份默认落在库内 `.fieldbackup/<日期>`（点开头，不被 Obsidian 索引，与 .dedupe/.linkfix 一致）
    backup_dir = (Path(args.backup) if args.backup
                  else vault / ".fieldbackup" / f"{datetime.now():%Y-%m-%d}")

    if args.op:
        ops_list = json.loads(Path(args.op).read_text(encoding="utf-8"))
        if isinstance(ops_list, dict):
            ops_list = [ops_list]
        ok = 0
        for item in ops_list:
            rel = item.get("文件") or item.get("file")
            if not rel:
                print("  ✗ 清单项缺「文件」")
                continue
            p = Path(vault) / str(rel).replace("/", "\\")
            if not p.exists():
                print(f"  ✗ 不存在：{rel}")
                continue
            if process(p, item, args.apply, backup_dir, Path(vault)):
                ok += 1
        print(f"\n处理 {ok}/{len(ops_list)} 个文件｜模式={'已落盘' if args.apply else 'dry-run'}")
    else:
        total = changed = 0
        for p in sorted(Path(vault).rglob("*.md")):
            if not is_question_file(p):
                continue
            if args.卷 and args.卷 not in str(p):
                continue
            total += 1
            if process(p, {"去重": args.去重}, args.apply, backup_dir, Path(vault)):
                changed += 1
        print(f"\n扫描 {total} 题｜模式={'已落盘' if args.apply else 'dry-run'}")
        print("（仅打印“有变化”的文件；无输出＝全部已无重复项）")

    if args.apply:
        print(f"备份目录：{backup_dir}")
    else:
        print("提示：加 --apply 才会写盘。")


if __name__ == "__main__":
    main()
