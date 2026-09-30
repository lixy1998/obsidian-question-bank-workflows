# -*- coding: utf-8 -*-
"""paperkit.images — 多形态图片引用的识别、打包与改写。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
覆盖五类引用形态：标准 md / md-alt / md-space / wk-bare / wk-path。
"""
import re
import shutil
from pathlib import Path

from paperkit.paths import img_ns

# —— 多形态图片引用识别（v1.22.0 放宽，2026-09-27）——
# 旧 IMG_REF_RE 只认 `![](images/无空格文件名)`，其余形态出卷时静默丢图
# （2026-09-26 七上月考第 5 题实测根因）。现兼容五类：
#   标准     ![](images/x.jpg)
#   md-alt   ![327](images/x.jpg)
#   md-space ![](images/Pasted image x.png)   （URL 含空格）
#   wk-bare  ![[Pasted image x.png]]          （Obsidian 粘贴默认）
#   wk-path  ![[某目录/x.jpg|314]]            （带路径/尺寸）
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp|svg)\s*$", re.I)
MD_IMG_ANY_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
WK_IMG_ANY_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")


def _is_img_ref(p: str) -> bool:
    """是否图片引用（扩展名白名单；http(s)/data: 外链不算本地图）。"""
    p = p.strip()
    return (not re.match(r"(?i)^(https?:|data:)", p)
            and bool(IMG_EXT_RE.search(p)))


def iter_img_refs(text: str) -> list:
    """识别全部形态图片引用 → [(规范化路径, 原始完整引用)]。

    规范化：去首尾空白、反斜杠→正斜杠；wikilink 已去掉 `|尺寸` 后缀。
    非图片扩展名（如 `![[某笔记]]` 笔记嵌入）不算。
    """
    refs = []
    for m in MD_IMG_ANY_RE.finditer(text):
        p = m.group(1).strip().replace("\\", "/")
        if _is_img_ref(p):
            refs.append((p, m.group(0)))
    for m in WK_IMG_ANY_RE.finditer(text):
        p = m.group(1).strip().replace("\\", "/")
        if _is_img_ref(p):
            refs.append((p, m.group(0)))
    return refs


def resolve_img_src(ref: str, note_dir: Path) -> Path:
    """引用路径 → 实际图片路径（题库约定：图片放题目同目录 images/ 子夹）。"""
    if re.match(r"(?i)^images/", ref):
        return note_dir / ref
    cand = note_dir / ref                                   # 引用自带相对路径
    if cand.exists():
        return cand
    return note_dir / "images" / ref.split("/")[-1]         # 兜底：同目录 images/ 同名


def _repl_std(ref: str) -> str:
    """任意形态引用 → 标准 md 形态（无命名空间；命名空间由 rewrite_img 统一加）。"""
    return f"![](images/{ref.split('/')[-1]})"


def _copy_img_refs(text: str, note_dir: Path, img_dir: Path) -> int:
    """把 text 中全部形态图片引用复制到 img_dir（按 basename 落位），返回张数。"""
    copied = 0
    for ref, _raw in iter_img_refs(text):
        name = ref.split("/")[-1]
        src = resolve_img_src(ref, note_dir)
        dst = img_dir / name
        if not src.exists():
            print(f"[警告] 图片缺失：{src}")
            continue
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        img_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            copied += 1
        except PermissionError:
            print(f"[警告] 图片被占用跳过：{dst}")
    return copied


def bundle_images(recs: list, out_dir: Path) -> int:
    """把选中题目引用的图片复制到 {输出目录}/images/{卷名}/。"""
    copied = 0
    for r in recs:
        img_dir = out_dir / "images" / img_ns(r["vol"])
        copied += _copy_img_refs(
            r["path"].read_text(encoding="utf-8"), r["path"].parent, img_dir)
    return copied


def rewrite_img(body: str, vol: str, ns: bool = True) -> str:
    """把全部形态图片引用统一改写为 `![](images/[{卷命名空间}/]{文件名})`。

    ns=False 用于单卷导出（无跨卷命名空间，直接 images/{文件名}）。
    """
    prefix = f"images/{img_ns(vol)}/" if ns else "images/"

    def _repl(ref: str) -> str:
        return f"![]({prefix}{ref.split('/')[-1]})"

    def _sub_md(m):
        p = m.group(1).strip().replace("\\", "/")
        return _repl(p) if _is_img_ref(p) else m.group(0)

    def _sub_wk(m):
        p = m.group(1).strip().replace("\\", "/")
        return _repl(p) if _is_img_ref(p) else m.group(0)

    body = MD_IMG_ANY_RE.sub(_sub_md, body)
    return WK_IMG_ANY_RE.sub(_sub_wk, body)
