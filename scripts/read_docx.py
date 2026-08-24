# -*- coding: utf-8 -*-
"""read_docx.py — 文字版卷子结构化提取（免 MinerU）

文字版（docx）录入链路入口：把 Word 版试卷转成与 MinerU md 类似的中间产物，
供「轻量核对 → 写题库 JSON → gen_problem.py → scan_questions.py」使用。

用法：
  python read_docx.py <试卷.docx> [-o 输出目录]

输出：
  <输出目录>/<docx名>.md    结构化文本（题号/题干/选项/表格/图片引用/【答案】，按文档原顺序）
  <输出目录>/images/        提取的内嵌图片（按出现顺序 001.jpg / 001.png ...）

说明：
  - 文字版多来自第三方整理（zxls 等），必须与官方扫描件交叉核对关键项；
    冲突以官方扫描件为准，差异在题库「解析」字段注明（针对题目本身存在问题的情况）。
  - docx 图片常不完整（如选择题图片选项被吞成 "A.B.C.D."），
    缺图从同目录官方 PDF 用 pull_pdf_images.py 裁剪补图。
  - 运行环境：主环境 python（已装 python-docx）。venv 无 docx 库，勿用 venv 跑。
"""
import argparse
import sys
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn

EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/bmp": "bmp", "image/tiff": "tiff", "image/x-emf": "emf",
    "image/x-wmf": "wmf",
}


def iter_blocks(doc):
    """按文档真实顺序遍历段落与表格。"""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _extract_para_images(paragraph, doc, img_dir, counter, sink):
    """提取段落内嵌图片到 img_dir，sink 追加 ![](images/xxx) 引用。返回新 counter。"""
    for blip in paragraph._p.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid or rid not in doc.part.related_parts:
            continue
        part = doc.part.related_parts[rid]
        ext = EXT_BY_TYPE.get(getattr(part, "content_type", ""), "bin")
        name = f"{counter:03d}.{ext}"
        (img_dir / name).write_bytes(part.blob)
        sink.append(f"![](images/{name})")
        counter += 1
    return counter


def _cell_text(cell, doc, img_dir, counter):
    """表格单元格 → 文本（含单元格内图片引用）。"""
    parts = []
    for p in cell.paragraphs:
        counter = _extract_para_images(p, doc, img_dir, counter, parts)
        t = p.text.strip()
        if t:
            parts.append(t)
    return " ".join(parts).replace("|", "\\|"), counter


def _table_md(table, doc, img_dir, counter):
    md = []
    for i, row in enumerate(table.rows):
        cells = []
        for c in row.cells:
            txt, counter = _cell_text(c, doc, img_dir, counter)
            cells.append(txt)
        md.append("| " + " | ".join(cells) + " |")
        if i == 0:
            md.append("|" + "---|" * len(cells))
    return "\n".join(md), counter


def main():
    ap = argparse.ArgumentParser(description="docx 文字版卷子 → 结构化 md + 图片提取")
    ap.add_argument("docx", help="Word 试卷路径（教师用卷/普通用卷均可）")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认与 docx 同目录）")
    args = ap.parse_args()

    docx_path = Path(args.docx)
    if not docx_path.exists():
        print(f"[失败] 文件不存在：{docx_path}", file=sys.stderr)
        raise SystemExit(1)
    out_dir = Path(args.out) if args.out else docx_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    doc = Document(str(docx_path))
    counter = 1
    out = []
    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            counter = _extract_para_images(block, doc, img_dir, counter, out)
            text = block.text.strip()
            if text:
                out.append(text)
        else:
            md, counter = _table_md(block, doc, img_dir, counter)
            out.append(md)

    md_path = out_dir / f"{docx_path.stem}.md"
    md_path.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    print(f"[输出] {md_path}")
    print(f"[图片] {img_dir} 共 {counter - 1} 张")


if __name__ == "__main__":
    main()
