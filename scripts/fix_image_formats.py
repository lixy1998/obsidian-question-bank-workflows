# -*- coding: utf-8 -*-
"""fix_image_formats.py — 题库图片巡检与修复（不可显示格式 + 非标准引用形态）

两类问题，一条命令：

① 格式：Obsidian（Chromium 渲染）**不显示 TIFF/TIF/EMF/WMF**，题库里存了就是一排破图。
  源头已在 read_docx.py / pull_pdf_images.py 加格式守卫，本脚本处理**存量**图片。

② 引用形态：export_student_paper.py 自 v1.22.0 起 IMG 识别已多形态兼容（出卷不再丢图），
  但库内非标准引用仍影响渲染一致性与协作卫生，本脚本统一规范化为 `![](images/<无空格文件名>)`。
  五类非标准引用（v1.21.0 曾致出卷静默丢图，2026-09-26 七上月考实测第 5 题根因）：
    wk-bare   ![[Pasted image x.png]]         无路径 wikilink（Obsidian 粘贴默认形态）
    wk-path   ![[某目录/x.jpg|314]]           带路径/带尺寸 wikilink
    md-alt    ![327](images/x.jpg)            alt 非空
    md-space  ![](images/Pasted image x.png)  文件名含空格（老 MD_REF 也匹配不到）
    md-broken ![](images/x.jpg)               标准形态但图不在引用路径上
  修复 = 统一改写为 `![](images/<无空格文件名>)`；图不在同目录 images/ 时自动移入
  （题库约定：题目图片一律放同目录 images/ 子夹）。

用法：
  # ① 巡检（只读，不动任何文件）
  python fix_image_formats.py
  python fix_image_formats.py --vault "<题库库路径>"

  # ② 修复（默认 dry-run，只打印计划）
  python fix_image_formats.py --fix
  python fix_image_formats.py --fix --apply                    # 规范化引用 → 转换格式 → 改引用 → 原图入回收站
  python fix_image_formats.py --fix --apply --keep-original    # 同上但保留原图（不删）

流程：备份 → ①引用规范化（wikilink/alt/空格 → 标准引用；含空格图改名、异位图移入 images/）
      → ②格式转换（真透明→png，其余→jpg；灰度存真灰度 JPEG）→ 同步改 md 引用
      → 校验（新图可读且尺寸一致、引用零失效）→ 原图移入回收站
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

from qb_bootstrap import bootstrap, lazy_import

# 模块级导入改走 lazy_import：缺 numpy 时给中文安装提示（退出码 3），
# 而不是在 main() 之前抛裸 traceback（bootstrap 的预检来不及生效）
np = lazy_import("numpy")

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
# vault 定位统一走 qb_bootstrap.resolve_vault（--vault > TNOTE_VAULT > skill_config.json）

# Obsidian 能直接显示的格式（Chromium 渲染）；不在列的一律视为待修
DISPLAYABLE = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tif", "tiff",
           "emf", "wmf", "avif", "heic", "jfif", "ico", "psd"}

MD_REF = re.compile(r"\]\(([^)\s]+)\)")          # ![](images/x.jpg) / [](path)
WK_REF = re.compile(r"!\[\[([^\]]+)\]\]")        # ![[images/x.jpg|300]]
MD_IMG_ANY = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")   # 任意 md 图片引用（含 alt 非空 / URL 含空格）


def get_vault(cli_vault=None) -> Path:
    """vault 优先级：--vault > 环境变量 TNOTE_VAULT > scripts/skill_config.json。

    2026-09-30 起统一走 qb_bootstrap.resolve_vault（与其余脚本同一套优先级与退出码），
    本函数仅保留为返回 Path 的薄封装。
    """
    from qb_bootstrap import resolve_vault

    return Path(resolve_vault(cli_vault))


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
    """有真透明 → png；颜色数很少（图表/二值）→ png；其余 → jpg
    格式选择依据（2026-09-15 实测）：连续调扫描件/照片存 PNG 反而比 JPG 大 4~6 倍
    （如 853x463 灰度照片：PNG 692KB vs JPG 145KB），故默认转 JPG；只有**真透明**或
    **颜色数 ≤16 的图/图表**才留 PNG（无损且更小）。"""
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
    """→ (bad_files, ref_map, ref_files, name_count, img_by_name)
    bad_files:  [Path] 扩展名不可显示的图片
    ref_map:    {md路径: 原文}（所有 md）
    ref_files:  {图片 basename(小写): [md路径, ...]}（md 去重）
    name_count: {图片 basename(小写): 库内出现次数}——同名文件不唯一时禁用文件名兜底
    img_by_name:{图片 basename(小写): [Path, ...]} 全库图片路径索引（引用形态定位用）"""
    bad, ref_map, ref_files = [], {}, collections.defaultdict(list)
    name_count = collections.Counter()
    img_by_name = collections.defaultdict(list)
    for dp, dn, fn in os.walk(vault):
        dn[:] = [d for d in dn if d not in (".obsidian", ".git", ".trash",
                                            ".Z-Archive", ".dedupe")]
        for f in fn:
            p = Path(dp) / f
            ext = p.suffix.lower().lstrip(".")
            if ext in IMG_EXT:
                name_count[f.lower()] += 1
                img_by_name[f.lower()].append(p)
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
    return bad, ref_map, ref_files, name_count, dict(img_by_name)


# ------------------------------------------------------------
# 引用形态扫描与规范化（wikilink 粘贴图 / 非空 alt / 含空格文件名）
# ------------------------------------------------------------
def scan_refs(ref_map, img_by_name):
    """扫描全库 md 图片引用形态 → 非标准引用清单 {md: [item]}
    item = dict(kind, raw, target)；kind 五类见文件头 docstring。"""
    bad_refs = collections.defaultdict(list)
    for md, txt in ref_map.items():
        for m in WK_REF.finditer(txt):
            tgt = m.group(1).split("|")[0].strip()
            if not tgt or tgt.startswith(("http", "data:")):
                continue
            if os.path.splitext(tgt)[1].lower().lstrip(".") not in IMG_EXT:
                continue                       # ![[某笔记]] 等非图片嵌入
            kind = "wk-path" if "/" in tgt else "wk-bare"
            bad_refs[md].append({"kind": kind, "raw": m.group(0), "target": tgt})
        for m in MD_IMG_ANY.finditer(txt):
            alt, url = m.group(1).strip(), m.group(2).strip()
            if url.startswith(("http", "data:")):
                continue
            base = os.path.basename(url.split("#")[0])
            if not base or "." not in base:
                continue
            if os.path.splitext(base)[1].lower().lstrip(".") not in IMG_EXT:
                continue
            if alt:
                bad_refs[md].append({"kind": "md-alt", "raw": m.group(0), "target": url})
            elif " " in url:
                bad_refs[md].append({"kind": "md-space", "raw": m.group(0), "target": url})
            elif not (md.parent / url.split("#")[0]).exists():
                bad_refs[md].append({"kind": "md-broken", "raw": m.group(0), "target": url})
    return dict(bad_refs)


def build_ref_by_path(ref_map):
    """标准 md 引用按「解析后的图路径」建反查索引 → {normcase(图路径): {md, ...}}
    ⚠ 覆盖检查必须用它而不是 ref_files：题库里 01_1.jpg / 21_1.jpg 这类同名图是常态，
    ref_files 按 basename 索引会把**其他目录同名图**的引用者误算进来，导致大量误判人工。"""
    idx = collections.defaultdict(set)
    for md, txt in ref_map.items():
        for m in MD_IMG_ANY.finditer(txt):
            url = m.group(2).strip()
            if url.startswith(("http", "data:")):
                continue
            rel = url.split("#")[0]
            if not rel:
                continue
            try:
                p = (md.parent / rel).resolve()
            except Exception:
                continue
            if p.suffix.lower().lstrip(".") in IMG_EXT:
                idx[os.path.normcase(str(p))].add(md)
    return idx


def plan_ref_fix(vault, bad_refs, img_by_name, ref_by_path):
    """非标准引用 → 修复计划。
    返回 dict(edits={md: [(raw, 标准引用)]}, file_ops=[(src, dst, act)], issues=[(tag, desc)])，
    完全无可行动作则 None。修复动作：图移入/改名于引用笔记同目录 images/，引用统一 `![](images/<名>)`。
    安全约束：
      · wikilink/断链按 basename 全库定位，不唯一 → 人工（同名图两份，猜错必断链）
      · 图改名/移动的前提：其全部引用笔记都在本次改写集合内（否则断未收录笔记的链）
      · 同图被多个目录的笔记引用 → 人工（移动无法同时满足各处的 images/ 形态）"""
    items = []                                   # (md, item, Path|None, 说明)
    for md, lst in bad_refs.items():
        for it in lst:
            kind, tgt = it["kind"], it["target"]
            p, note = None, ""
            if kind == "md-broken":
                hits = img_by_name.get(os.path.basename(tgt.split("#")[0]).lower(), [])
                if len(hits) == 1:
                    p = hits[0]
                elif len(hits) > 1:
                    note = f"库内同名图 {len(hits)} 个，无法唯一定位"
                else:
                    note = "全库找不到该图，疑似已丢失"
            elif kind.startswith("wk"):
                hits = img_by_name.get(os.path.basename(tgt).lower(), [])
                if len(hits) == 1:
                    p = hits[0]
                elif len(hits) > 1:
                    note = f"库内同名图 {len(hits)} 个，wikilink 无法唯一定位"
                else:
                    note = "库内找不到目标图"
            else:                                # md-alt / md-space：先按相对路径找
                p = md.parent / tgt.split("#")[0]
                if not p.exists():
                    p = None
                    hits = img_by_name.get(os.path.basename(tgt).lower(), [])
                    if len(hits) == 1:
                        p, note = hits[0], "引用路径上无图，已按全库唯一同名定位"
                    elif len(hits) > 1:
                        note = "引用路径上无图，且库内同名图多个"
                    else:
                        note = "引用路径上无图，全库亦未命中"
            items.append((md, it, p, note))

    # 图级聚合：引用覆盖检查（动文件不能断未收录笔记的链）
    ok = collections.defaultdict(set)            # normcase(路径) -> {md}
    paths = {}
    for md, it, p, _note in items:
        if p is not None:
            k = os.path.normcase(str(p))
            ok[k].add(md)
            paths[k] = p
    covered = {md for md, _it, p, _n in items if p is not None}
    final_name, file_ops, issues = {}, [], []
    for k, p in paths.items():
        users = ok[k] | ref_by_path.get(k, set())
        if not users <= covered:
            issues.append(("人工", f"{p.relative_to(vault)}：被 {len(users - covered)} 篇未收录笔记引用，"
                                   "动文件会断它们的链"))
            continue
        parents = {md.parent for md in ok[k]}
        if len(parents) != 1:
            issues.append(("人工", f"{p.relative_to(vault)}：被 {len(parents)} 个目录的笔记引用，"
                                   "移动无法同时满足各处 `![](images/…)` 形态"))
            continue
        img_dir = next(iter(parents)) / "images"
        new_name = p.name.replace(" ", "_")
        dst = img_dir / new_name
        if p != dst and dst.exists():
            issues.append(("跳过", f"目标已存在：{dst.relative_to(vault)}"))
            continue
        if p != dst:
            file_ops.append((p, dst, "move" if p.parent != img_dir else "rename"))
        final_name[k] = new_name

    edits = collections.defaultdict(list)
    for md, it, p, note in items:
        if p is None:
            issues.append(("人工", f"{md.relative_to(vault)} ← {it['raw'][:70]}：{note}"))
            continue
        k = os.path.normcase(str(p))
        if k in final_name:
            std = f"![](images/{final_name[k]})"
            if it["raw"] != std:             # 图移到位后文本恰好已标准 → 无需改写
                edits[md].append((it["raw"], std))
    if not edits and not file_ops:
        return None
    return {"edits": dict(edits), "file_ops": file_ops, "issues": issues}


def run_ref_fix(vault, plan, ref_map, bak):
    """执行引用规范化：文件操作（备份后 move/rename）→ 改写 md → 复检断链。返回统计 dict。"""
    edits, file_ops = plan["edits"], plan["file_ops"]
    moves = renames = 0
    for src, dst, act in file_ops:
        bak(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moves += (act == "move")
        renames += (act == "rename")
        print(f"  ✓ [{act}] {src.relative_to(vault)}  →  {dst.relative_to(vault)}")
    changed = 0
    for md, pairs in sorted(edits.items()):
        txt = ref_map.get(md)
        if txt is None:
            txt = md.read_text(encoding="utf-8", errors="ignore")
        new = txt
        for raw, std in pairs:
            new = new.replace(raw, std)
        if new != txt:
            bak(md)
            md.write_text(new, encoding="utf-8")
            changed += 1
            print(f"  ✓ [改写] {md.relative_to(vault)}（{len(pairs)} 处）")
    miss = 0
    for md, pairs in edits.items():
        for _raw, std in pairs:
            m = MD_IMG_ANY.match(std)
            url = m.group(2) if m else ""
            if url and not (md.parent / url.split("#")[0]).exists():
                miss += 1
                print(f"  ✗ 修复后断链：{md.relative_to(vault)} → {std}")
    print(f"[引用] 文件操作 {len(file_ops)} 个（移动 {moves} / 改名 {renames}）；"
          f"改写 {changed} 篇；修复后断链 {miss}" + ("  ✓" if not miss else "  ⚠ 请检查"))
    return {"ops": len(file_ops), "moves": moves, "renames": renames,
            "changed": changed, "miss": miss}


def build_format_plan(vault, bad, ref_files):
    """不可显示格式图片 → 转换计划（沿用 2026-09-15 口径：默认 jpg，透明/少色 png）"""
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
    return plan


def print_format_plan(vault, plan):
    for x in plan:
        tag = "被引" if x["cited"] else "孤儿"
        print(f"  [{tag}] {x['src'].relative_to(vault)}  →  {x['dst'].name if x['dst'] else '?'}   ({x['note']})"
              + (f"  引用于 {len(x['cited'])} 篇" if x["cited"] else ""))
    n_cited = sum(1 for x in plan if not x["orphan"])
    print(f"\n合计：{len(plan)} 个（被引 {n_cited} / 孤儿 {len(plan) - n_cited}；"
          f"{sum(x['src'].stat().st_size for x in plan) / 1024:.0f}KB）")


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
    bootstrap(("yaml", "cv2", "numpy", "PIL"))
    ap = argparse.ArgumentParser(description="题库图片巡检与修复（不可显示格式 + 非标准引用形态）")
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

    bad, ref_map, ref_files, name_count, img_by_name = scan(vault)
    print(f"[md] 扫描 {len(ref_map)} 篇笔记，库内图片 {sum(name_count.values())} 个")

    bad_refs = scan_refs(ref_map, img_by_name)

    # ---- 巡检报告 ----
    if not bad and not bad_refs:
        print("\n✅ 图片格式与引用形态全部规范——无需修复")
        return

    if bad:
        print(f"\n发现 {len(bad)} 个不可显示格式的图片（Obsidian 不显示 tiff/emf/wmf 等）：")
        plan = build_format_plan(vault, bad, ref_files)
        print_format_plan(vault, plan)
    else:
        print("\n✅ 未发现 Obsidian 不显示的图片格式（tiff/emf/wmf 等）")
        plan = []

    if bad_refs:
        desc = {"wk-bare":   "![[粘贴图.png]]（Obsidian 粘贴默认形态，无路径 wikilink）",
                "wk-path":   "![[路径/x.jpg|314]]（带路径/尺寸 wikilink）",
                "md-alt":    "![说明](images/x.jpg)（alt 非空）",
                "md-space":  "![](images/含 空格.png)（文件名含空格）",
                "md-broken": "![](images/x.jpg)（标准形态但图不在引用路径上）"}
        cnt = collections.Counter(it["kind"] for lst in bad_refs.values() for it in lst)
        print(f"\n发现 {sum(cnt.values())} 处非标准图片引用（{len(bad_refs)} 篇）——"
              f"export 脚本出卷时全部丢图：")
        for k in ("wk-bare", "wk-path", "md-alt", "md-space", "md-broken"):
            if cnt.get(k):
                print(f"  {k:<10}{cnt[k]:>4} 处   {desc[k]}")
        for md in sorted(bad_refs):
            kc = collections.Counter(it["kind"] for it in bad_refs[md])
            print(f"    {md.relative_to(vault)}  "
                  + "  ".join(f"{k}×{n}" for k, n in sorted(kc.items())))

    if not a.fix:
        print("\n（当前为巡检模式。加 --fix 看修复计划，加 --fix --apply 真正执行）")
        return

    # ---- 修复计划 ①：引用形态 ----
    ref_plan = None
    if bad_refs:
        ref_by_path = build_ref_by_path(ref_map)
        ref_plan = plan_ref_fix(vault, bad_refs, img_by_name, ref_by_path)
    if ref_plan:
        e_md, f_ops = ref_plan["edits"], ref_plan["file_ops"]
        n_move = sum(1 for *_x, act in f_ops if act == "move")
        print(f"\n[引用计划] 改写 {len(e_md)} 篇笔记；文件操作 {len(f_ops)} 个"
              f"（移动 {n_move} / 改名 {len(f_ops) - n_move}）")
        for src, dst, act in f_ops:
            print(f"  {act:<7} {src.relative_to(vault)}  →  {dst.relative_to(vault)}")
        if ref_plan["issues"]:
            print(f"  ⚠ 需人工处理 {len(ref_plan['issues'])} 项：")
            for tag, d in ref_plan["issues"]:
                print(f"     [{tag}] {d}")
    elif bad_refs:
        print("\n[引用计划] 无可自动修复项（全部需人工，见上）")

    # ---- 修复计划 ②：图片格式 ----
    conflict = [x for x in plan if x["dst"] and x["dst"].exists()]
    ready = [x for x in plan if x["dst"] and not x["dst"].exists()]
    touched = {md for x in ready for md in x["cited"]}
    if plan:
        print(f"\n[格式计划] 转换 {len(ready)} 个；涉及引用 {len(touched)} 篇笔记；"
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

    # ---- 执行 ①：引用形态修复（先做：规范化后的引用才能被格式修复正确改写）----
    ref_stat = None
    if ref_plan:
        ref_stat = run_ref_fix(vault, ref_plan, ref_map, bak)
        # 文件已变，重扫刷新格式修复的输入
        bad, ref_map, ref_files, name_count, img_by_name = scan(vault)
        plan = build_format_plan(vault, bad, ref_files)
        ready = [x for x in plan if x["dst"] and not x["dst"].exists()]

    # ---- 执行 ②：图片格式转换 ----
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
    if done:
        print(f"[引用] 改写 {changed} 篇笔记")

    # 6) 复检：新图全部可读、引用零失效（无格式转换时跳过——存量断链已在巡检报告里单列）
    broken = []
    for x in done:
        if not x["dst"].exists():
            broken.append(str(x["dst"]))
    miss = 0
    if done:
        all_md = {p: p.read_text(encoding="utf-8", errors="ignore") for p in ref_map}
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
    lines = [f"题库图片修复台账（引用形态规范化 + 格式转换）  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"题库：{vault}", f"备份：{backup}", ""]
    if ref_stat:
        lines += ["── 引用形态规范化 ──",
                  f"文件操作 {ref_stat['ops']} 个（移动 {ref_stat['moves']} / 改名 {ref_stat['renames']}）；"
                  f"改写 {ref_stat['changed']} 篇；修复后断链 {ref_stat['miss']}",
                  ""]
    lines += [f"{x['src'].name:<20} → {x['dst'].name:<20} {x['note']:<22} "
              f"{'被引' if not x['orphan'] else '孤儿'}" for x in done]
    lines += ["", f"转换 {len(done)} 个；改写 {changed} 篇；原图"
              f"{'保留' if a.keep_original else '已入回收站'}",
              f"未处理（目标已存在/解码失败）：{[x['src'].name for x in plan if x not in done] or '无'}"]
    ledger.write_text("\n".join(lines), encoding="utf-8")
    print(f"[台账] {ledger}")


if __name__ == "__main__":
    main()
