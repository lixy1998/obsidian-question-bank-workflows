# -*- coding: utf-8 -*-
"""find_better_images.py — 低质图筛查 + 开放图库候选检索（网络补图辅助，只读）

定位：**只读辅助**，绝不自动替换图片。完整链条是「筛查 → 检索候选 → 人工挑用 →
（人工或 fix_image_formats.py）换图」。换图会改变题目的史料载体，必须逐张人审。

① 筛查（`--卷` / `--全库`）：按门槛列出低质图，附所在卷 / 题号 / 图注（图注即天然检索词）
    严格档：边长 <150px 或 体积 <6KB      （明显低质，优先处理）
    宽松档（默认）：边长 <300px 或 体积 <20KB
② 检索（`--查 <词>` / `--查表 <文件>`）：对检索词查 Wikimedia Commons，出候选
    （文件名 / 分辨率 / 许可 / URL）。**英文词命中率显著高于中文**（2026-10-09 实测），
    建议中英双轨、英文优先。无命中的改走 WebSearch 定向找博物馆官网等。

⚠ 三类**不要换**（换了破坏题目）：原卷原创示意图/流程图、统计数据图表、带试卷版式水印的截图。
⚠ 同名检索会返回「同物不同角度」或「异地同类器物」——候选**必须人工确认是同一件**。
⚠ 可替换类别仅限：文物/器物实物照片、历史地图、书影名画、人物肖像（时政新闻图基本无解）。

用法：
  # ① 筛查（只读）
  python find_better_images.py --卷 2026洛阳二模
  python find_better_images.py --全库 --门槛 严格 --out <工作区>/低质图清单.md

  # ② 查候选
  python find_better_images.py --查 "应县木塔" --查 "Pagoda of Fogong Temple"
  python find_better_images.py --查表 检索词.txt --out <工作区>/候选清单.md
  #   检索词.txt 每行一个词，支持「中文 | English」用竖线分隔双轨

输出默认打印到 stdout，`--out` 写 markdown 文件。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from qb_bootstrap import bootstrap, try_import

PIL_Image = try_import("PIL.Image")   # 可选：缺 Pillow 时降级为「只看体积」

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
UA = "tnote-find-better-images/1.0 (Obsidian history question bank image audit)"

# 门槛：严格 / 宽松 → (最小边长(px), 最小体积(byte))
THRESHOLDS = {
    "严格": (150, 6 * 1024),
    "宽松": (300, 20 * 1024),
}
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
MD_IMG = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")


# ------------------------------------------------------------
# ① 筛查
# ------------------------------------------------------------
def image_size(p):
    """返回 (宽, 高)；缺 Pillow 或读不出时返回 (0, 0)。"""
    if PIL_Image is None:
        return 0, 0
    try:
        with PIL_Image.open(p) as im:
            return im.size
    except Exception:
        return 0, 0


def collect_captions(note_path):
    """从题目 md 提取 `images/xx` → 紧随其后的图注行。

    题库正文的两类图形态都要覆盖：
      A) 单图   `![](images/06_1.jpg)` 换行后紧跟图注
      B) 多图表 | ![](images/01_1.jpg) | … |  → 图注在表格第 2 行（按列对应）
    """
    try:
        text = note_path.read_text(encoding="utf-8")
    except Exception:
        return {}
    lines = text.split("\n")
    cap = {}
    for i, ln in enumerate(lines):
        for m in MD_IMG.finditer(ln):
            ref = m.group(1)
            name = ref.split("/")[-1]
            if name in cap:
                continue
            if ln.lstrip().startswith("|"):       # 表格行 → 找表格第 2 行同列图注
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                col = [j for j, c in enumerate(cells) if ref in c]
                if col:
                    for k in range(i + 1, min(i + 5, len(lines))):
                        if "---" in lines[k]:
                            continue
                        if lines[k].lstrip().startswith("|"):
                            nxt = [c.strip() for c in lines[k].strip().strip("|").split("|")]
                            j = col[0]
                            if j < len(nxt) and nxt[j]:
                                cap[name] = nxt[j]
                            break
            else:                                  # 普通行 → 下一非空行且不含图片语法
                for k in range(i + 1, min(i + 3, len(lines))):
                    s = lines[k].strip()
                    if not s:
                        continue
                    if "![" not in s and not s.startswith("|"):
                        cap[name] = s
                    break
    return cap


def scan(vault, vols=None, level="宽松"):
    min_px, min_bytes = THRESHOLDS[level]
    root = Path(vault)
    target_dirs = []
    for seg in ["初中-1中考真题", "初中-2中考模拟题", "初中-3阶段测试",
                "高中-1高考真题", "高中-2高考模拟题", "高中-3阶段测试", "4-案例学习"]:
        base = root / seg
        if not base.is_dir():
            continue
        for vol in sorted(os.listdir(base)):
            vp = base / vol
            if not vp.is_dir():
                continue
            if vols and vol not in vols:
                continue
            target_dirs.append((seg, vol, vp))

    rows = []
    for seg, vol, vp in target_dirs:
        for sub in sorted(vp.iterdir()):
            if not sub.is_dir() or not (sub / "images").is_dir():
                continue
            caps = {}
            for note in sub.glob("*.md"):
                caps.update(collect_captions(note))
            for f in sorted((sub / "images").iterdir()):
                if f.suffix.lower() not in IMG_EXT:
                    continue
                sz = f.stat().st_size
                w, h = image_size(f)
                if (w and h and (w < min_px or h < min_px)) or sz < min_bytes:
                    rows.append({
                        "卷": vol, "类别": seg, "题型目录": sub.name,
                        "文件": f.name, "字节": sz, "尺寸": f"{w}x{h}" if w else "-",
                        "图注": caps.get(f.name, ""),
                    })
    return rows


# ------------------------------------------------------------
# ② 检索（Wikimedia Commons）
# ------------------------------------------------------------
def commons_search(kw, limit, retry, interval):
    q = urllib.parse.quote(kw)
    url = (f"{COMMONS_API}?action=query&generator=search&gsrsearch={q}&gsrnamespace=6"
           f"&gsrlimit={limit}&prop=imageinfo&iiprop=url|size|extmetadata&format=json")
    last = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            out = []
            for _, v in data.get("query", {}).get("pages", {}).items():
                ii = (v.get("imageinfo") or [{}])[0]
                em = ii.get("extmetadata", {})
                out.append({
                    "title": v["title"].replace("File:", ""),
                    "size": f"{ii.get('width')}x{ii.get('height')}",
                    "lic": em.get("LicenseShortName", {}).get("value", "?"),
                    "url": ii.get("url", ""),
                })
            time.sleep(interval)          # 限流：Commons 查询过密会返回 429
            return out, None
        except Exception as e:            # SSL 握手超时 / 429 等一律重试
            last = str(e)
            time.sleep(interval * (attempt + 1))
    return [], last


def render_report(rows, level, vault):
    lines = [f"# 低质图清单（门槛：{level}）", "",
             f"- 题库：`{vault}`",
             f"- 判定：{'边长<' + str(THRESHOLDS[level][0]) + 'px 或 体积<' + str(THRESHOLDS[level][1] // 1024) + 'KB'}",
             f"- 合计：**{len(rows)} 张**", "",
             "| 卷 | 题型 | 文件 | 尺寸 | 体积 | 图注（检索词） |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['卷']} | {r['题型目录'].split('_')[-1]} | {r['文件']} | "
                     f"{r['尺寸']} | {r['字节'] // 1024}KB | {r['图注'] or '—'} |")
    lines += ["", "> ⚠ 可替换范围仅限：文物/器物照片、历史地图、书影名画、人物肖像。",
              "> 原卷原创示意图、统计数据图表、带版式水印的截图**不要换**；",
              "> 检索到的候选**必须人工确认是否为同一件**（同名会返回同物不同角度／异地同类器物）。"]
    return "\n".join(lines)


def render_candidates(pairs, limit, retry, interval):
    lines = ["# 开放图库候选（Wikimedia Commons）", ""]
    for kw in pairs:
        res, err = commons_search(kw, limit, retry, interval)
        lines.append(f"## {kw}")
        if err:
            lines.append(f"- 查询失败：{err}")
        elif not res:
            lines.append("- 无命中（改走 WebSearch 定向找博物馆官网等）")
        else:
            lines.append("")
            lines.append("| 候选文件 | 分辨率 | 许可 |")
            lines.append("|---|---|---|")
            for r in res:
                lines.append(f"| {r['title'][:70]} | {r['size']} | {r['lic']} |")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="低质图筛查 + 开放图库候选检索（只读辅助，不自动替换图片）")
    ap.add_argument("--卷", action="append", default=[],
                    help="限定卷名（可重复）；与 --全库 二选一")
    ap.add_argument("--全库", action="store_true", help="扫描全部卷")
    ap.add_argument("--门槛", choices=list(THRESHOLDS), default="宽松",
                    help="严格＝边长<150px 或 <6KB；宽松（默认）＝边长<300px 或 <20KB")
    ap.add_argument("--查", action="append", default=[], help="检索词（可重复）")
    ap.add_argument("--查表", help="检索词文件（每行一个，支持「中文 | English」双轨）")
    ap.add_argument("--条数", type=int, default=3, help="每次检索返回候选条数（默认 3）")
    ap.add_argument("--间隔", type=float, default=1.5, help="查询间隔秒（默认 1.5，防 429）")
    ap.add_argument("--重试", type=int, default=2, help="失败重试次数（默认 2）")
    ap.add_argument("--out", help="输出 markdown 文件（默认打印到 stdout）")
    ap.add_argument("--vault", help="临时指定题库库路径")
    args = ap.parse_args()

    # 参数自校验（须排在 require_vault 之前，否则未配置库时会掩盖真正的参数报错）
    if not args.卷 and not args.全库 and not args.查 and not args.查表:
        ap.error("至少指定一项：--卷 / --全库（筛查） 或 --查 / --查表（检索）")

    bootstrap(vault=args.vault)                      # 依赖预检 + 管道 UTF-8
    from qb_bootstrap import require_vault
    vault = require_vault(args.vault)                # 参数校验之后再取库路径

    chunks = []
    if args.卷 or args.全库:
        rows = scan(vault, vols=args.卷 or None, level=args.门槛)
        chunks.append(render_report(rows, args.门槛, vault))

    pairs = list(args.查)
    if args.查表:
        for ln in Path(args.查表).read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            pairs.extend([p.strip() for p in ln.split("|") if p.strip()])
    if pairs:
        chunks.append(render_candidates(pairs, args.条数, args.重试, args.间隔))

    text = "\n\n".join(chunks)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"[写出] {out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
