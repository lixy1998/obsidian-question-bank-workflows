# -*- coding: utf-8 -*-
"""normalize_id.py — id 写法规范化：标量/块式 → 流式单行 list

`id` 在 `references/fields.md` 中定义为 **list（多值）**：首项 = 主卷 id，其余 = 一题多卷
合并时追加的别名 id。历史上三种写法并存（流式 `[A]`、块式 `id:` + 换行 `- A`、标量
`id: A`），三者 `qb_schema.split_frontmatter` 都读得懂，但**混写会让自写解析器与 Obsidian
属性面板行为不一**（2026-10-23 全库实测：3677 题中 1366 题标量、36 题块式，按 id 建索引
会漏题）。本脚本统一为**流式单行**：

    id: [2026河南中考-01]
    id: [2024河南中考-08, 河南省实验中学2025-2026学年8上期末-03]

只改 frontmatter 内的 `id` 行，**其他字段与正文一字不动**；幂等（已是流式则跳过）。

用法：
  python normalize_id.py                        # 只体检（默认；有待改项退出码 1）
  python normalize_id.py --apply                 # 执行修复（逐文件备份 + 写回滚日志）
  python normalize_id.py --apply --limit=10       # 试点前 10 题
  python normalize_id.py --rollback <日志.json>    # 按日志回滚

⚠ 本脚本**只改写法、不改值**。「序号＝原卷题号」这类值层面的问题由
  `scan_questions.py` 的定位提示或人工判定处理，别在这里顺手改值。
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from qb_bootstrap import bootstrap
from qb_schema import stage_roots  # noqa: E402

ID_RE = re.compile(r"^id\s*:")
ITEM_RE = re.compile(r"^\s*-\s")

ORDER = ("已规范", "流式微调", "标量", "块式单", "块式多", "缺失", "无frontmatter", "不可读")
DESC = {
    "已规范": "流式 list，无需改",
    "流式微调": "已是流式，仅括号内空格不统一",
    "标量": "id: A（单值 str 历史兼容写法）",
    "块式单": "id: 换行 + 单条 - A",
    "块式多": "id: 换行 + 多条 - A",
    "缺失": "无 id 字段",
    "无frontmatter": "无 YAML frontmatter（多为 _ 前缀辅助文件）",
    "不可读": "读取失败（多为云同步占位未下载）",
}


def _quote(v: str) -> str:
    return '"%s"' % v if re.search(r'[,:\[\]{}]', v) else v


def normalize_line(vals) -> str:
    return "id: [" + ", ".join(_quote(v) for v in vals) + "]"


def inspect(text: str):
    """返回 (form, patch)。patch = None 表示无需改；否则 (start, stop, new_line)。"""
    if not text.startswith("---"):
        return "无frontmatter", None
    end = text.find("\n---", 3)
    if end < 0:
        return "无frontmatter", None
    lines = text[3:end].split("\n")
    hit = [i for i, ln in enumerate(lines) if ID_RE.match(ln)]
    if not hit:
        return "缺失", None
    i = hit[0]
    raw = lines[i]
    val = raw.split(":", 1)[1].strip()

    if val.startswith("["):
        vals = [x.strip().strip("'\"") for x in val.strip("[]").split(",") if x.strip()]
        if not vals:
            return "缺失", None
        new = normalize_line(vals)
        return ("已规范", None) if raw.strip() == new else ("流式微调", (i, i + 1, new))

    if val:
        return "标量", (i, i + 1, normalize_line([val.strip("'\"")]))

    vals, j = [], i + 1
    while j < len(lines) and ITEM_RE.match(lines[j]):
        vals.append(lines[j].split("-", 1)[1].strip().strip("'\""))
        j += 1
    if not vals:
        return "缺失", None
    return ("块式单" if len(vals) == 1 else "块式多"), (i, j, normalize_line(vals))


def rewrite(text: str, patch) -> str:
    start, stop, new = patch
    end = text.find("\n---", 3)
    lines = text[3:end].split("\n")
    lines[start] = new
    del lines[start + 1:stop]
    return "---" + "\n".join(lines) + text[end:]


def main():
    bootstrap()
    ap = argparse.ArgumentParser(description="id 写法规范化：标量/块式 → 流式单行 list（只改写法不改值）")
    ap.add_argument("--apply", action="store_true", help="执行修复（默认只体检）")
    ap.add_argument("--limit", type=int, default=0, help="试点：最多修 N 题")
    ap.add_argument("--backup-dir", default="", help="备份目录（默认系统临时目录 qb-id-normalize/时间戳）")
    ap.add_argument("--rollback", default="", help="按日志 JSON 回滚")
    a = ap.parse_args()

    if a.rollback:
        log = json.loads(Path(a.rollback).read_text(encoding="utf-8"))
        n = 0
        for rec in log.get("files", []):
            src = Path(rec["backup"])
            if src.exists():
                shutil.copy2(src, rec["path"])
                n += 1
        print(f"✓ 已回滚 {n} 个文件（日志：{a.rollback}）")
        return 0

    stat, todo, vault = {}, [], None
    for root in stage_roots():
        vault = root.parent
        for p in sorted(root.rglob("*.md")):
            if p.name.startswith("_"):
                stat["无frontmatter"] = stat.get("无frontmatter", 0) + 1
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                stat["不可读"] = stat.get("不可读", 0) + 1
                continue
            form, patch = inspect(text)
            stat[form] = stat.get(form, 0) + 1
            if patch:
                todo.append((p, form, patch, text))

    print("=== id 写法分布 ===")
    for k in ORDER:
        if stat.get(k):
            print(f"  {stat[k]:5d}  {k:<8}  {DESC.get(k, '')}")
    print(f"\n=== 待规范化 {len(todo)} 题 ===")
    for p, form, _, _ in todo[:10]:
        rel = p.relative_to(vault).as_posix() if vault else p.name
        print(f"  [{form}] {rel}")
    if len(todo) > 10:
        print(f"  … 其余 {len(todo) - 10} 题从略")

    if not todo:
        print("\n✓ 全库 id 均为流式 list，无需处理")
        return 0
    if not a.apply:
        print("\n（只体检；加 --apply 执行修复）")
        return 1

    bak = Path(a.backup_dir) if a.backup_dir else Path(tempfile.gettempdir()) / "qb-id-normalize" / time.strftime("%Y%m%d-%H%M%S")
    bak.mkdir(parents=True, exist_ok=True)
    log = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "vault": str(vault), "files": []}
    done = 0
    for p, form, patch, text in todo:
        if a.limit and done >= a.limit:
            break
        rel = p.relative_to(vault).as_posix() if vault else p.name
        dst = bak / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        p.write_text(rewrite(text, patch), encoding="utf-8")
        log["files"].append({"path": str(p), "backup": str(dst), "form": form})
        done += 1
    logfile = bak / "rollback.json"
    logfile.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n✓ 已规范化 {done} 题")
    print(f"  备份与日志：{bak}")
    print(f"  回滚：python normalize_id.py --rollback \"{logfile}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
