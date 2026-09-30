# -*- coding: utf-8 -*-
"""import_from_md.py — 源头 md → 录入 JSON：批量回填「题干」「选项」。

用法：
  python import_from_md.py --源 <提取出的 md> --json <录入 JSON> [--skip 7,18] [--dry-run]

**链路B（文字版 docx/md）批量录入的第二步**——先用 `read_docx.py` 把试卷提取成 md，
再用本脚本按题号把题干与选项回填进录入 JSON，免去逐题复制粘贴；JSON 的其余字段
（册别/单元/课/答案/解析/图片…）保持不动。

自动处理（均为录入实测踩过的坑）：
  · **剔除卷首「注意事项」段**——其「1. 2. 3.」编号行会被正则误认成题号，
    按「首次出现」建块时会覆盖真正的第 1~3 题；
  · **截断答案区**——按「此资料来源于 / 历史试题参考答案 / 【答案与解析】/ 参考答案」
    与紧凑答案行（`1-5:BABDA`）中**最靠前者**截断（试卷区恒在答案区之前）；
  · **选择题**切出题干与 A–D 选项：容忍两项紧邻、字母后无空格（`理性C.人物…`）；
  · **非选择题**删块首整题分值（`（20分）`，可位于行首或行尾），**保留各小问分值**；
  · **图片引用**按题号规范为 `images/{题号}_{n}.{ext}`，**保留原扩展名**
    （库内不可显示格式另有 `fix_image_formats.py` 兜底）。

只改写 JSON 中的「题干」「选项」两字段，JSON 结构与其他字段一律不动。
默认直接写回；加 `--dry-run` 只打印预览。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from qb_bootstrap import bootstrap

ANSWER_MARKERS = ("此资料来源于", "历史试题参考答案", "【答案与解析】", "参考答案")
ANSWER_ROW_RE = re.compile(r"\n[ \t]*\d{1,2}[-–]\d{1,2}[：:]")
NOTE_RE = re.compile(r"[注註]意事项")
SECTION_RE = re.compile(r"\n[ \t]*[一二三四五六七八九十]+、")
QNO_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?(\d{1,2})[.．][ \t]*(.*)$")
OPT_START_RE = re.compile(r"(?:^|\n)[ \t]*A[.．]")
IMG_RE = re.compile(r"!\[[^\]]*\]\(images/[^)]+\)")


def strip_answer_area(md: str) -> str:
    """截断答案区（取各标记中首次出现的最靠前者）。"""
    cands = [md.find(m) for m in ANSWER_MARKERS]
    m = ANSWER_ROW_RE.search(md)
    if m:
        cands.append(m.start())
    cands = [c for c in cands if c > 0]
    return md[:min(cands)] if cands else md


def strip_notes_area(md: str) -> str:
    """删除卷首「注意事项」段（到其后第一个「一、/二、」节标题为止）。"""
    m = NOTE_RE.search(md)
    if not m:
        return md
    m2 = SECTION_RE.search(md[m.start():])
    if not m2:
        return md
    return md[:m.start()] + md[m.start() + m2.start():]


def load_blocks(md: str) -> dict:
    """按题号切块（保留**首次**出现的块——试卷区在答案区之前）。"""
    blocks, cur = {}, None
    for line in md.split("\n"):
        m = QNO_RE.match(line)
        if m:
            cur = int(m.group(1))
            if cur not in blocks:
                blocks[cur] = m.group(2)
        elif cur is not None:
            blocks[cur] = blocks[cur] + "\n" + line
    return blocks


def split_options(block: str):
    """块 → (题干, [选项…])；无选项行时返回 (题干, [])。"""
    m = OPT_START_RE.search(block)
    if not m:
        return block.strip(), []
    stem = block[:m.start()].strip()
    opt_part = re.sub(r"[ \u3000]+", " ", block[m.start():].replace("\n", " ").replace("\t", " ")).strip()
    opts = []
    for part in re.split(r"(?=[ABCD][.．])", opt_part):
        part = part.strip()
        mm = re.match(r"^([ABCD])[.．]\s*(.*)$", part)
        if mm:
            opts.append(mm.group(2).strip())
    return stem, opts


def norm_body(text: str, qno: int, strip_total_score: bool = False) -> str:
    """整题分值剔除 + 图片引用规范化 + 空行收敛。"""
    s = text.strip()
    if strip_total_score:
        lines = s.split("\n")
        if lines:
            head = re.sub(r"^（\d+分）\s*", "", lines[0])
            head = re.sub(r"\s*（\d+分）\s*$", "", head)
            lines[0] = head
            s = "\n".join(lines).strip()
    counter = [0]

    def _img(_m):
        counter[0] += 1
        ext = _m.group(0).rsplit(".", 1)[-1].rstrip(")").strip().lower()
        return f"![](images/{qno}_{counter[0]}.{ext})"

    s = IMG_RE.sub(_img, s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def main() -> None:
    bootstrap()
    ap = argparse.ArgumentParser(description="源 md → 录入 JSON：回填题干/选项")
    ap.add_argument("--源", required=True, help="源 md 路径")
    ap.add_argument("--json", required=True, help="录入 JSON 路径（数组或单对象）")
    ap.add_argument("--skip", default="", help="逗号分隔题号：保留 JSON 中已手写的内容（如 7,18）")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写回 JSON")
    args = ap.parse_args()

    skip = {int(x) for x in re.split(r"[,，\s]+", args.skip.strip()) if x.strip().isdigit()}
    src = Path(args.源)
    if not src.exists():
        print(f"✗ 源文件不存在：{src}")
        sys.exit(1)
    md = src.read_text(encoding="utf-8")
    md = strip_answer_area(md)
    md = strip_notes_area(md)
    blocks = load_blocks(md)

    tgt = Path(args.json)
    data = json.loads(tgt.read_text(encoding="utf-8"))
    single = isinstance(data, dict)
    if single:
        data = [data]

    filled = missing = skipped = 0
    for rec in data:
        qno = int(rec.get("序号"))
        if qno in skip:
            skipped += 1
            print(f"  - 跳过 {qno}（保留 JSON 现有内容）")
            continue
        blk = blocks.get(qno)
        if not blk:
            missing += 1
            print(f"  ⚠ 序号 {qno}：源 md 中未找到题块")
            continue
        if rec.get("题型") == "选择题":
            blk = re.split(SECTION_RE, blk)[0]      # 块尾可能粘住下一节标题
            stem, opts = split_options(blk)
            rec["题干"] = norm_body(stem, qno)
            rec["选项"] = opts
            if not opts:
                print(f"  ⚠ 序号 {qno}：未切出选项，请人工核对")
            filled += 1
        else:
            rec["题干"] = norm_body(blk, qno, strip_total_score=True)
            filled += 1

    print(f"\n回填 {filled} 题｜跳过 {skipped} 题｜源中缺块 {missing} 题")
    if args.dry_run:
        print("（--dry-run，未写回 JSON）")
        return
    tgt.write_text(json.dumps(data if not single else data[0], ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[写出] {tgt}")


if __name__ == "__main__":
    main()
