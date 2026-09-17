# -*- coding: utf-8 -*-
"""fix_image_formats.py — 题库图片格式巡检与修复（不可显示格式 → jpg/png）

背景：Obsidian（Chromium 渲染）**不显示 TIFF/TIF/EMF/WMF**，题库里存了就是一排破图。
      源头已在 read_docx.py / pull_pdf_images.py 加格式守卫，本脚本处理**存量**图片。

用法：
  # ① 巡检（只读，不动任何文件）
  python fix_image_formats.py
  python fix_image_formats.py --vault "<题库库路径>"

  # ② 修复（默认 dry-run，只打印计划）
  python fix_image_formats.py --fix
  python fix_image_formats.py --fix --apply                    # 转换 → 改引用 → 原图入回收站
  python fix_image_formats.py --fix --apply --keep-original    # 同上但保留原图（不删）

流程：备份原图 → 转换（真透明→png，其余→jpg；灰度存真灰度 JPEG）→ 同步改 md 引用
      （md 语法与 wikilink 都改）→ 校验（新图可读且尺寸一致、引用零失效）→ 原图移入回收站

格式选择依据（2026-09-15 实测）：连续调扫描件/照片存 PNG 反而比 JPG 大 4~6 倍
（如 853x463 灰度照片：PNG 692KB vs JPG 145KB），故默认转 JPG；只有**真透明**或
**颜色数 ≤16 的图/图表**才留 PNG（无损且更小）。
"""
import argparse
import collections
import ctypes
import json
import os
import re
import shutil
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
CONFIG_FILE = Path(__file__).parent / "skill_config.json"

# Obsidian 能直接显示的格式（Chromium 渲染）；不在列的一律视为待修
DISPLAYABLE = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tif", "tiff",
           "emf", "wmf", "avif", "heic", "jfif", "ico", "psd"}

MD_REF = re.compile(r"\]\(([^)\s]+)\)")          # ![](images/x.jpg) / [](path)
WK_REF = re.compile(r"!\[\[([^\]]+)\]\]")        # ![[images/x.jpg|300]]


def get_vault(cli_vault=None):
    """vault 优先级：--vault > 环境变量 TNOTE_VAULT > scripts/skill_config.json"""
    if cli_vault:
        return Path(cli_vault)
    v = os.environ.get("TNOTE_VAULT")
    if not v and CONFIG_FILE.exists():
        try:
            v = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("vault")
        except Exception:
            v = None
    if not v:
        print("[失败] 未配置题库库：请加 --vault <路径>，或先跑 setup.py 写 skill_config.json",
              file=sys.stderr)
        raise SystemExit(1)
    return Path(v)


# ------------------------------------------------------------
# 图片读取 / 保存
# ------------------------------------------------------------
def load_image(path):
    """读图 → ndarray（灰度 2D / BGR 3 通道 / BGRA 4 通道）。
    用 cv2；个别 TIFF 的 IFD 会让 Pillow 报 cannot identify，cv2 能读。
    np.fromfile 是为绕开 cv2.imread 不支持中文路径。"""
    import cv2
    buf = np.fromfile(str(path), dtype=np.uint8)
    arr = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise ValueError("无法解码")
    return arr


def analyze(arr):
    """→ (kind, has_alpha)；kind ∈ {'gray','rgb'}"""
    if arr.ndim == 2:
        return "gray", False
    ch = arr.shape[2]
    if ch == 4:
        rgb = arr[:, :, :3]
        is_gray = bool((rgb[:, :, 0] == rgb[:, :, 1]).all() and (rgb[:, :, 1] == rgb[:, :, 2]).all())
        return ("gray" if is_gray else "rgb"), bool((arr[:, :, 3] < 255).any())
    return "rgb", False


def choose_format(im, kind, has_alpha, png_max_colors=16):
    """有真透明 → png；颜色数很少（图表/二值）→ png；其余 → jpg"""
    if has_alpha:
        return "png"
    gray = im if im.ndim == 2 else None
    if gray is None and kind == "gray":
        gray = im[:, :, 0]
    if gray is not None and len(np.unique(gray)) <= png_max_colors:
        return "png"
    return "jpg"


def src_dpi(path, default=300.0):
    try:
        from PIL import Image
        with Image.open(path) as im:
            d = im.info.get("dpi")
            if d and d[0]:
                return float(d[0])
    except Exception:
        pass
    return default


def save_image(arr, dst, fmt, dpi):
    import cv2
    from PIL import Image
    if fmt == "png":
        if arr.ndim == 3 and arr.shape[2] == 4:
            img = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGRA2RGBA))
        elif arr.ndim == 3:
            img = Image.fromarray(arr[:, :, ::-1])
        else:
            img = Image.fromarray(arr)
        img.save(str(dst), "PNG")
    else:
        if arr.ndim == 3:
            img = Image.fromarray(arr[:, :, ::-1])
            img.save(str(dst), "JPEG", quality=95, subsampling=0, dpi=(dpi, dpi))
        else:
            Image.fromarray(arr).save(str(dst), "JPEG", quality=95, dpi=(dpi, dpi))


# ------------------------------------------------------------
# 扫描
# ------------------------------------------------------------
def scan(vault):
    """→ (bad_files, ref_map, ref_files, name_count)
    bad_files:  [Path] 扩展名不可显示的图片
    ref_map:    {md路径: 原文}（所有 md）
    ref_files:  {图片 basename(小写): [md路径, ...]}（md 去重）
    name_count: {图片 basename(小写): 库内出现次数}——同名文件不唯一时禁用文件名兜底"""
    bad, ref_map, ref_files = [], {}, collections.defaultdict(list)
    name_count = collections.Counter()
    for dp, dn, fn in os.walk(vault):
        dn[:] = [d for d in dn if d not in (".obsidian", ".git", ".trash")]
        for f in fn:
            p = Path(dp) / f
            ext = p.suffix.lower().lstrip(".")
            if ext in IMG_EXT:
                name_count[f.lower()] += 1
                if ext not in DISPLAYABLE:
                    bad.append(p)
            elif f.endswith(".md"):
                try:
                    ref_map[p] = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    pass
    for md, txt in ref_map.items():
        for m in MD_REF.finditer(txt):
            b = os.path.basename(m.group(1).split("#")[0]).lower()
            if b and md not in ref_files[b]:
                ref_files[b].append(md)
        for m in WK_REF.finditer(txt):
            b = os.path.basename(m.group(1).split("|")[0]).lower()
            if b and md not in ref_files[b]:
                ref_files[b].append(md)
    return bad, ref_map, ref_files, name_count


# ------------------------------------------------------------
# 回收站（Windows）
# ------------------------------------------------------------
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def to_recycle(paths):
    """移入回收站。⚠ 不要用 send2trash（本机环境必报 WinError -2147024809）。
    ⚠ 返回码不可信（成功也可能返回 2）——判定成功请看文件是否已消失。"""
    if not paths:
        return 0
    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x0004, 0x0010, 0x0040, 0x0400
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = "\0".join(str(Path(p).resolve()) for p in paths) + "\0\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc


def recycle_verify(paths, rec_root=None):
    """核对是否真进回收站：扫 $I / $R 记录（当前用户 SID 目录可读）

    盘符取各路径自身的 drive（相对路径先 resolve），仅在无法判定时才用 rec_root 兜底。
    """
    names, sizes = set(), collections.defaultdict(set)
    for drv in {(Path(p).resolve().drive or rec_root) for p in paths}:
        if not drv:
            continue
        root = Path(drv + "/") / "$Recycle.Bin"
        if not root.is_dir():
            continue
        for sid in root.iterdir():
            try:
                items = list(sid.glob("$I*"))
            except Exception:
                continue
            for f in items:
                try:
                    data = f.read_bytes()
                    if struct.unpack_from("<Q", data, 0)[0] == 2:
                        size = struct.unpack_from("<Q", data, 8)[0]
                        plen = struct.unpack_from("<I", data, 24)[0]
                        raw = data[28:28 + (plen - 1) * 2 if plen > 1 else 28]
                    else:
                        size, raw = 0, data[24:]
                    p = raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
                    if p:
                        name = Path(p).name
                        names.add(name)
                        sizes[name].add(size)
                except Exception:
                    pass
    return names, sizes


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="题库图片格式巡检与修复（Obsidian 不显示的格式）")
    ap.add_argument("--vault", default=None, help="题库库根目录（默认读 skill_config.json）")
    ap.add_argument("--fix", action="store_true", help="进入修复模式（默认只巡检）")
    ap.add_argument("--apply", action="store_true", help="真正执行；不加则只打印修复计划（dry-run）")
    ap.add_argument("--keep-original", action="store_true", help="保留原图（不移入回收站）")
    ap.add_argument("--backup", default=None, help="备份目录（默认 <库同级>/_imgfix_backup_<时间戳>）")
    a = ap.parse_args()

    vault = get_vault(a.vault)
    if not vault.is_dir():
        print(f"[失败] 目录不存在：{vault}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[题库] {vault}")

    bad, ref_map, ref_files, name_count = scan(vault)
    print(f"[md] 扫描 {len(ref_map)} 篇笔记，库内图片 {sum(name_count.values())} 个")

    if not bad:
        print("\n✅ 未发现 Obsidian 不显示的图片格式（tiff/emf/wmf 等）——无需修复")
        return

    print(f"\n发现 {len(bad)} 个不可显示格式的图片：")
    plan = []
    for p in sorted(bad):
        cited = ref_files.get(p.name.lower(), [])
        try:
            arr = load_image(p)
            kind, has_alpha = analyze(arr)
            fmt = choose_format(arr, kind, has_alpha)
            h, w = arr.shape[:2]
            note = f"{w}x{h} {kind}{'+alpha' if has_alpha else ''}"
        except Exception as e:
            fmt, note = None, f"解码失败：{e}"
        dst = p.with_suffix("." + fmt) if fmt else None
        plan.append({"src": p, "dst": dst, "fmt": fmt, "note": note,
                     "cited": cited, "orphan": not cited})
        tag = "被引" if cited else "孤儿"
        print(f"  [{tag}] {p.relative_to(vault)}  →  {dst.name if dst else '?'}   ({note})"
              + (f"  引用于 {len(cited)} 篇" if cited else ""))

    n_cited = sum(1 for x in plan if not x["orphan"])
    print(f"\n合计：{len(plan)} 个（被引 {n_cited} / 孤儿 {len(plan) - n_cited}；"
          f"{sum(x['src'].stat().st_size for x in plan) / 1024:.0f}KB）")

    if not a.fix:
        print("\n（当前为巡检模式。加 --fix 看修复计划，加 --fix --apply 真正执行）")
        return

    # ---- 修复计划 ----
    conflict = [x for x in plan if x["dst"] and x["dst"].exists()]
    ready = [x for x in plan if x["dst"] and not x["dst"].exists()]
    touched = {md for x in ready for md in x["cited"]}
    print(f"\n[计划] 转换 {len(ready)} 个；涉及引用 {len(touched)} 篇笔记；"
          f"原图{'保留' if a.keep_original else '移入回收站'}")
    if conflict:
        print("  ⚠ 目标文件已存在，这些将跳过：")
        for x in conflict:
            print(f"     {x['dst'].name}")

    if not a.apply:
        print("\n（dry-run，未改动任何文件。确认后加 --apply）")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = Path(a.backup) if a.backup else vault.parent / f"_imgfix_backup_{stamp}"
    backup.mkdir(parents=True, exist_ok=True)

    def bak(p):
        """备份到 <backup>/<img|md>/<相对题库的路径>——保留目录结构。
        ⚠ 不能平铺：题库里不同卷常有同名图（多个 01_1.tiff），平铺会互相覆盖。"""
        d = backup / ("md" if p.suffix.lower() == ".md" else "img") / p.relative_to(vault)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, d)
        return d

    print(f"\n[备份] 原图/原件 → {backup}" + os.linesep)

    done = []          # 已成功转换的项
    for x in plan:
        if not x["dst"] or x["dst"].exists():
            continue
        src, dst = x["src"], x["dst"]
        bak(src)                                     # 1) 备份
        arr = load_image(src)                        # 2) 转换
        fmt = x["fmt"]
        save_image(arr, dst, fmt, src_dpi(src))      # 3) 落盘
        chk = load_image(dst)                        # 4) 校验：可读 + 尺寸一致
        if chk.shape[:2] != arr.shape[:2]:
            print(f"  ✗ 尺寸不一致，回滚：{src.name}")
            dst.unlink(missing_ok=True)
            continue
        bak(dst)                                     # 备份新图，便于对比
        done.append(x)
        print(f"  ✓ {src.name} → {dst.name}  ({src.stat().st_size / 1024:.0f}KB → "
              f"{dst.stat().st_size / 1024:.0f}KB)")

    # 5) 改引用：优先按绝对路径精确匹配；同名文件（题库里多个 01_1.tiff）只在
    #    「done 内唯一 且 全库唯一」时才按文件名兜底。
    #    ⚠ 只按文件名匹配是危险的：同名两图若一个转 jpg、一个转 png，会把后者改错。
    abs_map = {str(x["src"].resolve()): x["dst"].name for x in done}
    by_name = collections.defaultdict(set)
    for x in done:
        by_name[x["src"].name.lower()].add(x["dst"].name)
    name_map = {k: next(iter(v)) for k, v in by_name.items()
                if len(v) == 1 and name_count.get(k, 0) == 1}

    def lookup(base_dir, url):
        """url 相对 base_dir 解析 → 新文件名；命中不了返回 None"""
        try:
            if str((base_dir / url).resolve()) in abs_map:
                return abs_map[str((base_dir / url).resolve())]
        except Exception:
            pass
        return name_map.get(os.path.basename(url).lower())

    def fix_text(md, txt):
        def sub_md(m):
            url = m.group(1).split("#")[0]
            nb = lookup(md.parent, url)
            return m.group(0).replace(os.path.basename(url), nb) if nb else m.group(0)

        def sub_wk(m):
            tgt = m.group(1).split("|")[0]
            nb = lookup(vault, tgt) or lookup(md.parent, tgt)     # wikilink 多按库根解析
            return "![[" + m.group(1).replace(os.path.basename(tgt), nb) + "]]" if nb else m.group(0)

        return WK_REF.sub(sub_wk, MD_REF.sub(sub_md, txt))

    changed = 0
    for md, txt in ref_map.items():
        new = fix_text(md, txt)
        if new != txt:
            bak(md)                                  # md 原件也备份（保留目录结构）
            md.write_text(new, encoding="utf-8")
            changed += 1
    print(f"[引用] 改写 {changed} 篇笔记")

    # 6) 复检：新图全部可读、引用零失效
    after = [x for x in done]
    broken = []
    for x in after:
        if not x["dst"].exists():
            broken.append(str(x["dst"]))
    all_md = {p: p.read_text(encoding="utf-8", errors="ignore") for p in ref_map}
    miss = 0
    for md, txt in all_md.items():
        for m in MD_REF.finditer(txt):
            u = m.group(1).split("#")[0]
            if u.startswith(("http", "data:")) or "." not in os.path.basename(u):
                continue
            if os.path.splitext(u)[1].lower().lstrip(".") in IMG_EXT and not (md.parent / u).exists():
                miss += 1
                print(f"  ✗ 断链：{md.relative_to(vault)} → {u}")
    print(f"[复检] 新图缺失 {len(broken)}；图片引用断链 {miss}"
          + ("  ✓" if not broken and not miss else "  ⚠ 请检查"))

    # 7) 原图移入回收站
    if not a.keep_original:
        srcs = [x["src"] for x in done]
        # 记录「名 + 字节数」的列表（不用 dict：同名图会互相顶掉）
        before = [(p.name, p.stat().st_size) for p in srcs]
        rc = to_recycle(srcs)
        time.sleep(0.6)
        left = [p for p in srcs if p.exists()]
        names, sizes = recycle_verify(srcs)
        hit = sum(1 for n, _ in before if n in names)
        hit_size = sum(1 for n, s in before if s in sizes.get(n, set()))
        print(f"[回收] 移入 {len(srcs)} 个（Shell 返回码 {rc}，不可信）；仍在原目录 {len(left)}"
              f"；回收站核对 {hit}/{len(srcs)} 命中，其中 {hit_size} 个大小一致"
              + ("  ✓" if not left and hit_size == len(srcs) else "  ⚠ 请人工核对"))
    else:
        print("[回收] 已按 --keep-original 保留原图")

    # 8) 台账
    ledger = backup / "fix_ledger.txt"
    lines = [f"题库图片格式修复台账  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"题库：{vault}", f"备份：{backup}", ""]
    lines += [f"{x['src'].name:<20} → {x['dst'].name:<20} {x['note']:<22} "
              f"{'被引' if not x['orphan'] else '孤儿'}" for x in done]
    lines += ["", f"转换 {len(done)} 个；改写 {changed} 篇；原图"
              f"{'保留' if a.keep_original else '已入回收站'}",
              f"未处理（目标已存在/解码失败）：{[x['src'].name for x in plan if x not in done] or '无'}"]
    ledger.write_text("\n".join(lines), encoding="utf-8")
    print(f"[台账] {ledger}")


if __name__ == "__main__":
    main()
