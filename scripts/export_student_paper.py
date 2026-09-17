# -*- coding: utf-8 -*-
"""export_student_paper.py — 按卷导出学生卷/教师卷。

学生卷：剥离 frontmatter，只保留题干/选项/材料（答案永不外泄）
教师卷：正文 + 答案 + 解析（追加在每题后）

用法：
  python export_student_paper.py --卷 2026河南中考
  python export_student_paper.py --卷 2026河南中考 --模式 教师
  python export_student_paper.py --卷 2026河南中考 --题型 选择题 --输出 <输出目录>/2026河南中考_学生版.md
  python export_student_paper.py --卷 2026河南中考 --only 01,02,03 --排序 路径|id

输出：默认 {卷目录}/{卷名}_学生版.md 或 _教师版.md
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

from qb_schema import VAULT, split_frontmatter, stage_roots, strip_comments

try:
    import yaml
except ImportError:
    yaml = None


def find_volume(vol: str, stage: str = None, category: str = None) -> Path:
    """定位卷目录：{vault}/{学段}-{类别}/{卷名}。"""
    cands = []
    for root in stage_roots():
        if stage and not root.name.startswith(stage):
            continue
        if category and category not in root.name:
            continue
        cands.extend(p for p in root.glob(vol) if p.is_dir())
    cands = sorted(set(cands))
    if not cands:
        raise FileNotFoundError(f"未找到卷目录：{vol}")
    if len(cands) > 1:
        print(f"[提示] 命中多个同名目录，取第一个：\n  " + "\n  ".join(map(str, cands)))
    return cands[0]


def sort_key(p: Path, mode: str):
    name = p.stem
    if mode == "id":
        m = re.search(r"(\d+)", name)
        return (0, int(m.group(1))) if m else (1, name)
    return (0, name)


def collect(vol_dir: Path, qtype: str = None, only: list = None):
    files = [p for p in vol_dir.rglob("*.md")
             if p.stem != "README" and not p.name.endswith(("_学生版.md", "_教师版.md"))]
    if qtype:
        files = [p for p in files if f"_{qtype}" in p.parent.name]
    if only:
        wanted = set(only)
        files = [p for p in files if p.stem in wanted or p.stem.zfill(2) in wanted]
    return files


IMG_RE = re.compile(r"!\[\]\(images/([^)\s]+)\)")


def bundle_images(files, out_dir: Path) -> None:
    """把题目引用的图片复制到导出文件旁的 images/，保证卷面自包含。"""
    img_dir = out_dir / "images"
    copied = set()
    for p in files:
        body = p.read_text(encoding="utf-8")
        for name in IMG_RE.findall(body):
            src = p.parent / "images" / name
            dst = img_dir / name
            if not src.exists():
                print(f"[警告] 图片缺失：{src}")
                continue
            if name in copied or (dst.exists() and dst.stat().st_size == src.stat().st_size):
                copied.add(name)
                continue
            img_dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except PermissionError:
                print(f"[警告] 图片被占用跳过：{dst}")
            copied.add(name)
    if copied:
        print(f"[图片] 已打包 {len(copied)} 张到 {img_dir}")


def export(vol_dir: Path, mode: str, files, out: Path):
    blocks = []
    for i, p in enumerate(files, 1):
        fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
        if err:
            print(f"[警告] {p.name}：{err}，该题按无元数据处理")
        body = strip_comments(body)
        if not body:
            continue
        if mode == "教师" and fm:
            ans = fm.get("答案", "")
            jx = str(fm.get("解析", "") or "").strip()
            tail = f"\n\n**答案**：{ans}"
            if jx:
                tail += f"\n**解析**：{jx}"
            body += tail
        blocks.append(body)
    content = "\n\n---\n\n".join(blocks) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    bundle_images(files, out.parent)
    print(f"[导出] {out}（{len(blocks)} 题，{mode}版）")


def main():
    ap = argparse.ArgumentParser(description="历史题库卷面导出器")
    ap.add_argument("--卷", required=True)
    ap.add_argument("--学段", choices=["初中", "高中"])
    ap.add_argument("--类别", choices=["中考真题", "中考模拟题", "阶段测试", "高考真题", "高考模拟题"])
    ap.add_argument("--题型")
    ap.add_argument("--模式", choices=["学生", "教师"], default="学生")
    ap.add_argument("--输出")
    ap.add_argument("--only", help="逗号分隔的题号，如 01,02,03")
    ap.add_argument("--排序", choices=["路径", "id"], default="id")
    args = ap.parse_args()

    vol_dir = find_volume(args.卷, args.学段, args.类别)
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    files = collect(vol_dir, args.题型, only)
    if not files:
        print(f"[失败] {vol_dir} 下没有匹配的题目文件", file=sys.stderr)
        sys.exit(1)
    files.sort(key=lambda p: sort_key(p, args.排序))

    out = Path(args.输出) if args.输出 else (
        vol_dir / f"{vol_dir.name}_{'教师' if args.模式 == '教师' else '学生'}版.md")
    export(vol_dir, args.模式, files, out)


if __name__ == "__main__":
    main()
