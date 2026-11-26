# -*- coding: utf-8 -*-
"""find_better_images.py — 低质图筛查 + 开放图库候选检索（网络补图辅助，只读）

定位：**只读辅助**，绝不自动替换图片。完整链条是「筛查 → 检索候选 → 人工挑用 →
（人工或 fix_image_formats.py）换图」。换图会改变题目的史料载体，必须逐张人审。

① 筛查（`--卷` / `--全库`）：按门槛列出低质图，附所在卷 / 题号 / 图注（图注即天然检索词）
    严格档：短边 <120px 或 总像素 <4 万     （明显低质，优先处理）
    宽松档（默认）：短边 <200px 或 总像素 <12 万
② 检索（`--查 <词>` / `--查表 <文件>` / `--分类 <Category>`）：对检索词查 Wikimedia Commons，
    出候选（文件名 / 分辨率 / 许可 / URL）。**英文词命中率显著高于中文**（2026-10-09 实测），
    建议中英双轨、英文优先。无命中的改走 WebSearch 定向找博物馆官网等。

    检索内置 `filetype:bitmap` 过滤——中文词裸查会被 CADAL 批量上传的 djvu/pdf 古籍扫描
    淹没（实测「杜岭方鼎」前 3 条全是 djvu 且靠单字误命中），加该过滤后清零
    （2026-11-07 实测）。候选按面积降序，可用 `--最小边` / 检索词行内原图尺寸再筛。

    `--分类` 直接列某 Category 的成员文件（如「Longmen Grottoes」500+ 张），
    用于「已知器物在某馆某分类下」的定向找图；分类无文件时列出子分类名。

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
  #   检索词.txt 每行一个词，支持「中文 | English」用竖线分隔双轨；
  #   第三段可写原图尺寸（如「贾湖骨笛 | Jiahu bone flute | 232x146」），
  #   写了就只保留面积 ≥ 原图 --倍率 倍的候选（默认 1.5）
  python find_better_images.py --查 "妇好鸮尊" --最小边 800

  # ③ 按分类找图（已知器物在某馆/某遗址分类下）
  python find_better_images.py --分类 "Longmen Grottoes" --分类 "Yinxu"

输出默认打印到 stdout，`--out` 写 markdown 文件。
"""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import qb_schema
from qb_bootstrap import bootstrap, try_import

PIL_Image = try_import("PIL.Image")   # 可选：缺 Pillow 时降级为「只看体积」

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia 官方要求 UA 带可联系信息（否则可能被限流/封禁）
UA = "tnote-find-better-images/1.1 (Obsidian history question bank image audit; contact: young5960@agent.qq.com)"
# 位图扩展名白名单：Commons 的 namespace 6 同时装 djvu/pdf/ogg/svg，只留能直接进题库的
BITMAP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}

# 门槛：严格 / 宽松 → (最小短边(px), 最小总像素)
# ⚠ 用「短边 / 总像素」判分辨率，不用体积——线稿、表格、时间轴这类图压缩率高、体积天然小，
#    按体积判会把「宽扁的线框图」（实测 1334x268 的主题一时间轴）误报为低质（2026-10-09）。
THRESHOLDS = {
    "严格": (120, 40_000),
    "宽松": (200, 120_000),
}
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
# 卷目录所在类别（scan 与 vol_dir 共用，勿两处各写一份）
# 题根目录名由 qb_schema 统一登记（2026-11-26 起不再本地复制一份，避免两处判断打架）
CATEGORY_SEGS = ([f"{s}-{c}" for s, cats in qb_schema.CATEGORIES.items() for c in cats]
                 + [qb_schema.CASE_ROOT])
MD_IMG = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)\)")
# 选项行（如「(A) 历史价值  (B) 科技价值 …」）——选择题图片下一行常是它，不能当图注
OPT_LINE = re.compile(r"[（(][A-D][)）]")


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
                    if "![" not in s and not s.startswith("|") and not OPT_LINE.search(s):
                        cap[name] = s
                    break
    return cap


def vol_dir(vault, vol):
    """按卷名定位卷目录（`{vault}/{类别}/{卷名}`）；找不到返回 None。"""
    root = Path(vault)
    for seg in CATEGORY_SEGS:
        p = root / seg / vol
        if p.is_dir():
            return p
    return None


def scan(vault, vols=None, level="宽松"):
    min_short, min_area = THRESHOLDS[level]
    root = Path(vault)
    target_dirs = []
    for seg in CATEGORY_SEGS:
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
                if w and h:
                    low = min(w, h) < min_short or w * h < min_area
                else:                     # 缺 Pillow → 降级：只看极端小体积
                    low = sz < 5 * 1024
                if low:
                    rows.append({
                        "卷": vol, "类别": seg, "题型目录": sub.name,
                        "文件": f.name, "字节": sz, "尺寸": f"{w}x{h}" if w else "-",
                        "图注": caps.get(f.name, ""),
                    })
    return rows


# ------------------------------------------------------------
# ② 检索（Wikimedia Commons）
# ------------------------------------------------------------
def _fetch(url, retry, interval):
    """统一请求：429 / SSL 超时按 interval 递增退避重试。"""
    last = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r), None
        except Exception as e:            # 429 / SSL 握手超时 / 连接重置
            last = str(e)
            time.sleep(interval * (attempt + 1) * 2)   # 退避：2×、4×、6×…
    return None, last


def _parse_pages(data, min_side=0, min_area=0):
    """把 API 返回的 pages 转成候选列表，滤掉非位图与过小图，按面积降序。"""
    out = []
    for _, v in (data.get("query", {}).get("pages", {}) or {}).items():
        ii = (v.get("imageinfo") or [{}])[0]
        url = ii.get("url", "")
        title = v["title"].replace("File:", "")
        ext = os.path.splitext(title)[1].lower()
        if ext and ext not in BITMAP_EXT:          # djvu/pdf/ogg 一律剔
            continue
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if not w or not h:                          # 音频/无尺寸文件
            continue
        if min_side and min(w, h) < min_side:
            continue
        if min_area and w * h < min_area:
            continue
        em = ii.get("extmetadata", {})
        out.append({
            "title": title,
            "w": w, "h": h, "area": w * h,
            "size": f"{w}x{h}",
            "lic": em.get("LicenseShortName", {}).get("value", "?"),
            "url": url,
        })
    out.sort(key=lambda r: -r["area"])
    return out


def _relevance(title, kw):
    """候选文件名与检索词的词重合度（0~1）。

    Commons 全文检索的相关性排序弱：查「Water and Land Battle bronze hu」会返回
    山彪1号墓空首布这类只沾边词的文件。重合度低的不是不能用（英文名可能不同），
    只是**必须重点核对**，故只做标注、不做剔除。
    """
    t = title.lower()
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", kw)]
    zh = re.sub(r"[^\u4e00-\u9fff]", "", kw)
    frags = {zh[i:i + 2] for i in range(len(zh) - 1)} if len(zh) >= 2 else set()
    total = len(words) + len(frags)
    if not total:
        return 1.0
    hit = sum(1 for w in words if w in t) + sum(1 for f in frags if f in t)
    return hit / total


def commons_search(kw, limit, retry, interval, min_side=0, min_area=0):
    """按关键词检索。gsrsearch 追加 filetype:bitmap（中文裸查会被 CADAL 古籍 djvu 淹没）。"""
    q = urllib.parse.quote(f"{kw} filetype:bitmap")
    url = (f"{COMMONS_API}?action=query&generator=search&gsrsearch={q}&gsrnamespace=6"
           f"&gsrlimit={max(limit * 4, 12)}&prop=imageinfo&iiprop=url|size|extmetadata&format=json")
    data, err = _fetch(url, retry, interval)
    if err:
        return [], err
    time.sleep(interval)                  # 限流：Commons 查询过密会返回 429
    out = _parse_pages(data, min_side, min_area)
    # 纯中文词检索到的文件多为英文命名，文件名比对无意义 → 不标（渲染为「—」）
    if re.search(r"[A-Za-z]{3,}", kw):
        for r in out:
            r["rel"] = _relevance(r["title"], kw)
    return out[:limit], None


def commons_category(cat, limit, retry, interval, min_side=0, min_area=0):
    """列 Category 成员文件；该分类只有子分类时，返回子分类名供二次检索。"""
    cat = cat[9:] if cat.lower().startswith("category:") else cat
    q = urllib.parse.quote(f"Category:{cat}")
    url = (f"{COMMONS_API}?action=query&generator=categorymembers&gcmtitle={q}"
           f"&gcmtype=file&gcmlimit=50&prop=imageinfo&iiprop=url|size|extmetadata&format=json")
    data, err = _fetch(url, retry, interval)
    if err:
        return [], [], err
    time.sleep(interval)
    files = _parse_pages(data, min_side, min_area)[:limit]
    if files:
        return files, [], None
    # 无直接文件 → 列子分类（Commons 常把内容放在子分类里）
    url2 = (f"{COMMONS_API}?action=query&list=categorymembers&cmtitle={q}"
            f"&cmtype=subcat&cmlimit=40&format=json")
    data2, err2 = _fetch(url2, retry, interval)
    time.sleep(interval)
    subs = [m["title"].replace("Category:", "") for m in
            (data2 or {}).get("query", {}).get("categorymembers", [])]
    return [], subs, (err2 if not subs else None)


def render_report(rows, level, vault):
    lines = [f"# 图片体检（门槛：{level}）", "",
             f"- 题库：`{vault}`",
             f"- 判定：短边 <{THRESHOLDS[level][0]}px 或 总像素 <{THRESHOLDS[level][1]:,}",
             f"- 合计：**{len(rows)} 张**（「判定」列由人工填写，四值见下）", "",
             "| 卷 | 题型 | 文件 | 尺寸 | 体积 | 图注（检索词） | 判定 | 依据／来源 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['卷']} | {r['题型目录'].split('_')[-1]} | {r['文件']} | "
                     f"{r['尺寸']} | {r['字节'] // 1024}KB | {r['图注'] or '—'} | | |")
    lines += ["",
              "**判定四值（必填，勿自造）**：",
              "",
              "- `已换`——已换成同物高清图（依据栏写来源与许可，如「Commons Banliang 3888x2592 CC0」）",
              "- `保留-原创`——原卷示意图/流程图/统计图表，**禁换**（换了破坏题目）",
              "- `保留-本底`——历史照片/器物照，但该件本身无更好源（**原图即上限**，非疏漏）",
              "- `待补`——该换且已有线索但尚未落地（依据栏写去哪儿找）",
              "",
              "> ⚠ 可替换范围仅限：文物/器物照片、历史地图、书影名画、人物肖像。",
              "> 原卷原创示意图、统计数据图表、带版式水印的截图**不要换**；",
              "> 检索到的候选**必须人工确认是否为同一件**（同名会返回同物不同角度／异地同类器物）。"]
    return "\n".join(lines)


def render_candidates(pairs, limit, retry, interval, min_side, ratio):
    """pairs 项：{"kw": 检索词, "orig": (w,h) 或 None, "tag": 中文词标注原文}"""
    lines = ["# 开放图库候选（Wikimedia Commons）", "",
             f"- 检索已自动过滤非位图（djvu/pdf/ogg）；候选按面积降序"
             + (f"；最小短边 {min_side}px" if min_side else ""),
             "- ⚠ 候选**必须人工确认是同一件**（同名会返回同物不同角度／异地同类器物）", ""]
    for it in pairs:
        kw, orig = it["kw"], it.get("orig")
        min_area = int(orig[0] * orig[1] * ratio) if orig else 0
        res, err = commons_search(kw, limit, retry, interval, min_side, min_area)
        head = f"## {kw}"
        if orig:
            head += f"　（原图 {orig[0]}x{orig[1]}，仅列 ≥{ratio:g}× 面积）"
        lines.append(head)
        if err:
            lines.append(f"- 查询失败：{err}")
        elif not res:
            lines.append(f"- 无命中（改走 WebSearch 定向找博物馆官网等）")
        else:
            lines.append("")
            header = ["候选文件", "分辨率", "许可", "相关"]
            if orig:
                header.append("相对原图")
            header.append("下载")
            lines.append("| " + " | ".join(header) + " |")
            lines.append("|" + "---|" * len(header))
            base = orig[0] * orig[1] if orig else 0
            for r in res:
                rel = r.get("rel")
                row = [r["title"], r["size"], r["lic"],
                       "—" if rel is None else
                       ("✓" if rel >= 0.4 else "⚠ 弱（须重点核对）")]
                if orig:
                    row.append(f"{r['area'] / base:.0f}×")
                row.append(f"[直链]({r['url']})")
                lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return "\n".join(lines)


def render_categories(cats, limit, retry, interval, min_side, min_area):
    lines = ["# Wikimedia Commons 分类图（定向找图）", ""]
    for cat in cats:
        files, subs, err = commons_category(cat, limit, retry, interval, min_side, min_area)
        lines.append(f"## Category:{cat}")
        if err:
            lines.append(f"- 查询失败：{err}")
        elif files:
            lines.append("")
            lines.append("| 候选文件 | 分辨率 | 许可 | 下载 |")
            lines.append("|---|---|---|---|")
            for r in files:
                lines.append(f"| {r['title']} | {r['size']} | {r['lic']} | [直链]({r['url']}) |")
        elif subs:
            lines.append("- 该分类下无直接文件，子分类如下（用 `--分类` 逐个再查）：")
            lines.append("")
            for s in subs:
                lines.append(f"  - {s}")
        else:
            lines.append("- 该分类下无文件、也无子分类（换英文分类名或先用 `--查` 试词）")
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
                    help="严格＝短边<120px 或 总像素<4万；宽松（默认）＝短边<200px 或 总像素<12万")
    ap.add_argument("--查", action="append", default=[], help="检索词（可重复）")
    ap.add_argument("--查表", help="检索词文件（每行一个，支持「中文 | English」双轨，"
                                  "可选第三段原图尺寸如 232x146，写了就只留 ≥倍率 面积的候选）")
    ap.add_argument("--分类", action="append", default=[],
                    help="列某 Wikimedia Commons 分类的成员文件（可重复，无需写 Category: 前缀）")
    ap.add_argument("--条数", type=int, default=3, help="每个词返回候选条数（默认 3）")
    ap.add_argument("--最小边", type=int, default=0, help="候选最小短边像素（默认 0 不限）")
    ap.add_argument("--倍率", type=float, default=1.5,
                    help="检索词带原图尺寸时，候选面积须 ≥ 原图×该倍率（默认 1.5）")
    ap.add_argument("--间隔", type=float, default=2.5,
                    help="查询间隔秒（默认 2.5；1.5 实测 30 次查询 7 次 429）")
    ap.add_argument("--重试", type=int, default=3, help="失败重试次数（默认 3，间隔逐次翻倍）")
    ap.add_argument("--留痕", action="store_true",
                    help="体检表直接写到该卷目录的 _图片体检.md（须与单个 --卷 同用；"
                         "无低质图时不生成文件）")
    ap.add_argument("--out", help="输出 markdown 文件（默认打印到 stdout）")
    ap.add_argument("--vault", help="临时指定题库库路径")
    args = ap.parse_args()

    # 参数自校验（须排在 require_vault 之前，否则未配置库时会掩盖真正的参数报错）
    if not any([args.卷, args.全库, args.查, args.查表, args.分类]):
        ap.error("至少指定一项：--卷 / --全库（筛查） 或 --查 / --查表 / --分类（检索）")
    if args.留痕 and len(args.卷) != 1:
        ap.error("--留痕 须与且仅与一个 --卷 同用（一次只给一卷写体检表）")

    bootstrap(vault=args.vault)                      # 依赖预检 + 管道 UTF-8
    from qb_bootstrap import require_vault
    vault = require_vault(args.vault)                # 参数校验之后再取库路径

    # 留痕模式：只出单卷体检表、写入卷目录，不掺检索结果
    if args.留痕:
        vd = vol_dir(vault, args.卷[0])
        if vd is None:
            ap.error(f"题库里找不到卷目录：{args.卷[0]}")
        rows = scan(vault, vols=args.卷, level=args.门槛)
        if not rows:
            print(f"[留痕] {args.卷[0]} 无低质图（门槛：{args.门槛}），不生成 _图片体检.md")
            return
        target = vd / "_图片体检.md"
        target.write_text(render_report(rows, args.门槛, vault), encoding="utf-8")
        print(f"[写出] {target}（{len(rows)} 张，判定列待填）")
        return

    chunks = []
    if args.卷 or args.全库:
        rows = scan(vault, vols=args.卷 or None, level=args.门槛)
        chunks.append(render_report(rows, args.门槛, vault))

    pairs = [{"kw": w, "orig": None} for w in args.查]
    if args.查表:
        for ln in Path(args.查表).read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|") if p.strip()]
            orig = None
            m = re.fullmatch(r"(\d+)\s*[x×*]\s*(\d+)", parts[-1]) if parts else None
            if m and len(parts) > 1:          # 第三段＝原图尺寸 → 作过滤门槛
                orig = (int(m.group(1)), int(m.group(2)))
                parts = parts[:-1]
            pairs.extend({"kw": p, "orig": orig} for p in parts)
    if pairs:
        chunks.append(render_candidates(pairs, args.条数, args.重试, args.间隔,
                                        args.最小边, args.倍率))
    if args.分类:
        chunks.append(render_categories(args.分类, max(args.条数, 20), args.重试, args.间隔,
                                        args.最小边, 0))

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
