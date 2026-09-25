# -*- coding: utf-8 -*-
"""export_student_paper.py — 按卷导出学生卷/教师卷（v2：支持跨卷组卷）。

学生卷：剥离 frontmatter，只保留题干/选项/材料（答案永不外泄）
教师卷：正文 + 答案 + 解析（追加在每题后）

用法：
  # ① 单卷（原用法不变）
  python export_student_paper.py --卷 2026河南中考
  python export_student_paper.py --卷 2026河南中考 --模式 教师
  python export_student_paper.py --卷 2026河南中考 --only 01,02,03

  # ② 跨卷组卷：多卷一条命令拼成一卷，卷面顺序 = --卷 给定顺序
  python export_student_paper.py --卷 "2024河南中考,2025河南中考" \
         --only "2024河南中考:01,03,05;2025河南中考:07" \
         --输出 <卷目录之外>/期中卷_学生版.md
  # 各卷取相同题号时可省略卷名前缀（题号施于所有卷）：
  python export_student_paper.py --卷 "A,B" --only "01,03" --输出 out.md
  # 也可按位置逐卷对应（组数 == 卷数），如 --卷 "A,B" --only "01,03;07"

  # ③ 教师卷附加来源标注（跨卷核对用，默认关闭）
  python export_student_paper.py --卷 "A,B" --only "A:01;B:07" --模式 教师 \
         --来源标注 --输出 out_教师版.md

输出与行为：
  单卷默认 {卷目录}/{卷名}_学生版.md（或 _教师版.md）；
  多卷必须显式 --输出（避免把拼好的卷写进任一来源卷目录）。
  跨卷时图片按卷放入 images/{卷名}/，同名图不再互相覆盖，正文图片引用同步改写。
  多卷时题号需在排版阶段重编（本脚本不改写正文题号）。
  顺序：给出 --only 时按 --only 的书写顺序（组顺序 + 组内顺序）；
        未给 --only 时按 --排序 排（默认 id）。
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


def norm_no(s) -> str:
    """题号归一化：1 与 01 视为同一题号（原脚本 `--only 1` 匹配不到 01.md）。"""
    s = str(s).strip()
    return str(int(s)) if s.isdigit() else s


def split_nums(s: str) -> list:
    """拆题号串（逗号/顿号/空白分隔），同卷内重复题号去重并保序。"""
    nums = [x.strip() for x in re.split(r"[,，、\s]+", s) if x.strip()]
    if not nums:
        raise SystemExit("[失败] --only 里有空的题号组（如 `卷名:` 后未写题号）")
    seen, out = set(), []
    for n in nums:
        k = norm_no(n)
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def parse_specs(vol_arg: str, only_arg: str) -> list:
    """解析 --卷 / --only → [(卷名, [题号...] | None), ...]，顺序即卷面顺序。

    支持的写法：
      --卷 "A,B"   --only "A:01,03;B:07"   每组带卷名（推荐，跨卷最清晰）
      --卷 "A,B"   --only "01,03"          1 组施于所有卷
      --卷 "A,B"   --only "01,03;07"       组数与卷数相同 → 逐位对应
      --卷 "A"     --only "01,03"          单卷（原用法）
    """
    vols = [v.strip() for v in re.split(r"[,，]", vol_arg) if v.strip()]
    if not vols:
        raise SystemExit("[失败] --卷 不能为空")
    dup_vol = sorted({v for v in vols if vols.count(v) > 1})
    if dup_vol:
        raise SystemExit(
            f"[失败] --卷 里同一卷出现多次：{'、'.join(dup_vol)}\n"
            "        同一卷的题请写在同一个 --only 组内，如 `A:01,03,05`。"
        )

    if not only_arg:
        return [(v, None) for v in vols]

    groups = [s.strip() for s in re.split(r"[;；]", only_arg) if s.strip()]
    if not groups:
        raise SystemExit("[失败] --only 里没有任何题号组")
    named = [bool(re.search(r"[:：]", g)) for g in groups]

    if any(named):
        if not all(named):
            raise SystemExit(
                "[失败] --only 混用了两种写法：带卷名的（`卷名:题号`）与不带卷名的。\n"
                "       请统一——要么每组都写卷名，要么都不写（不带卷名时按 --卷 顺序逐位对应）。"
            )
        specs = []
        for g in groups:
            name, nums = re.split(r"[:：]", g, maxsplit=1)
            specs.append((name.strip(), split_nums(nums)))
        unknown = [n for n, _ in specs if n not in vols]
        if unknown:
            raise SystemExit(
                f"[失败] --only 里的卷名不在 --卷 列表内：{'、'.join(unknown)}\n"
                f"        --卷 = {vols}"
            )
        names = [n for n, _ in specs]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise SystemExit(
                f"[失败] --only 里同一卷出现多次：{'、'.join(dup)}\n"
                "        同卷的题请写在同一个组内，如 `A:01,03,05`。"
            )
        return specs

    if len(groups) == 1:
        return [(v, split_nums(groups[0])) for v in vols]
    if len(groups) == len(vols):
        return [(v, split_nums(g)) for v, g in zip(vols, groups)]
    raise SystemExit(
        f"[失败] --only 有 {len(groups)} 组、--卷 有 {len(vols)} 卷，数量不匹配。\n"
        "        不带卷名的写法：要么只写 1 组（施于所有卷），要么与 --卷 逐位对应。\n"
        "        也可给每组加卷名，如 --only \"A:01,03;B:07\"。"
    )


def collect(vol_dir: Path, qtype: str = None, only: list = None):
    files = [p for p in vol_dir.rglob("*.md")
             if p.stem != "README"
             and not p.name.startswith("_")
             and not p.name.endswith(("_学生版.md", "_教师版.md"))]
    if qtype:
        files = [p for p in files if f"_{qtype}" in p.parent.name]
    if only:
        wanted = {norm_no(x) for x in only}
        files = [p for p in files if norm_no(p.stem) in wanted]
    return files


def order_by_only(files: list, only: list) -> list:
    """按 --only 的书写顺序排列（组内顺序即卷面顺序）。"""
    idx = {norm_no(x): i for i, x in enumerate(only)}
    return sorted(files, key=lambda p: idx.get(norm_no(p.stem), len(idx)))


IMG_REF_RE = re.compile(r"!\[\]\(images/([^)\s]+)\)")
IMG_PREFIX_RE = re.compile(r"!\[\]\(images/")


def img_ns(vol: str) -> str:
    """卷名 → 图片子目录名（去掉路径非法字符与空白）。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", vol)


def bundle_images(units: list, out_dir: Path, multi: bool) -> int:
    """把题目引用的图片复制到导出文件旁的 images/，保证卷面自包含。

    跨卷时按卷分目录 images/{卷名}/，避免不同卷的同名图（如 01_1.png）互相覆盖。
    """
    copied = 0
    for vol, _vol_dir, files in units:
        img_dir = (out_dir / "images" / img_ns(vol)) if multi else (out_dir / "images")
        for p in files:
            body = p.read_text(encoding="utf-8")
            for name in IMG_REF_RE.findall(body):
                src = p.parent / "images" / name
                dst = img_dir / name
                if not src.exists():
                    print(f"[警告] 图片缺失：{src}")
                    continue
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    continue
                img_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except PermissionError:
                    print(f"[警告] 图片被占用跳过：{dst}")
                copied += 1
    if copied:
        print(f"[图片] 已打包 {copied} 张")
    return copied


def export(units: list, mode: str, out: Path, multi: bool, cite: bool = False):
    blocks = []
    for vol, _vol_dir, files in units:
        prefix = f"images/{img_ns(vol)}/" if multi else "images/"
        for p in files:
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            if err:
                print(f"[警告] {p.name}：{err}，该题按无元数据处理")
            body = strip_comments(body)
            if not body:
                continue
            if mode == "教师":
                tail = ""
                if fm:
                    ans = fm.get("答案", "")
                    jx = str(fm.get("解析", "") or "").strip()
                    tail = f"\n\n**答案**：{ans}"
                    if jx:
                        tail += f"\n**解析**：{jx}"
                if cite:
                    tail += f"\n\n**来源**：{vol}（原第 {p.stem} 题）"
                body += tail
            if multi:
                body = IMG_PREFIX_RE.sub(f"![]({prefix}", body)
            blocks.append(body)
    content = "\n\n---\n\n".join(blocks) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    bundle_images(units, out.parent, multi)
    print(f"[导出] {out}（{len(blocks)} 题，{mode}版）")
    for vol, _vol_dir, files in units:
        nums = "、".join(p.stem for p in files) or "（无）"
        print(f"  · {vol}：{len(files)} 题 → {nums}")


def main():
    ap = argparse.ArgumentParser(description="历史题库卷面导出器（v2 支持跨卷组卷）")
    ap.add_argument("--卷", required=True, help="卷名；多卷用逗号分隔（顺序即卷面顺序）")
    ap.add_argument("--学段", choices=["初中", "高中"])
    ap.add_argument("--类别", choices=["中考真题", "中考模拟题", "阶段测试", "高考真题", "高考模拟题"])
    ap.add_argument("--题型")
    ap.add_argument("--模式", choices=["学生", "教师"], default="学生")
    ap.add_argument("--输出")
    ap.add_argument("--only", help="题号；跨卷用分号分组，可写 卷名:题号,题号")
    ap.add_argument("--排序", choices=["路径", "id"], default="id")
    ap.add_argument("--来源标注", action="store_true",
                    help="教师卷每题后附「来源：卷名（原第 N 题）」，跨卷核对用；默认关闭")
    args = ap.parse_args()

    specs = parse_specs(args.卷, args.only)
    multi = len(specs) > 1

    if multi and not args.输出:
        raise SystemExit(
            "[失败] 跨卷组卷必须显式指定 --输出（避免把拼好的卷写进任一来源卷目录）。\n"
            "        例：--输出 \"<工作区>/.workbuddy/tmp/期中卷_学生版.md\""
        )

    units, total = [], 0
    for vol, nums in specs:
        try:
            vol_dir = find_volume(vol, args.学段, args.类别)
        except FileNotFoundError as e:
            raise SystemExit(f"[失败] {e}")
        files = collect(vol_dir, args.题型, nums)
        if nums:
            files = order_by_only(files, nums)
            found = {norm_no(p.stem) for p in files}
            missing = [n for n in nums if norm_no(n) not in found]
            if missing:
                print(f"[警告] {vol}：题号未找到 → {'、'.join(missing)}")
        else:
            files = sorted(files, key=lambda p: sort_key(p, args.排序))
        if not files:
            print(f"[警告] {vol}：没有匹配的题目" + (f"（题号 {'、'.join(nums)}）" if nums else ""))
        units.append((vol, vol_dir, files))
        total += len(files)

    if total == 0:
        print("[失败] 未选到任何题目", file=sys.stderr)
        sys.exit(1)

    out = Path(args.输出) if args.输出 else (
        units[0][1] / f"{units[0][1].name}_{'教师' if args.模式 == '教师' else '学生'}版.md")
    export(units, args.模式, out, multi, args.来源标注)


if __name__ == "__main__":
    main()
