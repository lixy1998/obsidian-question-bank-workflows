# -*- coding: utf-8 -*-
"""read_docx.py — 文字版卷子结构化提取（免 MinerU）

文字版（docx）录入链路入口：把 Word 版试卷转成与 MinerU md 类似的中间产物，
供「轻量核对 → 写题库 JSON → gen_problem.py → scan_questions.py」使用。

用法：
  python read_docx.py <试卷.docx> [-o 输出目录]

输出：
  <输出目录>/<docx名>.md    结构化文本（题号/题干/选项/表格/图片引用/【答案】，按文档原顺序）
  <输出目录>/images/        提取的内嵌图片（按出现顺序 001.jpg / 001.png ...）
                            格式守卫：原图为 TIFF/EMF/WMF 时自动转 PNG 落盘（Obsidian 不显示 TIFF）

说明：
  - 文字版多来自第三方整理（zxls 等），必须与官方扫描件交叉核对关键项；
    冲突以官方扫描件为准，差异在题库「解析」字段注明（针对题目本身存在问题的情况）。
  - docx 图片常不完整（如选择题图片选项被吞成 "A.B.C.D."），
    缺图从同目录官方 PDF 用 pull_pdf_images.py 裁剪补图。
  - 运行环境：venv 或主环境均可（2026-09-14 实测受管 venv 已装 python-docx，可直接用 venv 解释器执行）
"""
import argparse
import sys
from pathlib import Path

from qb_bootstrap import bootstrap, lazy_import

# 模块级导入改走 lazy_import：缺 python-docx 时给中文安装提示（退出码 3），
# 而不是在 main() 之前抛裸 traceback（bootstrap 的预检来不及生效）
_docx = lazy_import("docx")
Document = _docx.Document
Table = _docx.table.Table
Paragraph = _docx.text.paragraph.Paragraph
qn = _docx.oxml.ns.qn

EXT_BY_TYPE = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/bmp": "bmp",
    "image/tiff": "tiff", "image/x-emf": "emf", "image/x-wmf": "wmf",
}

# Obsidian（Chromium 渲染）能直接显示的图片格式——TIFF 不在列，EMF/WMF 也不在。
# ⚠ 2026-09-15 立：docx 内嵌 TIFF 图片曾一路存成 .tiff 入库，Obsidian 里全是破图。
#   此处做第一道格式守卫：不在白名单的一律转 PNG 再落盘。
DISPLAYABLE = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}


def to_png(blob):
    """不可显示格式的图片字节 → PNG 字节；转换失败返回 None（调用方原样保留并告警）。"""
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
            elif im.mode not in ("L", "RGB"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        pass
    try:
        # Pillow 兜底失败时用 cv2——个别 TIFF 的 IFD（Photometric/ExtraSamples 不匹配）
        # 会让 Pillow 报 "cannot identify image file"，但文件本身完好
        import cv2
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_UNCHANGED)
        if arr is None:
            return None
        ok, buf = cv2.imencode(".png", arr)
        return buf.tobytes() if ok else None
    except Exception:
        return None


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
        blob = part.blob
        if ext not in DISPLAYABLE:          # TIFF/EMF/WMF/bin → PNG（Obsidian 不显示这些）
            png = to_png(blob)
            if png is not None:
                print(f"[格式] {ext} → png（Obsidian 不显示 {ext}）", file=sys.stderr)
                blob, ext = png, "png"
            else:
                print(f"[警告] {ext} 转换失败，原样输出；Obsidian 里可能显示为破图，"
                      f"入库前请手工转换", file=sys.stderr)
        name = f"{counter:03d}.{ext}"
        (img_dir / name).write_bytes(blob)
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
    bootstrap(("docx",))
    ap = argparse.ArgumentParser(description="docx 文字版卷子 → 结构化 md + 图片提取")
    ap.add_argument("docx", help="Word 试卷路径（学生卷或卷内带答案的卷均可）")
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
