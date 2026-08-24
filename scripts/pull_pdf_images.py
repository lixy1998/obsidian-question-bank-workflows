# -*- coding: utf-8 -*-
"""pull_pdf_images.py — 从官方 PDF 裁剪指定区域补图（文字版链路补图用）

文字版（docx）常缺图（如选择题图片选项被吞成 "A.B.C.D."），
本脚本从同目录官方扫描 PDF 渲染页面并裁剪出所需图片，补齐题库「图片」字段。

用法：
  python pull_pdf_images.py <文件.pdf> --page 3 --rect "0.05,0.42,0.95,0.62" --out 6_1.jpg
  python pull_pdf_images.py <文件.pdf> --page 1 --rect "0,0,1,1" --out p01.png --scale 3

参数：
  --page N   1 起始页码
  --rect "x0,y0,x1,y1"   裁剪矩形，坐标为页面宽高的相对值（0~1，左上原点）
  --out 文件名           输出文件
  --scale N              渲染倍率（默认 4，越高越清晰）

流程建议：
  1) 先整页渲染定位：--rect "0,0,1,1" --out page.png
  2) 用看图工具/视觉模型确定图片区域相对坐标
  3) 按相对坐标裁剪输出，再作为题库 JSON「图片」字段的 src
"""
import argparse
import sys
from pathlib import Path

import pymupdf  # PyMuPDF；主环境已装，勿用 venv


def main():
    ap = argparse.ArgumentParser(description="PDF 页面区域裁剪补图")
    ap.add_argument("pdf", help="官方扫描 PDF 路径")
    ap.add_argument("--page", type=int, required=True, help="页码（1 起始）")
    ap.add_argument("--rect", required=True,
                    help='裁剪矩形相对坐标 "x0,y0,x1,y1"（0~1，左上原点）')
    ap.add_argument("--out", required=True, help="输出文件名")
    ap.add_argument("--scale", type=float, default=4.0, help="渲染倍率（默认 4）")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"[失败] 文件不存在：{pdf_path}", file=sys.stderr)
        raise SystemExit(1)

    try:
        x0, y0, x1, y1 = [float(v) for v in args.rect.split(",")]
    except ValueError:
        print("[失败] --rect 需为 4 个数值：x0,y0,x1,y1", file=sys.stderr)
        raise SystemExit(1)

    doc = pymupdf.open(str(pdf_path))
    if not (1 <= args.page <= doc.page_count):
        print(f"[失败] 页码越界：1~{doc.page_count}", file=sys.stderr)
        raise SystemExit(1)
    page = doc[args.page - 1]
    r = page.rect

    clip = pymupdf.Rect(r.x0 + r.width * min(x0, x1), r.y0 + r.height * min(y0, y1),
                        r.x0 + r.width * max(x0, x1), r.y0 + r.height * max(y0, y1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(args.scale, args.scale), clip=clip)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    print(f"[输出] {out_path}（{pix.width}x{pix.height}，页{args.page} 区域 {clip}）")


if __name__ == "__main__":
    main()
