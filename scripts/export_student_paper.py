# -*- coding: utf-8 -*-
"""export_student_paper.py — 组卷导出（清单驱动，v4）

主用法：吃一份「组卷清单」→ 出**学生卷**与**命题说明**（md 或 Word）。
清单是**有序的题目引用**（可跨卷），每项可带分值/来源括注/考察角度/选项序；
题目正文实时从题库读取，清单只管「选哪些、什么顺序、怎么标注」。

用法：
  # ① 清单驱动（主用法）
  python export_student_paper.py --清单 组卷清单.yaml --模式 学生 --输出 <out.md>
  python export_student_paper.py --清单 组卷清单.yaml --docx <out.docx>     # 学生卷 Word（默认同出答题卡）
  python export_student_paper.py --清单 组卷清单.yaml --模式 命题说明 --docx 命题说明.docx
  python export_student_paper.py --清单 组卷清单.yaml --体检
  python export_student_paper.py --清单 组卷清单.yaml --均衡选项            # 答案分布诊断 + 重排方案

  # ② 按卷导出（兼容原用法，仅支持学生卷）
  python export_student_paper.py --卷 2024河南中考 --only 01,03
  python export_student_paper.py --卷 "A,B" --only "A:01;B:07" --输出 out.md

清单格式（.yaml/.yml/.json；.txt 则一行一个 id）：
  卷名: 9年级上历史第1-4单元月考        # 必填，用于输出命名与图片命名空间
  标题: 2026—2027学年第一学期　9年级历史上册　第1—4单元阶段测试
  副标题: 姓名：__________　班级：__________　得分：__________
  说明:
    - 满分：100分　　建议用时：45分钟
    - 每小题只有一个选项符合题意，请将所选答案填入卷末答题卡内。
  题目:
    - 2024河南中考-13                    # 纯 id：元数据自动从题库读
    - id: 2026湖北中考-27                # 带组卷元数据（可覆盖题库值）
      分值: 2
      来源: (2026·湖北中考·27)           # 学生卷按 --标来源 决定是否出现在卷面
      角度: 第1课·文明起源——知识卡片式材料判断农业文明起源的多源性
      选项序: [3,1,2,4]                  # 新 A/B/C/D 位依次放**原第**几项，答案自动换算

  兼容 9/21 式 selected.json：顶层为数组、元素带 id/文件 快照，自动识别。

输出：
  md／docx 学生卷 —— 卷头 + 题号重编（1．2．…）+ 题干原文；Word 版按《试卷排版规范》
          成卷：A4、宋体 12pt、固定行距 24pt、选项两项一行、图片居中（图注紧随）、页脚页码域
  md／docx 命题说明 —— 题目来源与考察角度（内部资料，不随试卷印发）：
      一、试卷结构与命题思路（卷面结构／单元配比／能力维度／难度与答案分布）
      二、选择题命题细目　三、非选择题命题细目
      四、参考答案与评分标准　五、组卷处理说明与待办
  图片 —— 统一放 {输出目录}/images/{卷名}/（跨卷同名图不冲突），md 引用同步改写
"""
import argparse
import json
import re
import shutil
import sys

from pathlib import Path

from qb_schema import (VAULT, is_question_file, as_list, split_frontmatter,
                       stage_roots, strip_comments, TYPE_LEGACY_DIRS)

try:
    import yaml
except ImportError:
    yaml = None


# ============================================================================
# 一、题库索引（id → 题目文件）
# ============================================================================

def build_index(verbose: bool = True) -> dict:
    """扫库建索引：`id → 题目文件路径`（id 多值时各项都指向同一文件）。"""
    idx = {}
    for root in stage_roots():
        for p in sorted(root.rglob("*.md")):
            if not is_question_file(p):
                continue
            fm, _, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            for qid in as_list(fm.get("id")):
                idx.setdefault(str(qid).strip(), p)
    if verbose:
        print(f"[索引] {len(idx)} 个 id")
    return idx


def vol_name_of(path: Path) -> str:
    """从题目文件路径取卷名：{vault}/{学段-类别}/{卷名}/…"""
    try:
        rel = path.relative_to(Path(VAULT)).parts
        return rel[1] if len(rel) > 1 else path.parent.name
    except ValueError:
        return path.parent.name


def norm_no(s) -> str:
    """题号归一化：1 与 01 视为同一题号。"""
    s = str(s).strip()
    return str(int(s)) if s.isdigit() else s


def img_ns(vol: str) -> str:
    """卷名 → 图片子目录名（去掉路径非法字符与空白）。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", vol)


# ============================================================================
# 二、清单加载
# ============================================================================

def load_manifest(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"[失败] 清单文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("[失败] 读 YAML 清单需要 pyyaml（venv 已内置）")
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:  # 纯文本：一行一个 id，`#` 起注释
        items = [l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        data = {"卷名": path.stem, "题目": items}
    if data is None:
        raise SystemExit(f"[失败] 清单为空：{path}")
    if isinstance(data, list):                      # 兼容旧 selected.json
        data = {"卷名": path.stem, "题目": data}
    if not isinstance(data, dict):
        raise SystemExit(f"[失败] 清单顶层应为映射或数组，实为 {type(data).__name__}")
    return data


def norm_items(man: dict) -> list:
    """清单条目统一为 dict（str → {'id': …}）。"""
    raw = man.get("题目") or man.get("questions") or []
    if not isinstance(raw, list):
        raise SystemExit("[失败] 清单的「题目」应为列表")
    out = []
    for x in raw:
        if isinstance(x, str):
            out.append({"id": x.strip()})
        elif isinstance(x, dict):
            out.append(dict(x))
        else:
            raise SystemExit(f"[失败] 清单条目格式不支持：{x!r}")
    return out


def resolve_items(items: list, index: dict, strict: bool = True) -> tuple:
    """逐条定位题目文件并读出元数据 → (记录列表, 缺失列表)。

    定位优先级：条目自带「文件」→ id 索引 → 「卷名-题号」拆分兜底。
    """
    ok, miss = [], []
    for i, it in enumerate(items, 1):
        path, why = None, ""
        f = it.get("文件") or it.get("file")
        if f and Path(f).exists():
            path = Path(f)
        else:
            qid = str(it.get("id") or "").strip()
            if not qid:
                miss.append((i, "(缺 id)", "条目既无「id」也无有效「文件」"))
                continue
            path = index.get(qid)
            if path is None and "-" in qid:          # 兜底：卷名-题号
                vol, no = qid.rsplit("-", 1)
                for root in stage_roots():
                    for cand in root.glob(f"{vol}/*/*.md"):
                        if norm_no(cand.stem) == norm_no(no):
                            path = cand
                            break
                    if path:
                        break
                why = "（按 卷名-题号 兜底命中）" if path else ""
        if path is None or not path.exists():
            miss.append((i, it.get("id") or it.get("文件"), "题库中未找到该 id"))
            continue
        fm, body, err = split_frontmatter(path.read_text(encoding="utf-8"))
        ok.append({
            "序号": i, "id": it.get("id") or fm.get("id"),
            "path": path, "vol": vol_name_of(path),
            "fm": fm, "body": strip_comments(body),
            "分值": it.get("分值", it.get("score", fm.get("分值"))),
            "来源括注": it.get("来源", ""), "角度": it.get("角度", ""),
            "选项序": it.get("选项序") or it.get("order"),
            "括号": it.get("括号"),
            "备注": it.get("备注", ""), "yaml_err": err, "定位提示": why,
        })
    if miss and strict:
        print(f"[警告] {len(miss)} 条未定位：")
        for i, qid, why in miss:
            print(f"   第{i}条 {qid} —— {why}")
    return ok, miss


# ============================================================================
# 三、图片打包（跨卷命名空间）
# ============================================================================

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


# ============================================================================
# 四、卷面文本（md / docx 共用）
# ============================================================================

CJK = r"\u4e00-\u9fff"


def clean(t: str) -> str:
    """卷面文本清理：中文之间的空格→全角（并列词/缩排）、中文字符后的半角逗号/分号→全角。

    ⚠ 两处必须保护，否则会毁版面（2026-09-25 实测）：
    ① **作答括号** `（　　）` 内部是全角空格、两侧是括号（非 CJK），会被「删多余空格」清成
       `（）` → 与后补的括号叠成 `（）（　　）`；先整段替换为 PUA 占位 `\\ue002`，末尾还原。
    ② **连续多个空格**要按原数量换回全角（`100分　　建议用时` 是刻意的两字宽间隔），
       只压成一个会丢版式（`分  建` 两个空格一个都不匹配单空格正则，最后被整段删掉）。
    """
    t = re.sub(r"[（(][\u3000 \t]*[）)]", "\ue002", t)          # 保护作答括号
    t = re.sub(r"^\s*>\s?", "", t)                              # md 引用符（题库正文偶带）
    t = re.sub(r"^\s*[-*+]\s+", "", t)                          # md 列表符
    t = t.replace("**", "").replace("__", "")                   # md 强调符
    t = t.replace("\u3000", " ")
    t = re.sub(rf"(?<=[{CJK}])(?P<sp> +)(?=[{CJK}])",
               lambda m: "\u3000" * len(m.group("sp")), t)
    t = re.sub(r"([A-Za-z0-9]) (?=[A-Za-z0-9])", lambda m: m.group(1) + "\ue000", t)
    t = t.replace(" ", "").replace("\ue000", " ")
    t = re.sub(rf"(?<=[{CJK}])[,;]", lambda m: "，" if m.group(0) == "," else "；", t)
    return t.replace("\ue002", "（　　）").strip()


OPT_SPLIT = re.compile(r"\((?:A|Ａ)\)")


def extract_images(body: str) -> tuple:
    """把图片行替换为 `\\x00IMGn\\x00` 占位，并摘出其后的图注行。

    返回 (去图文本, [(图片文件名, [图注行, …]), …])。
    图注判定：图片行后紧跟的非空行；遇到「（1）」「(A)」「——」「材料X」开头即停。
    """
    lines, out, imgs = body.split("\n"), [], []
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*!\[\]\(images/(.+?)\)\s*$", lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        cap, j = [], i + 1
        while j < len(lines):
            s = lines[j].strip()
            if not s:
                j += 1
                continue
            # ⚠ 下一张图片行也必须停 —— 否则会把后续图与图注一并吃成第一张的图注
            if (re.match(r"^!\[", s) or re.match(r"^[（(]\d+[）)]", s)
                    or re.match(r"^[（(][A-DＡ-Ｄ][）)]", s)
                    or s.startswith("——") or re.match(r"^材料[一二三四五六]", s)):
                break
            cap.append(clean(s))
            j += 1
        out.append(f"\ue001IMG{len(imgs)}\ue001")
        imgs.append((m.group(1), cap))
        i = j
    return "\n".join(out), imgs


TBL_SLOT_RE = re.compile(r"\ue003TBL(\d+)\ue003")
HTML_TABLE_RE = re.compile(r"<table>.*?</table>", re.S)
MD_TABLE_RE = re.compile(r"(?m)(?:^\|.*\|\s*$\n?){2,}")


def is_tbl_slot(ln: str) -> bool:
    """是否为表格占位行（`\\ue003TBLn\\ue003`）。"""
    return bool(TBL_SLOT_RE.fullmatch(ln.strip()))


def _html_table_rows(html: str) -> list:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if any(cells):
            rows.append(cells)
    return rows


def _md_table_rows(block: str) -> list:
    rows = []
    for line in block.strip().split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                                  # 分隔行 `|---|---|`
        rows.append(cells)
    return rows


def extract_tables(body: str) -> tuple:
    """摘出表格（HTML `<table>` 与 markdown 管道表）→ (占位文本, [表, …])，表 = [[单元格…]…]。

    ⚠ **必须摘出后另行渲染**：这两种语法都不能直接进 Word。原实现是把它们
    `re.sub` 删掉（只想着别让 md 语法漏到卷面），结果**所有表格题的表格在 Word 卷上凭空消失**——
    2026-09-25 用户实测发现第 1 题漏表，清查题库共 19 题 HTML 表 ＋ 181 题 md 表受影响。
    占位符自带换行，保证独立成行、能被逐行渲染识别。
    """
    tables = []

    def repl(m, is_html):
        rows = _html_table_rows(m.group(0)) if is_html else _md_table_rows(m.group(0))
        if not rows:
            return ""
        tables.append(rows)
        return f"\n\ue003TBL{len(tables) - 1}\ue003\n"

    body = HTML_TABLE_RE.sub(lambda m: repl(m, True), body)
    body = MD_TABLE_RE.sub(lambda m: repl(m, False), body)
    return body, tables


def split_stem_opts(body: str) -> tuple:
    """拆分题干与选项 → (题干行列表, 选项文本列表)。

    表格与图片已由 extract_tables / extract_images 摘成占位符，此处不再处理。
    """
    # 摘掉图片行与空表格行（docx 版图片另行插入）
    b = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body)
    m = re.search(r"\((?:A|Ａ)\).*", b, re.S)
    if m:
        stem_src, opt_src = b[:m.start()], m.group(0)
        opts = [x.strip() for x in re.split(r"\((?:A|Ｂ|B|Ｃ|C|Ｄ|D)\)", opt_src)[1:]]
    else:
        stem_src, opts = b, []
    lines = [clean(s) for s in stem_src.split("\n") if s.strip()]
    return lines, opts


Q_TAIL = ("具有", "是", "有", "为", "表明", "反映", "体现", "说明", "可知", "源于",
          "在于", "得益于", "属于", "指", "目的是", "原因是", "标志是", "可以看出")


IMG_SLOT_RE = re.compile(r"\ue001IMG(\d+)\ue001")


def is_img_slot(ln: str) -> bool:
    """是否为 docx 图片占位行（`\\ue001IMGn\\ue001`）。"""
    return bool(IMG_SLOT_RE.fullmatch(ln.strip()))


def find_answer_slot(lines: list, mode: str = None) -> int:
    """返回应补「（　　）」的行号；-1＝已有无需补，-2＝按清单要求不补。

    启发的顺序：清单显式「括号: 首行|末行|无」→ 否则找**短且以设问词结尾**的行
    （卡片式/条文式材料的设问常在首行，而非末行）→ 兜底最后一个文本行。
    ⚠ **图片占位行与表格占位行都不参与判定**：
      · 图片占位行不排除 → 括号补到图片位置上（实测踩坑）；
      · 表格占位行不排除 → 题干末行是表格时，括号被塞进表格内（2026-09-26 用户实测：
        多图题题干末行为表格，卷面出现「| 图注 |（　　）」）→ 现按「表格前最后一个文本行」补。
    """
    if any("（　　）" in l for l in lines):
        return -1
    if mode == "无":
        return -2
    rows = [i for i, l in enumerate(lines)
            if not is_img_slot(l) and not is_tbl_slot(l)]
    if not rows:
        return -1
    if mode == "首行":
        return rows[0]
    if mode == "末行":
        return rows[-1]
    for i in rows:
        ln = lines[i]
        if len(ln) <= 42 and not ln.endswith("。") and ln.endswith(Q_TAIL):
            return i
    return rows[-1]


def reorder_opts(opts: list, rec: dict) -> tuple:
    """按清单「选项序」重排选项 → (新选项列表, 新答案字母 或 None)。

    「选项序: [3,1,2,4]」＝ 新卷面 A/B/C/D 位依次放**原第** 3、1、2、4 项；
    答案字母同步换算（题库原题不动，仅卷内重排，对应 §3.2 答案分布均衡）。
    """
    seq = rec.get("选项序")
    if not seq or not opts:
        return opts, None
    perm = [int(x) - 1 for x in seq]
    if sorted(perm) != list(range(len(opts))):
        raise SystemExit(f"[失败] 第 {rec['序号']} 条「选项序」不合法：{seq}"
                         f"（应为 1..{len(opts)} 的排列）")
    new = [opts[i] for i in perm]
    old = str(rec["fm"].get("答案", "")).strip()
    if len(old) == 1 and old in "ABCD":
        new_ans = "ABCD"[perm.index("ABCD".index(old))]
    else:
        new_ans = None
    return new, new_ans


def opts_md(opts: list) -> str:
    """选项卷面文本：两项一行、`A．` 全角顿点、项间全角空格×2（《试卷排版规范》§三/§12.4）。"""
    rows = []
    for i in range(0, len(opts) - 1, 2):
        rows.append(f"{'ABCD'[i]}．{opts[i]}\u3000\u3000{'ABCD'[i + 1]}．{opts[i + 1]}")
    if len(opts) % 2:
        rows.append(f"{'ABCD'[len(opts) - 1]}．{opts[-1]}")
    return "\n".join(rows)


def rtype_of(r: dict) -> str:
    return str(r["fm"].get("题型", "")).strip()


def is_choice(r: dict) -> bool:
    return rtype_of(r) == "选择题"


def sub_scores(body: str, total, n: int) -> list:
    """各小问分值：优先取设问末尾的「（X分）」；取不齐则把整题分值均分。"""
    sc = [int(x) for x in re.findall(r"[（(]\s*(\d+)\s*分[^）)]*[）)]", body)]
    if len(sc) == n:                                 # 只在数量吻合时采信，避免误抓材料里的分数
        return sc
    if isinstance(total, int) and total > 0 and n > 0:
        base, rest = divmod(total, n)
        return [base + (1 if i < rest else 0) for i in range(n)]
    return [None] * n


def blanks_for(score, fixed: int = 0) -> int:
    """答题卡：某小问的留白行数。

    fixed > 0 → 用固定行数（命令行 `--答题卡行数`）；
    否则**按分值算**：约 2 分/行、下限 2 行（对齐参考卡「12 分题 ÷ 3 小问 = 每问 2 行」的实测密度）。
    """
    if fixed and fixed > 0:
        return fixed
    if not score or score <= 0:
        return 2
    return max(2, round(score / 2))


def big_title(recs: list, choice: bool) -> str:
    """大题标题（《试卷排版规范》§二）：选择题给「每小题X分，共Y分」，非选择题给总分。"""
    from collections import Counter
    part = [r for r in recs if is_choice(r) == choice]
    tot = sum(int(r["分值"]) for r in part if str(r["分值"]).strip().isdigit())
    if choice:
        per = Counter(str(r["分值"]).strip() for r in part).most_common(1)[0][0]
        if per.isdigit() and int(per) * len(part) == tot:      # 分值统一才写「每小题X分」
            return f"一、选择题（每小题{per}分，共{tot}分）"
        return f"一、选择题（共{len(part)}小题，{tot}分）"
    return f"二、非选择题（共{tot}分）"


def section_marks(recs: list) -> list:
    """返回 [(卷面序号, 该处需插入的大题标题或 None), …]。"""
    marks, switched = [], False
    for n, r in enumerate(recs, 1):
        if n == 1:
            marks.append((n, big_title(recs, is_choice(r))))
            switched = not is_choice(r)
        elif not is_choice(r) and not switched and any(is_choice(x) for x in recs):
            marks.append((n, big_title(recs, False)))
            switched = True
        else:
            marks.append((n, None))
    return marks


# ============================================================================
# 五、md 卷
# ============================================================================

def header_md(man: dict) -> list:
    out = []
    if man.get("标题"):
        out.append(f"# {man['标题']}")
    if man.get("副标题"):
        out.append(man["副标题"])
    notes = man.get("说明") or []
    if isinstance(notes, str):
        notes = [notes]
    if notes:
        out.append("")
        for i, n in enumerate(notes, 1):
            out.append(f"{i}.{n}")          # 半角点，与题目「1．」区分
    return out


def compose_md_body(body: str, rec: dict) -> tuple:
    """md 卷面加工：补作答括号 ＋ 选项重排（保留图片与题干原文，只规范化选项段）。

    → (新正文, 新答案字母 或 None)
    """
    m = re.search(r"\((?:A|Ａ).*", body, re.S)
    if not m:                                     # 非选择题：原样
        return body, None
    head, opt_src = body[:m.start()], m.group(0)
    opts = [x.strip() for x in re.split(r"\((?:A|Ｂ|B|Ｃ|C|Ｄ|D)\)", opt_src)[1:]]
    opts, new_ans = reorder_opts(opts, rec)
    lines = head.split("\n")
    idxs = [i for i, l in enumerate(lines) if l.strip()]
    slot = find_answer_slot([lines[i].strip() for i in idxs], rec.get("括号"))
    if slot >= 0 and idxs:
        tgt = idxs[min(slot, len(idxs) - 1)]
        lines[tgt] = lines[tgt].rstrip() + "（　　）"
    return "\n".join(lines) + opts_md(opts) + "\n", new_ans


def render_md(man: dict, recs: list, mode: str, mark_src: bool) -> str:
    parts = header_md(man)
    if parts:
        parts.append("")
    for n, mark in section_marks(recs):
        if mark:
            parts.append(f"**{mark}**")
            parts.append("")
        r = recs[n - 1]
        body, new_ans = compose_md_body(rewrite_img(r["body"], r["vol"]), r)
        r["_ans"] = new_ans if new_ans else r["fm"].get("答案", "")
        no = f"{n}．"
        if not is_choice(r) and str(r["分值"]).strip().isdigit():
            no += f"（{r['分值']}分）"              # 非选择题分值紧随题号（§四）
        if mark_src and mode == "学生" and r["来源括注"]:
            no += str(r["来源括注"])                 # 来源括注紧跟题号后（§12.1）
        parts.append(f"{no}{body.strip()}")
        if mode == "教师":      # v4：命题说明已独立成文档，本分支不再触达（保留供复用）
            fm = r["fm"]
            tail = [f"**答案**：{r['_ans']}"]
            jx = str(fm.get("解析", "") or "").strip()
            if jx:
                # 解析里可能内嵌图（实测 2024河南中考-01、2025河南中考-01）：
                # 与题干重复的直接去掉（否则教师卷重出一遍），独有的保留并改写命名空间
                # （v1.22.0：多形态识别，按 basename 比对；保留项统一改写标准 md 形态）
                same = {ref.split("/")[-1] for ref, _ in iter_img_refs(r["body"])}

                def _drop_md(m):
                    p = m.group(1).strip().replace("\\", "/")
                    if not _is_img_ref(p):
                        return m.group(0)
                    return "" if p.split("/")[-1] in same else _repl_std(p)

                def _drop_wk(m):
                    p = m.group(1).strip().replace("\\", "/")
                    if not _is_img_ref(p):
                        return m.group(0)
                    return "" if p.split("/")[-1] in same else _repl_std(p)

                jx = MD_IMG_ANY_RE.sub(_drop_md, jx)
                jx = WK_IMG_ANY_RE.sub(_drop_wk, jx)
                jx = jx.strip()
                if jx:
                    tail.append(f"**解析**：{rewrite_img(jx, r['vol'])}")
            if r["来源括注"]:
                tail.append(f"**来源**：{r['来源括注']}")
            if r["角度"]:
                tail.append(f"**考察角度**：{r['角度']}")
            if r["备注"]:
                tail.append(f"**备注**：{r['备注']}")
            parts.append("")
            parts.extend(tail)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ============================================================================
# 五之二、命题说明（题目来源与考察角度）
# ============================================================================

SRC_TYPE_TAGS = {"真题", "模拟卷", "期末卷", "期中卷", "月考卷", "原创题",
                 "教材改编", "中考改编", "阶段测试", "练习卷", "联考卷", "期末"
                 }
CHOICE_COLS = ["A1-识记", "A2-说明", "A3-概括", "B1-比较", "B2-分析", "B3-评价／C 类"]
KP_DROP_RE = re.compile(r"已弃用|待核|存疑|待删|废弃|新教材已")
_LESSON_RE = re.compile(r"第\s*(\d+)\s*课")


def effective_answer(r: dict) -> str:
    """清单「选项序」换算后的答案字母（未重排则取题库答案）。"""
    old = str(r["fm"].get("答案", "")).strip()
    seq = r.get("选项序")
    if not seq or len(old) != 1 or old not in "ABCD":
        return old
    perm = [int(x) - 1 for x in seq]
    if len(perm) != 4 or sorted(perm) != [0, 1, 2, 3]:
        return old
    return "ABCD"[perm.index("ABCD".index(old))]


def short_lesson(name) -> str:
    """课名取「第X课」前缀，用于兼涉标注等紧凑场景。"""
    m = _LESSON_RE.match(str(name).strip())
    return f"第{m.group(1)}课" if m else str(name).strip()


def lesson_no(name):
    m = _LESSON_RE.search(str(name))
    return int(m.group(1)) if m else None


def lesson_cell(r: dict) -> str:
    """考查课：首项＝主考；其余为兼涉（须在材料/设问中可指认，选项不算）。"""
    ls = [x for x in as_list(r["fm"].get("课")) if str(x).strip()]
    if not ls:
        return ""
    head, rest = str(ls[0]).strip(), [short_lesson(x) for x in ls[1:]]
    return head + ("（兼涉" + "、".join(rest) + "）" if rest else "")


def clean_src(r: dict) -> str:
    """题目来源：题库卷名（剔除类型标签后的首项）。"""
    srcs = [str(x).strip() for x in as_list(r["fm"].get("来源"))
            if str(x).strip() and str(x).strip() not in SRC_TYPE_TAGS]
    return srcs[0] if srcs else str(r["vol"])


def angle_cell(r: dict) -> str:
    """考查角度：清单「角度」优先，否则取知识点前两项（剔除内部标注项）。"""
    if r.get("角度"):
        return str(r["角度"])
    kps = [str(x).strip() for x in as_list(r["fm"].get("知识点"))
           if str(x).strip() and not KP_DROP_RE.search(str(x))]
    return "｜".join(kps[:2])


def scope_cell(r: dict) -> str:
    """非选择题考查范围：主考课＋兼涉课（跨册时带上册别，避免「第9课」歧义）。"""
    ls = [str(x).strip() for x in as_list(r["fm"].get("课")) if str(x).strip()]
    if not ls:
        return "—"
    books = [str(x).strip() for x in as_list(r["fm"].get("册别"))]
    head_book = books[0] if books else ""
    rest = []
    for i, c in enumerate(ls[1:], start=1):
        bk = books[i] if i < len(books) else (books[-1] if books else "")
        tag = short_lesson(c)
        if bk and bk != head_book:
            tag = f"{bk}{tag}"
        rest.append(tag)
    return ls[0] + ("（兼涉" + "、".join(rest) + "）" if rest else "")


def ability_cells(r: dict) -> list:
    """关键能力 → 表内文本（多值以「／」连接）。"""
    ks = [str(x).strip() for x in as_list(r["fm"].get("关键能力")) if str(x).strip()]
    return "／".join(ks) if ks else "—"


def ability_bucket(key: str) -> str:
    """关键能力归并到 9 要素六列（C 类合并）。"""
    k = str(key).strip().upper()
    for pre, col in (("A1", "A1-识记"), ("A2", "A2-说明"), ("A3", "A3-概括"),
                     ("B1", "B1-比较"), ("B2", "B2-分析"), ("B3", "B3-评价")):
        if k.startswith(pre):
            return "B3-评价／C 类" if col == "B3-评价" else col
    if k.startswith(("C1", "C2", "C3")):
        return "B3-评价／C 类"
    return ""


def sub_questions(body: str, total=None, override=None, limit: int = 46) -> list:
    """非选择题小问 → [(号, 文字, 分值)]。

    分值口径（与答题卡留白同一套判定）：清单「小问分值」> 正文自带分值之和恰等于卷面分值
    > 按卷面分值均分（避免原卷赋分与本卷赋分不符时细目表失真）。
    """
    txt = re.sub(r"<[^>]+>", " ", body)
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    raw = []
    for p in re.split(r"(?=[（(]\d+[）)])", txt):
        m = re.match(r"^[（(](\d+)[）)]\s*(.*)$", p.strip())
        if not m:
            continue
        seg = m.group(2).strip()
        sc = re.search(r"[（(]\s*(\d+)\s*分[^）)]*[）)]", seg)
        seg = re.split(r"[（(]\s*\d+\s*分", seg)[0].strip().strip("，,。；;、 ")
        if len(seg) > limit:
            seg = seg[:limit] + "…"
        raw.append((m.group(1), seg, int(sc.group(1)) if sc else None))
    n = len(raw)
    if not n:
        return []
    scores = None
    if override and len(override) == n:
        scores = [int(x) for x in override]
    elif str(total).strip().isdigit() and all(x[2] is not None for x in raw) \
            and sum(x[2] for x in raw) == int(total):
        scores = [x[2] for x in raw]
    elif str(total).strip().isdigit():
        base, rest = divmod(int(total), n)
        scores = [base + (1 if i < rest else 0) for i in range(n)]
    return [(a, b, scores[i] if scores else c) for i, (a, b, c) in enumerate(raw)]


def spec_lines(man: dict, recs: list) -> list:
    """生成「命题说明（题目来源与考察角度）」的 md 行列表。"""
    from collections import Counter, OrderedDict
    ch = [r for r in recs if is_choice(r)]
    nc = [r for r in recs if not is_choice(r)]
    tot = sum(int(r["分值"]) for r in recs if str(r["分值"]).strip().isdigit())
    ch_tot = sum(int(r["分值"]) for r in ch if str(r["分值"]).strip().isdigit())
    nc_tot = sum(int(r["分值"]) for r in nc if str(r["分值"]).strip().isdigit())

    out = []
    title = man.get("标题") or man.get("卷名") or "组卷"
    out.append(f"# {title}")
    out.append("")
    out.append("## 命题说明（题目来源与考察角度）")
    out.append("")
    out.append("（内部资料，不随试卷印发）")
    out.append("")

    # ------------------------------ 一、试卷结构与命题思路
    out.append("### 一、试卷结构与命题思路")
    out.append("")
    out.append("**1．卷面结构**")
    out.append("")
    if ch:
        per = Counter(str(r["分值"]).strip() for r in ch).most_common(1)[0][0]
        if per.isdigit() and int(per) * len(ch) == ch_tot:
            ch_desc = f"选择题 {len(ch)} 小题（每小题 {per} 分，共 {ch_tot} 分）"
        else:
            ch_desc = f"选择题 {len(ch)} 小题（共 {ch_tot} 分）"
    else:
        ch_desc = "本卷不含选择题"
    nc_desc = (f"非选择题 {len(nc)} 小题（共 {nc_tot} 分）" if nc else "本卷不含非选择题")
    head_note = ""
    for n in (man.get("说明") or []):
        if re.search(r"满分|用时|时间", str(n)):
            head_note = f"（卷头注明「{str(n).strip()}」）"
            break
    out.append(f"本卷共 {len(recs)} 小题，满分 {tot} 分。{ch_desc}；{nc_desc}。{head_note}")
    out.append("")

    # 2．各部分比例安排（按单元）
    out.append("**2．各部分比例安排**")
    out.append("")
    groups = OrderedDict()
    for r in recs:
        us = [str(x).strip() for x in as_list(r["fm"].get("单元")) if str(x).strip()]
        key = us[0] if us else "（未标注单元）"
        groups.setdefault(key, []).append(r)
    has_unit = any(k != "（未标注单元）" for k in groups)
    if has_unit:
        out.append("| 单元 | 课时 | 选择题 | 分值 | 涵盖课 |")
        out.append("|---|---|---|---|---|")
        for u, rs in groups.items():
            sel = [r for r in rs if is_choice(r)]
            s = sum(int(r["分值"]) for r in rs if str(r["分值"]).strip().isdigit())
            # 课时范围与涵盖课只取**主考课**（首项）——兼涉可能是跨册课（如 7上第9课），会污染范围
            main = [str(as_list(r["fm"].get("课"))[0]).strip()
                    for r in rs if as_list(r["fm"].get("课"))]
            nos = sorted({n for n in (lesson_no(x) for x in main) if n})
            span = (f"第{nos[0]}—{nos[-1]}课" if len(nos) > 1
                    else f"第{nos[0]}课" if nos else "—")
            covered = "、".join(dict.fromkeys(
                x.split(" ", 1)[1] if " " in x else x for x in main)) or "—"
            out.append(f"| {u} | {span} | {len(sel)}题 | {s}分 | {covered} |")
        out.append(f"| 合计 | {len(recs)}题 | {len(ch)}题 | {tot}分 | — |")
        out.append("")

    # 3．学科能力维度
    out.append("**3．学科能力维度（郑林 9 要素）**")
    out.append("")
    bucket = Counter()
    for r in recs:
        for k in as_list(r["fm"].get("关键能力")):
            col = ability_bucket(k)
            if col:
                bucket[col] += 1
    n_b = sum(bucket.values()) or 1
    out.append("| 能力要素 | " + " | ".join(CHOICE_COLS) + " |")
    out.append("|---|" + "---|" * len(CHOICE_COLS))
    out.append("| 题量（含兼涉） | "
               + " | ".join(f"{bucket.get(c, 0)}题" for c in CHOICE_COLS) + " |")
    out.append("| 占比 | "
               + " | ".join(f"{round(bucket.get(c, 0) * 100 / n_b)}%" for c in CHOICE_COLS)
               + " |")
    out.append("")
    top = bucket.most_common(2)
    if top:
        pair = "＋".join(c for c, _ in top)
        share = round(sum(v for _, v in top) * 100 / n_b)
        out.append(f"以 {pair} 为主，合计约 {share}%（含兼涉口径，一题多要素重复计入）。")
        out.append("")

    # 4．难度与答案分布
    out.append("**4．难度与答案分布**")
    out.append("")
    diff = Counter(str(r["fm"].get("难度", "?")).strip() for r in recs)
    d_desc = "、".join(f"{k} {v} 题" for k, v in
                       sorted(diff.items(), key=lambda kv: "易中难".find(kv[0][:1])))
    out.append(f"难度：{d_desc}。")
    out.append("")
    if ch:
        ans = Counter(effective_answer(r) for r in ch)
        ans = Counter({k: v for k, v in ans.items() if k in "ABCD"})
        a_desc = "、".join(f"{k} {v} 题" for k, v in sorted(ans.items()))
        seq = [effective_answer(r) for r in ch]
        runs, cur, start = [], 1, 0
        for i in range(1, len(seq)):
            if seq[i] and seq[i] == seq[i - 1]:
                cur += 1
            else:
                runs.append((start + 1, i, seq[start], cur))
                cur, start = 1, i
        runs.append((start + 1, len(seq), seq[start], cur))
        longest = max((x[3] for x in runs), default=1)
        tail = "；最长连续同项 " + "、".join(
            f"第{s}—{e}题 {a}（{n}连）" for s, e, a, n in runs if n == longest)
        long = [x for x in runs if x[3] >= 3 and x[3] != longest]
        if long:
            tail += "，另有 " + "、".join(f"第{s}—{e}题 {a}（{n}连）" for s, e, a, n in long)
        out.append(f"答案分布：{a_desc}{tail}。")
        if len(ch) >= 8 and (max(ans.values()) - min(ans.values())) > max(1, len(ch) * 0.15):
            out.append("")
            out.append("⚠ 分布偏斜，建议用 `--均衡选项` 生成「选项序」后写回清单，再重出本说明。")
        out.append("")

    # 5．重复规避（有「排除卷」信息才出）
    excl = man.get("排除卷") or man.get("去重卷")
    if excl:
        out.append("**5．与已用卷的重复规避**")
        out.append("")
        out.append(f"组卷前已比对：{excl}；本卷题目与该卷无一重复。")
        out.append("")

    # ------------------------------ 二、选择题命题细目
    if ch:
        out.append(f"### 二、选择题命题细目（第 1—{len(ch)} 题）")
        out.append("")
        out.append("| 题号 | 考查课 | 考查角度 | 关键能力 | 难度 | 题目来源 |")
        out.append("|---|---|---|---|---|---|")
        for i, r in enumerate(ch, 1):
            out.append(f"| {i} | {lesson_cell(r)} | {angle_cell(r)} | {ability_cells(r)} | "
                       f"{str(r['fm'].get('难度', '')).strip()} | {clean_src(r)} |")
        out.append("")
        moved = [i for i, r in enumerate(ch, 1) if r.get("选项序")]
        if moved:
            out.append(f"注：第 {'、'.join(map(str, moved))} 题选项顺序已重排"
                       f"（见第五部分）；「考查角度」前列为该题的核心落点。")
        else:
            out.append("注：「考查角度」前列为该题的核心落点。")
        out.append("")

    # ------------------------------ 三、非选择题命题细目
    if nc:
        base = len(ch)
        out.append(f"### 三、非选择题命题细目（第 {base + 1}—{len(recs)} 题）")
        out.append("")
        out.append("| 题号 | 考查范围 | 考查角度 | 设问与分值 | 关键能力 | 难度 | 题目来源 |")
        out.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(nc, base + 1):
            subs = sub_questions(r["body"], r["分值"], r.get("小问分值"))
            desc = " ".join(
                f"({a}) {b}（{c}分）" if c else f"({a}) {b}" for a, b, c in subs) or "—"
            rng = scope_cell(r)
            out.append(f"| {i} | {rng} | {angle_cell(r)} | {desc} | {ability_cells(r)} | "
                       f"{str(r['fm'].get('难度', '')).strip()} | {clean_src(r)} |")
        out.append("")

    # ------------------------------ 四、参考答案与评分标准
    out.append("### 四、参考答案与评分标准")
    out.append("")
    if ch:
        per = Counter(str(r["分值"]).strip() for r in ch).most_common(1)[0][0]
        cap = f"（每小题 {per} 分，共 {ch_tot} 分）" if per.isdigit() else f"（共 {ch_tot} 分）"
        out.append(f"**1．选择题**{cap}")
        out.append("")
        ans = [f"{i}．{effective_answer(r)}" for i, r in enumerate(ch, 1)]
        for k in range(0, len(ans), 10):
            out.append("\u3000".join(ans[k:k + 10]))
        out.append("")
    if nc:
        out.append(f"**2．非选择题**（共 {nc_tot} 分）")
        out.append("")
        for i, r in enumerate(nc, len(ch) + 1):
            a = str(r["fm"].get("答案", "") or "").strip()
            a = re.sub(r"\n{2,}", "\n", a)
            a = a.replace("\n", " ")
            out.append(f"{i}．{a}")
            out.append("")

    # ------------------------------ 五、组卷处理说明与待办
    out.append("### 五、组卷处理说明与待办")
    out.append("")
    items = ["试卷不标注题目来源（按命题要求）；材料出处「——摘编自……」属材料本身的"
             "引用信息，予以保留。题目来源见本说明第二、三部分。"]
    changed = [(i, r) for i, r in enumerate(recs, 1)
               if str(r["分值"]).strip().isdigit()
               and str(r["fm"].get("分值", "")).strip().isdigit()
               and int(r["分值"]) != int(r["fm"]["分值"])]
    if changed:
        items.append("分值调整：第 " + "、".join(str(i) for i, _ in changed)
                     + " 题的卷面分值与原题不同（按本卷总分结构统一赋分），设问文字未作改动。")
    moved = [(i, r) for i, r in enumerate(recs, 1) if r.get("选项序")]
    if moved:
        parts = []
        for i, r in moved:
            old = str(r["fm"].get("答案", "")).strip()
            seq = [int(x) - 1 for x in r["选项序"]]
            pos = "ABCD"[seq.index("ABCD".index(old))] if old in "ABCD" else "?"
            parts.append(f"第 {i} 题正确项由 {old} 位移至 {pos} 位")
        items.append(f"选项重排：为均衡全卷答案分布，对 {len(moved)} 道题作了选项顺序调整——"
                     + "，".join(parts)
                     + "。均为同题内部选项内容互换，题干与干扰项文字未变；"
                       "学生卷与参考答案使用同一版本。")
    mismatch = []
    for i, r in enumerate(nc, len(ch) + 1):
        a = str(r["fm"].get("答案", "") or "")
        got = [int(x) for x in re.findall(r"[（(]\s*(\d+)\s*分", a)]
        if got and str(r["分值"]).strip().isdigit() and sum(got) != int(r["分值"]):
            mismatch.append(i)
    if mismatch:
        items.append("答案分值核对：第 " + "、".join(map(str, mismatch))
                     + " 题题库答案文本自带的分值与本卷赋分值不同，"
                       "请按卷面分值核对评分标准文字。")
    pics = [i for i, r in enumerate(recs, 1) if iter_img_refs(r["body"])]
    if pics:
        items.append(f"图片处理：第 {'、'.join(map(str, pics))} 题含图，"
                     "图片取用题库内原卷图，统一放输出目录的 `images/{卷名}/`。"
                     "卷面撤图或换图的，请以卷面实际为准。")
    noans = [i for i, r in enumerate(recs, 1) if not str(r["fm"].get("答案", "")).strip()]
    if noans:
        items.append(f"待办：第 {'、'.join(map(str, noans))} 题题库未标答案，请补录后再用。")
    items.append("版式：正文宋体小四、固定行距 24 磅，辅文五号，符合 GB 40070-2021 "
                 "字号与行空要求；成卷后跑 `verify_paper_layout.py --docx` 复核。")
    for k, it in enumerate(items, 1):
        out.append(f"{k}．{it}")
        out.append("")
    return out


# ============================================================================
# 五之三、答案分布诊断与自动均衡
# ============================================================================

def balance_options(recs: list) -> list:
    """诊断选择题答案分布并给出重排方案。

    → [(卷面题号, 现答案, 新答案, 选项序)]；已带「选项序」的题视为人工已定，不再改动。
    目标：各字母计数尽量相等（余数补给当前较少的字母），且尽量避免三连及以上同项。
    """
    ch = [(i, r) for i, r in enumerate(recs, 1) if is_choice(r)]
    if len(ch) < 6:
        return []
    seq = [effective_answer(r) for _, r in ch]
    fixed = [bool(r.get("选项序")) for _, r in ch]
    n = len(ch)

    quota = {c: n // 4 for c in "ABCD"}
    for c in sorted("ABCD", key=lambda x: seq.count(x))[:n % 4]:
        quota[c] += 1
    for i, f in enumerate(fixed):
        if f and seq[i] in quota:
            quota[seq[i]] = max(0, quota[seq[i]] - 1)

    target = list(seq)
    for i in range(n):
        if fixed[i]:
            continue
        cand = [c for c in "ABCD" if quota[c] > 0] or list("ABCD")
        pick = None
        if seq[i] in cand and not (i >= 2 and target[i - 1] == seq[i] and target[i - 2] == seq[i]):
            pick = seq[i]
        if pick is None:
            def penalty(c, i=i):
                p = 1 if (i >= 1 and target[i - 1] == c) else 0
                if i >= 2 and target[i - 2] == c:
                    p += 3
                if i + 1 < n and target[i + 1] == c:
                    p += 1
                return p
            pick = min(cand, key=penalty)
        target[i] = pick
        quota[pick] -= 1

    plan = []
    for i, (no, _r) in enumerate(ch):
        if fixed[i] or target[i] == seq[i]:
            continue
        a, b = "ABCD".index(seq[i]), "ABCD".index(target[i])
        perm = [1, 2, 3, 4]
        perm[a], perm[b] = perm[b], perm[a]
        plan.append((no, seq[i], target[i], perm))
    return plan


def print_balance(recs: list, ch_offset: int = 0) -> None:
    """打印均衡方案（供写回清单「选项序」）。"""
    plan = balance_options(recs)
    if not plan:
        return
    print(f"     → 均衡方案：{len(plan)} 题需重排（在清单对应项下加「选项序」即可，答案自动换算）")
    for no, old, new, perm in plan:
        print(f"        第 {no} 题　{old} → {new}　选项序: [{' , '.join(map(str, perm))}]")


# ============================================================================
# 五之四、命题说明 Word
# ============================================================================

def render_spec_docx(man: dict, recs: list, out: Path) -> None:
    """命题说明 → Word（A4／宋体 12pt／固定行距 24pt／表内 10.5pt）。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    lines = spec_lines(man, recs)
    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Mm(210), Mm(297)
    s.left_margin = s.right_margin = Mm(21)
    s.top_margin = s.bottom_margin = Mm(25)

    def sty(run, size, bold, font="宋体"):
        run.font.size = Pt(size)
        run.bold = bold
        run.font.name = font
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font)

    def add_para(text, size=12, bold=False, align=None, line=24):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.line_spacing = Pt(line)
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        if align is not None:
            p.alignment = align
        if text:
            sty(p.add_run(text), size, bold)
        return p

    def add_table(rows):
        t = doc.add_table(rows=len(rows), cols=len(rows[0]))
        t.style = "Table Grid"
        for ri, row in enumerate(rows):
            for ci, val in enumerate(row[:len(rows[0])]):
                cell = t.cell(ri, ci)
                cell.text = ""
                p = cell.paragraphs[0]
                pf = p.paragraph_format
                pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                pf.line_spacing = Pt(20)            # 辅文 10.5pt 需行距 ≥20pt（GB 40070-2021）
                pf.space_before = Pt(0)
                pf.space_after = Pt(0)
                sty(p.add_run(val), 10.5, ri == 0)

    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln:
            i += 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                add_table(rows)
            continue
        if ln.startswith("# "):
            add_para(ln[2:], 15, True, WD_ALIGN_PARAGRAPH.CENTER, 28)
        elif ln.startswith("## "):
            add_para(ln[3:], 14, True, WD_ALIGN_PARAGRAPH.CENTER, 26)
        elif ln.startswith("### "):
            add_para(ln[4:], 12, True, None, 24)
        elif ln.startswith("**") and ln.rstrip().endswith("**") and ln.count("**") == 2:
            add_para(ln.strip().strip("*"), 12, True)
        else:
            add_para(ln, 12)
        i += 1

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


# ============================================================================
# 六、Word 卷
# ============================================================================

def render_docx(man: dict, recs: list, mode: str, out: Path,
                mark_src: bool, img_width_mm: float = 0.0,
                img_right: bool = False,
                card_inline: bool = False, card_per_row: int = 10,
                card_blanks: int = 0) -> None:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_BREAK
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    def setrun(r, size=12, bold=False, cn="宋体"):
        r.font.name = "Times New Roman"
        r.font.size = Pt(size)
        r.font.bold = bold
        r._element.rPr.rFonts.set(qn("w:eastAsia"), cn)
        return r

    def para(doc, size=12, line=24, align=None, before=0, after=0,
             left=0, keep=False, keepn=False):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing = Pt(line)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        pf.left_indent = Pt(left)
        if align is not None:
            p.alignment = align
        pf.keep_together, pf.keep_with_next = keep, keepn
        return p

    def run(p, text, size=12, bold=False, cn="宋体"):
        return setrun(p.add_run(text), size, bold, cn)

    # ---- 图片：文字环绕-上下型环绕（2026-09-26 用户裁定） ----------------------
    # 背景：python-docx 的 add_picture 只出**嵌入型**（wp:inline）。嵌入型图片随段落行距走，
    #       而本卷所有段落行距都是「固定值」（§一），于是图片被行距裁成一条横带
    #       （实测：单元格 20pt 行距把三张文物图全压成横条，用户截图即为该现象）。
    #       改用**上下型环绕**后图片浮动于文字层之外、不受行距约束，文字只在图上下排布，
    #       这同时是「宽幅单图偏小」的根源——浮动图不再需要为嵌入行留白，可按版心放宽。
    from docx.oxml import OxmlElement

    VW_MM = 168.0            # 版心宽（A4 210 − 左右各 21mm）
    IMG_GAP = 6.0            # 并排图之间的水平间距（规范 §10.2：5~8mm）
    MAX_W = 155.0            # 单图可铺的最大宽度（版心留 13mm 余量）
    BASE_W = 78.0            # 单图基准宽（非宽幅图，沿旧默认值）
    RIGHT_W = 55.0           # §10.1 靠右环绕的单图宽（约版心 1/3）
    RIGHT_MAX_W = 80.0       # 靠右环绕时宽幅图的上限（再宽就没有"绕左"空间了）
    PT_MM = 2.8346           # 1mm ≈ 2.8346pt（图文混排的垂直/水平补偿换算）
    _rel_h = [2]             # relativeHeight 递增，避免多图 z 序相同

    def _ar(src) -> float:
        """图片宽高比（宽/高）；读不到时按 1.5 估算。"""
        try:
            from PIL import Image
            with Image.open(src) as im:
                return (im.width / im.height) if im.height else 1.5
        except Exception:
            return 1.5

    def _fit(src, max_w: float, max_h: float) -> float:
        """在 max_w × max_h 的框内等比装箱，返回可用宽度（mm）。"""
        ar = _ar(src)
        w = float(max_w)
        if ar > 0 and w / ar > max_h:
            w = max_h * ar
        return max(10.0, w)

    def _auto_single_w(src, max_h: float) -> float:
        """单图宽度：**宽幅图铺到 MAX_W**，普通/高窄图回落到基准宽（并按高度上限收缩）。

        依据规范 §10.1（单图约版心 1/3–1/2）与用户 2026-09-26 裁定：
        嵌入型改为上下型环绕后，宽幅图（年代尺、箭头示意、时间轴，宽高比 ≥2.6）
        不再受"嵌入行"限制，可铺满版心；普通方图保持基准宽，避免占版过度。
        """
        if img_width_mm:                        # 命令行显式给了固定宽 → 一律照此
            return _fit(src, img_width_mm, max_h)
        ar = _ar(src)
        w = min(MAX_W, max_h * ar)
        if ar < 2.6:
            w = min(w, BASE_W)
        return max(10.0, w)

    def _auto_right_w(src, max_h: float = 60.0) -> float:
        """§10.1 靠右环绕的单图宽：正文绕左，故图不能占满版心。

        普通图取 RIGHT_W（≈版心 1/3）；宽幅图放宽到 RIGHT_MAX_W；高窄图按高度上限反算。
        `--图片宽` 显式给值则一律照此。
        """
        if img_width_mm:
            return _fit(src, img_width_mm, max_h)
        ar = _ar(src)
        w = min(RIGHT_MAX_W, max_h * ar) if ar >= 2.6 else min(RIGHT_W, max_h * ar)
        return max(20.0, w)

    def add_float_pic(p, src, w_mm: float, x_mm=None, y_mm=None, right: bool = False,
                      wrap: str = "topAndBottom"):
        """在段落 p 内插一张浮动图。

        x_mm / y_mm：相对**正文左边界 / 锚点段落顶**的偏移；
        x=None → 按 right 决定居右/居中；y=None → 贴锚点段落顶。
        wrap：`topAndBottom`＝上下型环绕（图文上下排，本 skill 默认）；
              `square`＝四周型环绕（文字可绕图侧，用于 §10.1「图靠右、题干绕左」）。
        实现：把 `wp:inline` 整段改写成 `wp:anchor` + `wp:wrapTopAndBottom`
        （python-docx 无内置 API；anchor 子元素顺序受 schema 约束，不可乱序）。
        """
        r = p.add_run()
        r.add_picture(str(src), width=Mm(w_mm))
        drawing = r._element.find(qn("w:drawing"))
        if drawing is None:
            return
        inline = drawing.find(qn("wp:inline"))
        if inline is None:                          # 已是 anchor（异常路径）→ 原样保留
            return
        extent = inline.find(qn("wp:extent"))
        docpr = inline.find(qn("wp:docPr"))
        cnv = inline.find(qn("wp:cNvGraphicFramePr"))
        graphic = inline.find(qn("a:graphic"))

        _rel_h[0] += 1
        an = OxmlElement("wp:anchor")
        for k, v in (("distT", "0"), ("distB", "0"), ("distL", "0"), ("distR", "0"),
                     ("simplePos", "0"), ("relativeHeight", str(_rel_h[0])),
                     ("behindDoc", "0"), ("locked", "0"),
                     ("layoutInCell", "1"), ("allowOverlap", "1")):
            an.set(k, v)

        sp = OxmlElement("wp:simplePos")
        sp.set("x", "0")
        sp.set("y", "0")
        an.append(sp)                                   # ① simplePos

        ph = OxmlElement("wp:positionH")                # ② positionH
        ph.set("relativeFrom", "margin")
        if x_mm is None:
            al = OxmlElement("wp:align")
            al.text = "right" if right else "center"
            ph.append(al)
        else:
            off = OxmlElement("wp:posOffset")
            off.text = str(int(round(x_mm * 36000)))    # 1mm = 36000 EMU
            ph.append(off)
        an.append(ph)

        pv = OxmlElement("wp:positionV")                # ③ positionV
        pv.set("relativeFrom", "paragraph")
        if y_mm is None:
            al2 = OxmlElement("wp:align")
            al2.text = "top"
            pv.append(al2)
        else:
            off2 = OxmlElement("wp:posOffset")
            off2.text = str(int(round(y_mm * 36000)))
            pv.append(off2)
        an.append(pv)

        an.append(extent)                               # ④ extent（复用原节点）
        ee = OxmlElement("wp:effectExtent")             # ⑤ effectExtent
        for k in ("l", "t", "r", "b"):
            ee.set(k, "0")
        an.append(ee)
        if wrap == "square":                            # ⑥ 环绕方式
            ws = OxmlElement("wp:wrapSquare")           #    四周型：文字可绕图两侧
            ws.set("wrapText", "bothSides")
            an.append(ws)
            for k in ("distL", "distR"):                #    与文字留 2mm 间距，避免贴字
                an.set(k, str(int(2 * 36000)))
        else:
            an.append(OxmlElement("wp:wrapTopAndBottom"))   # 上下型：文字只在图上下
        an.append(docpr)                                # ⑦ docPr
        if cnv is not None:
            an.append(cnv)                              # ⑧ cNvGraphicFramePr
        an.append(graphic)                              # ⑨ graphic

        drawing.remove(inline)
        drawing.append(an)

    def pic_block(items: list, max_h: float = 105.0):
        """一段内并排若干「图列」+ 图注行（图注列与图列对齐）。

        items = [([图片路径…], [图注…]), …] —— 一个图列可含多张图（题库里同格多图），
        列内**横向均分**（列宽恒定，保证列对齐；与原卷「格内多小图并排」一致）。
        n == 1 且单图时居中、图注为普通居中段落。
        锚点段落行距取极小值：图片自锚点顶向下浮动，文字被排到图下（上下型环绕语义）。
        """
        n = len(items)
        keep = para(doc, size=1, line=1, after=0, keep=True, keepn=True)
        col_w = (VW_MM - IMG_GAP * (n - 1)) / n
        for j, (srcs, _caps) in enumerate(items):
            m = len(srcs)
            if n == 1 and m == 1:
                add_float_pic(keep, srcs[0], _auto_single_w(srcs[0], max_h), right=img_right)
                continue
            in_gap = 2.0 if m > 1 else 0.0
            iw = (col_w - in_gap * (m - 1)) / m
            for t, src in enumerate(srcs):
                w = _fit(src, iw, max_h)
                add_float_pic(keep, src, w,
                              x_mm=j * (col_w + IMG_GAP) + t * (iw + in_gap) + (iw - w) / 2.0)
        caps = [c for _s, c in items]
        if n == 1:
            for c in caps[0]:
                pc = para(doc, size=10.5, line=20, align=WD_ALIGN_PARAGRAPH.CENTER,
                          after=0, keep=True, keepn=True)
                run(pc, c, 10.5)
            return
        if not any(caps):
            return
        t = doc.add_table(rows=1, cols=n)               # 默认 Table Normal：无框线
        t.autofit = False
        tl = OxmlElement("w:tblLayout")
        tl.set(qn("w:type"), "fixed")
        t._tbl.tblPr.append(tl)
        for j in range(n):
            c = t.cell(0, j)
            c.text = ""
            c.width = Mm(col_w)
            txt = "\n".join(caps[j]) if j < len(caps) else ""
            for li, tk in enumerate(txt.split("\n")):
                cp = c.paragraphs[0] if li == 0 else c.add_paragraph()
                cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                cp.paragraph_format.line_spacing = Pt(20)
                cp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                cp.paragraph_format.space_before = cp.paragraph_format.space_after = Pt(0)
                setrun(cp.add_run(tk), 10.5)

    CELL_IMG_ONLY = re.compile(r"(?:!\[[^\]]*\]\([^)]+\)\s*)+")

    def pic_table_groups(rows: list):
        """是否「题库多图排版表」→ 返回 [(图片行, 图注行), …]；否则返回 None。

        形态：偶数行、成对出现，「图片行 + 图注行」为一组（一组可 3 图/4 图/6 图…）。
        6 图题在题库里是「图/注/图/注」四行表 → 视为两组，逐组渲染为并排浮动图行。
        """
        if len(rows) < 2 or len(rows) % 2:
            return None
        groups = []
        for i in range(0, len(rows), 2):
            head = [c.strip() for c in rows[i] if c.strip()]
            if not head or not all(CELL_IMG_ONLY.fullmatch(c) for c in head):
                return None
            groups.append((rows[i], rows[i + 1]))
        return groups

    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Mm(210), Mm(297)
    s.left_margin = s.right_margin = Mm(21)
    s.top_margin = s.bottom_margin = Mm(25)
    s.footer_distance = Mm(12)
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    # 卷头
    def centered(text, size=14, bold=False, before=2, after=2, keepn=True):
        p = para(doc, size=size, line=24, align=WD_ALIGN_PARAGRAPH.CENTER,
                 before=before, after=after, keepn=keepn)
        run(p, text, size, bold)
        return p

    if man.get("标题"):
        centered(man["标题"], 14, True, before=0, after=4)
    if man.get("副标题"):
        centered(man["副标题"], 12, False, before=0, after=4)

    notes = man.get("说明") or []
    if isinstance(notes, str):
        notes = [notes]
    if notes:
        para(doc, size=10.5, line=20, after=2, keepn=True)
        for i, n in enumerate(notes, 1):
            p = para(doc, size=10.5, line=20, after=0, keepn=True)
            run(p, f"{i}.{clean(n)}", 10.5)          # 半角点，与题目「1．」区分
        para(doc, size=10.5, line=12, after=0)

    # 题目
    for n, mark in section_marks(recs):
        r = recs[n - 1]
        if mark:                                     # 大题标题（§二）
            p = para(doc, before=6, after=2, keepn=True)
            run(p, clean(mark), 12, True)
        img_dir = out.parent / "images" / img_ns(r["vol"])
        stripped, imgs = extract_images(r["body"])
        stripped, tbls = extract_tables(stripped)
        lines, opts = split_stem_opts(stripped)
        if not lines:
            lines = [clean(stripped)]
        opts, new_ans = reorder_opts(opts, r)
        r["_ans"] = new_ans if new_ans else r["fm"].get("答案", "")
        rtype = rtype_of(r)
        if rtype == "选择题":                        # 材料题作答在答题区，不补卷内括号
            slot = find_answer_slot(lines, r.get("括号"))
            if slot >= 0:
                lines[slot] += "（　　）"

        no_txt = f"{n}．"
        if rtype and rtype != "选择题" and str(r["分值"]).strip().isdigit():
            no_txt += f"（{r['分值']}分）"           # 非选择题分值紧随题号（§四）
        if mark_src and mode == "学生" and r["来源括注"]:
            no_txt += str(r["来源括注"])              # 来源括注紧跟题号后（§12.1）

        # 连续图片占位合成一组 → 并排（§10.2）；单张默认**上下型环绕＋居中**；
        # `--图靠右` 时改 §10.1 式样：**四周型环绕 ＋ 靠右**，图锚在题干首段、文字绕左
        stem_first = None                            # 题干首段（靠右环绕的锚点）
        stem_paras = 0                               # 题干已排段数（图注垂直补偿用）
        right_pic = None                             # 靠右环绕的待补图注：(源, 图注, 宽, 图高pt)
        k = 0
        while k < len(lines):
            ln = lines[k]
            if is_tbl_slot(ln):                      # 表格：居中、细框线、表头加粗、表内五号
                rows = tbls[int(TBL_SLOT_RE.fullmatch(ln.strip()).group(1))]
                grps = pic_table_groups(rows)        # 题库多图排版表 → 浮动上下型并排
                if grps:
                    for head_row, cap_row in grps:
                        items = []
                        for j in range(len(head_row)):
                            srcs = []
                            for r1 in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", head_row[j]):
                                s = img_dir / r1.split("images/", 1)[-1]
                                if s.exists():
                                    srcs.append(s)
                            if not srcs:
                                continue
                            cap = ([cap_row[j].strip()]
                                   if j < len(cap_row) and cap_row[j].strip() else [])
                            items.append((srcs, cap))
                        if items:
                            pic_block(items)
                    para(doc, size=10.5, line=10, after=2)
                    k += 1
                    continue

                ncol = max(len(x) for x in rows)
                t = doc.add_table(rows=len(rows), cols=ncol)
                t.style = "Table Grid"
                t.alignment = WD_TABLE_ALIGNMENT.CENTER
                for i, row in enumerate(rows):
                    for j in range(ncol):
                        cell = t.cell(i, j)
                        cell.text = ""
                        cp = cell.paragraphs[0]

                        def _cell_sty(p, pic=False):
                            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            _pf = p.paragraph_format
                            if pic:
                                # ⚠ 含图段落禁用「固定值」行距：嵌入型图片会被行距裁成一条横带
                                _pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
                            else:
                                _pf.line_spacing = Pt(20)
                                _pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                            _pf.space_before = _pf.space_after = Pt(1)
                            return p

                        raw = row[j] if j < len(row) else ""
                        # ① 单元格内 md 图片 → 实际图片；② `<br>` → 单元格内换行
                        #    （2026-09-26 修：原实现把单元格内容整段当纯文本，表格内图片漏成
                        #     `![](images/…)` 字面量、`<br>` 也原样显示 → 多图题在 Word 卷上失效）
                        cell_imgs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", raw)
                        txt = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", raw).replace("<br>", "\n").strip()
                        _cell_sty(cp, pic=bool(cell_imgs))
                        cell_w = (168 - 6 * (ncol - 1)) / ncol - 6
                        per_w = max(cell_w / max(len(cell_imgs), 1) - 2, 12) if cell_imgs else 0
                        first_p = True
                        for src_rel in cell_imgs:
                            rel = src_rel.split("images/", 1)[-1] if "images/" in src_rel else src_rel
                            src = img_dir / rel
                            if not src.exists():
                                continue
                            p = cp if first_p else _cell_sty(cell.add_paragraph(), pic=True)
                            first_p = False
                            try:
                                p.add_run().add_picture(
                                    str(src),
                                    width=Mm(min(per_w, img_width_mm) if img_width_mm else per_w))
                            except Exception:
                                continue
                        for li, tk in enumerate(txt.split("\n")):
                            if not tk.strip():
                                continue
                            p = cp if (first_p and li == 0) else _cell_sty(cell.add_paragraph())
                            first_p = False
                            setrun(p.add_run(tk), 10.5, bold=(i == 0))
                para(doc, size=10.5, line=10, after=0)
                k += 1
                continue
            if is_img_slot(ln):
                grp = []
                while k < len(lines) and is_img_slot(lines[k]):
                    grp.append(int(IMG_SLOT_RE.fullmatch(lines[k].strip()).group(1)))
                    k += 1
                items = []
                for gi in grp:
                    nm, cap = imgs[gi]
                    src = img_dir / nm
                    if src.exists():
                        items.append(([src], cap))
                if len(grp) == 1 and img_right and stem_first is not None and items:
                    # §10.1 图靠右、题干绕左：图锚在题干首段（四周型环绕），题干与选项都绕在
                    # 图左侧；图注（或等高空段）要落图正下方，故**推迟到选项之后再插**
                    # （见下方 right_pic 处理；2026-09-26 实测：插在题干后会把选项一并顶到图下）。
                    src, cap = items[0][0][0], items[0][1]
                    w = _auto_right_w(src)
                    add_float_pic(stem_first, src, w, right=True, wrap="square")
                    right_pic = (src, cap, w, (w / max(_ar(src), 0.05)) * PT_MM)
                elif items:
                    pic_block(items)
                    para(doc, size=10.5, line=10, after=2)
                continue
            p = para(doc, left=(12 if ln.startswith("材料") else 0),
                     align=(WD_ALIGN_PARAGRAPH.RIGHT if ln.startswith("——") else None),
                     keepn=True)
            run(p, (no_txt if k == 0 else "") + ln, 12)
            if stem_first is None:
                stem_first = p
            stem_paras += 1
            k += 1
        opt_lines = 0
        if opts:                                     # 两项一行、A．全角顿点（§三/§12.4）
            for i in range(0, len(opts) - 1, 2):
                p = para(doc, before=0, after=0, keepn=(i + 2 < len(opts)))
                run(p, f"{'ABCD'[i]}．{opts[i]}\u3000\u3000"
                       f"{'ABCD'[i + 1]}．{opts[i + 1]}", 12)
                opt_lines += 1
            if len(opts) % 2:
                p = para(doc, before=0, after=0)
                run(p, f"{'ABCD'[len(opts) - 1]}．{opts[-1]}", 12)
                opt_lines += 1

        # §10.1 靠右环绕收尾：图注（无图注则等高空段）补在**选项之后**——
        # 题干与选项同绕图左，图注才落图正下方；补偿量＝图高 −「题干＋选项」自然高度。
        # ⚠ 缺此步则图不占文档流，下一题会与本题图片垂直重叠（2026-09-26 实测）。
        if right_pic is not None:
            _src_r, cap_r, w_r, h_pt = right_pic
            need = max(0.0, h_pt + 4.0 - (stem_paras + opt_lines) * 24.0)
            left_pt = (VW_MM - w_r) * PT_MM
            if cap_r:
                for ci, c in enumerate(cap_r):
                    pc = para(doc, size=10.5, line=20, align=WD_ALIGN_PARAGRAPH.CENTER,
                              before=(need if ci == 0 else 0), left=left_pt,
                              keep=True, keepn=True)
                    run(pc, c, 10.5)
            elif need > 0:
                para(doc, size=1, line=max(2.0, need), after=0)
        para(doc, size=12, line=12, after=0)

    # 答案卷：分页另起（v4：命题说明已独立成文档，本分支不再触达，保留供复用）
    if mode == "教师":
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        centered(f"{man.get('卷名', '')}　参考答案", 14, True, before=0, after=6)
        ch_ans = [(n, r) for n, r in enumerate(recs, 1) if is_choice(r)]
        if ch_ans:
            p = para(doc, after=2, keepn=True)
            run(p, clean(big_title(recs, True).split("（")[0]), 12, True)
            items = [f"{n}．{r.get('_ans', r['fm'].get('答案', ''))}" for n, r in ch_ans]
            for i in range(0, len(items), 10):        # 选择题答案连排（§六，每行 10 项）
                p = para(doc, after=0)
                run(p, "\u3000".join(items[i:i + 10]), 12)
        for n, r in enumerate(recs, 1):
            fm = r["fm"]
            jx = re.sub(r"!\[[^\]]*\]\([^)]+\)", "",            # 答案区不带图
                        str(fm.get("解析", "") or "")).strip()
            if is_choice(r):                            # 选择题答案已连排：仅补解析
                if not jx:
                    continue
                p = para(doc, size=10.5, line=20, left=12, before=2, after=0)
                run(p, f"[{n} 解析] {clean(jx)}", 10.5)
                continue
            p = para(doc, before=3, after=0, keepn=True)
            run(p, f"{n}．{r.get('_ans', fm.get('答案', ''))}", 12)
            if jx:
                p = para(doc, size=10.5, line=20, left=12, after=0)
                run(p, f"[解析] {clean(jx)}", 10.5)
            if r["角度"]:
                p = para(doc, size=10.5, line=20, left=12, after=0)
                run(p, f"[考察角度] {clean(r['角度'])}", 10.5)

    # 答题区附卷末（小测试/周测：无独立答题卡文件，学生卷末尾分页后接「附：答题卡」）
    if card_inline:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        render_card_body(doc, man, recs, card_per_row, card_blanks, inline=True)

    # 页脚页码域
    from docx.oxml import OxmlElement
    fp = doc.sections[0].footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pre = run(fp, f"{man.get('卷名', '')}　第 ", 9)
    for instr in ("PAGE", "NUMPAGES"):
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), instr)
        fp._p.append(fld)
        if instr == "PAGE":
            run(fp, " 页　共 ", 9)
    run(fp, " 页", 9)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


# ============================================================================
# 七、卷面体检
# ============================================================================

def card_setrun(rr, size=12, bold=False, cn="宋体"):
    from docx.oxml.ns import qn
    from docx.shared import Pt
    rr.font.name = "Times New Roman"
    rr.font.size = Pt(size)
    rr.font.bold = bold
    rr._element.rPr.rFonts.set(qn("w:eastAsia"), cn)
    return rr


def card_run(p, text, size=12, bold=False, cn="宋体"):
    return card_setrun(p.add_run(text), size, bold, cn)


def card_para(doc, size=12, line=24, align=None, after=0, keepn=False):
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Pt
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = Pt(line)
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.space_before, pf.space_after = Pt(0), Pt(after)
    if align is not None:
        p.alignment = align
    pf.keep_with_next = keepn
    return p


def render_card_body(doc, man, recs, per_row=10, blanks=0, inline=False):
    """答题卡**内容**（不含页面设置与页眉页脚）——独立答题卡与「卷末附加」两种模式共用。

    inline=False：独立答题卡 —— 标题两行（卷名＋「答　题　卡」），姓名栏由调用方写进首页页眉；
    inline=True ：卷末附加（小测试/周测不出独立答题卡文件）—— 简版标题「附：答题卡」＋正文姓名栏
    （卷尾无首页装订遮挡问题，姓名栏放正文即可）。
    """
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.shared import Mm, Pt

    if inline:
        p = card_para(doc, size=14, align=WD_ALIGN_PARAGRAPH.CENTER, after=2, keepn=True)
        card_run(p, "附：答题卡", 14, True, "黑体")
        p = card_para(doc, after=6, keepn=True)
        card_run(p, "姓名__________　班级__________　考场__________　考号__________", 12)
    else:
        for txt in (man.get("标题") or man.get("卷名") or "", "答　题　卡"):
            p = card_para(doc, size=14, align=WD_ALIGN_PARAGRAPH.CENTER, after=2, keepn=True)
            card_setrun(p.add_run(txt), 14, True, "黑体")

    pairs = list(enumerate(recs, 1))
    ch = [(n, r) for n, r in pairs if is_choice(r)]
    ot = [(n, r) for n, r in pairs if not is_choice(r)]

    if ch:
        p = card_para(doc, after=2, keepn=True)
        card_run(p, clean(big_title(recs, True)), 12)
        for start in range(0, len(ch), per_row):
            grp = ch[start:start + per_row]
            t = doc.add_table(rows=2, cols=len(grp) + 1)
            t.style = "Table Grid"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            t.autofit = False
            for j in range(len(t.columns)):           # 首列 16mm、其余 14.4mm（照参考卡）
                t.columns[j].width = Mm(16) if j == 0 else Mm(14.4)
            for row in t.rows:
                row.height = Mm(8.5)
                for j, cell in enumerate(row.cells):
                    cell.width = Mm(16) if j == 0 else Mm(14.4)
                    cell.text = ""
                    cp = cell.paragraphs[0]
                    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    pf = cp.paragraph_format
                    pf.line_spacing = Pt(20)
                    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                    pf.space_before = pf.space_after = Pt(0)
            card_setrun(t.cell(0, 0).paragraphs[0].add_run("题号"), 10.5)
            card_setrun(t.cell(1, 0).paragraphs[0].add_run("答案"), 10.5)
            for j, (qn_, _r) in enumerate(grp, 1):
                card_setrun(t.cell(0, j).paragraphs[0].add_run(str(qn_)), 10.5)
            card_para(doc, size=12, line=12, after=4)

    if ot:
        p = card_para(doc, after=2, keepn=True)
        card_run(p, clean(big_title(recs, False)), 12)
        for n, r in ot:
            subs = list(dict.fromkeys(
                re.findall(r"[（(]\s*(\d+)\s*[）)]", r["body"])))
            total = int(r["分值"]) if str(r["分值"]).strip().isdigit() else None
            seg = []                                 # 本题占用的段落，用于「整题尽量同页」

            def mk(size=12, line=24, after=0):
                p = card_para(doc, size=size, line=line, after=after)
                seg.append(p)
                return p

            if not subs:                             # 无小问（论述题等开放题型）：按整题分值留白
                p = mk()
                card_run(p, f"{n}．", 12)
                for _ in range(max(blanks_for(total, blanks), 6)):
                    mk()
                mk(size=12, line=12)
            else:
                scores = sub_scores(r["body"], total, len(subs))
                for si, sub in enumerate(subs):
                    label = f"{n}．" if si == 0 else ""
                    p = mk()
                    card_run(p, f"{label}（{sub}）", 12)
                    for _ in range(blanks_for(scores[si], blanks)):
                        mk()
                mk(size=12, line=12)
            # 「同一题的若干小问尽量同页」（方便批改）：除末段外全部「与下段同页」，
            # 整题放不下时整体下移，不会把 (1) 留在上页、其余甩到下页
            for p in seg[:-1]:
                p.paragraph_format.keep_with_next = True


def render_answer_card(man: dict, recs: list, out: Path,
                       per_row: int = 10, blanks: int = 2) -> None:
    """答题卡（**独立文件**，校级测试·手阅版，A4 两页）。版式照 2026.09.22 月考答题卡实物：

    · 页面 A4，**装订区在上侧（姓名栏页眉处）**：上边距 22mm（钉子走页眉上方 0~12mm 空白），
      左右对称 20 / 下 18，页脚距 12.7
    · 标题两行：卷名 ＋「答　题　卡」，黑体 14pt 居中
    · 姓名栏在**首页页眉**（页面最上方，仅第 1 页；正文放会被装订钉盖住第二页作答区）
    · 选择题 **全框线表格**（`题号`／`答案` 两行，每行 per_row 题，首列 16mm、其余 14.4mm、行高 8.5mm）
    · 非选择题逐题逐小问「21．（1）」各留 blanks 行（0＝按分值自动）
    · 页脚「历史答题卡　第 X 页　共 Y 页」

    小测试/周测的「答题区附卷末」不走本函数——用 render_docx 的 card_inline 路径（同一内容渲染）。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Mm(210), Mm(297)
    # 装订区在**上侧姓名栏处**（2026-09-25 用户定版）：装订钉走页眉上方的 0~12mm 空白区，
    # 姓名栏 12mm 起、正文 22mm 起——上边距只比常规略大，避免挤掉版面多出一页；
    # 左侧无需特殊边距，左右对称 20mm
    s.left_margin, s.right_margin = Mm(20), Mm(20)
    s.top_margin, s.bottom_margin = Mm(22), Mm(18)
    s.footer_distance = Mm(12.7)
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    # 姓名栏放**首页页眉**（页面最上方）——只第 1 页有，第 2 页起不重复。
    # ⚠ 不能放正文：那样第二页顶部直接是学生作答区，装订时被钉住/盖住（2026-09-25 用户实测反馈）
    # 页眉距顶 12mm：装订钉（0~10mm 左右）从姓名栏上方空白穿过，钉不到字
    s.header_distance = Mm(12)
    s.different_first_page_header_footer = True          # 首页页眉页脚独立
    fh = s.first_page_header
    fh.is_linked_to_previous = False
    hp = fh.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hpf = hp.paragraph_format
    hpf.line_spacing = Pt(20)
    hpf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    hpf.space_before = hpf.space_after = Pt(0)
    card_setrun(hp.add_run("姓名__________　班级__________　考场__________　考号__________"), 12)
    s.header.is_linked_to_previous = False               # 后续页页眉留空

    render_card_body(doc, man, recs, per_row, blanks, inline=False)

    # 页脚：首页与后续页都要页码（启用「首页不同」后两处须分别写）
    for part in (s.footer, s.first_page_footer):
        part.is_linked_to_previous = False
        fp = part.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        card_run(fp, "历史答题卡\u3000第 ", 9)
        for instr in ("PAGE", "NUMPAGES"):
            fld = OxmlElement("w:fldSimple")
            fld.set(qn("w:instr"), instr)
            fp._p.append(fld)
            if instr == "PAGE":
                card_run(fp, " 页\u3000共 ", 9)
        card_run(fp, " 页", 9)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))


def checkup(man: dict, recs: list) -> None:
    from collections import Counter
    total = sum(int(r["分值"]) for r in recs if str(r["分值"]).isdigit())
    ttypes = Counter(str(r["fm"].get("题型", "?")) for r in recs)
    diff = Counter(str(r["fm"].get("难度", "?")) for r in recs)
    ch = [r for r in recs if is_choice(r)]
    ans = Counter(effective_answer(r) for r in ch)
    ans = Counter({k: v for k, v in ans.items() if k in ("A", "B", "C", "D")})
    vols = Counter(r["vol"] for r in recs)
    books = Counter(b for r in recs for b in as_list(r["fm"].get("册别")))
    lessons = Counter(l for r in recs for l in as_list(r["fm"].get("课"))[:1])
    srcs = Counter(("教材改编" if "教材改编" in as_list(r["fm"].get("来源"))
                    else "真题" if "真题" in as_list(r["fm"].get("来源"))
                    else "原创题" if "原创题" in as_list(r["fm"].get("来源"))
                    else "模拟/其他") for r in recs)

    print(f"\n===== 卷面体检：{man.get('卷名', '')} =====")
    print(f"题量 {len(recs)}　总分 {total}　"
          f"题型 {' / '.join(f'{k} {v}' for k, v in ttypes.most_common())}")
    print(f"难度 {' · '.join(f'{k} {v}' for k, v in diff.most_common())}")
    ac = " · ".join(f"{k} {v}" for k, v in sorted(ans.items())) or "（无选择题）"
    print(f"答案 {ac}　（{len(ch)} 道选择题）")
    if ans and len(ans) > 1 and max(ans.values()) > max(2, len(ch) * 0.35):
        print("     ⚠ 答案分布偏集中 → 对部分题加「选项序」重排（答案会自动同步换算）")
    n_re = sum(1 for r in recs if r.get("选项序"))
    if n_re:
        print(f"选项重排 {n_re} 题（答案已同步换算，题库原题不动）")
    print(f"来源 {' · '.join(f'{k} {v}' for k, v in srcs.most_common())}")
    print(f"册别 {' · '.join(f'{k} {v}' for k, v in books.most_common())}")
    print(f"卷源 {len(vols)} 个 —— " + " · ".join(f"{k}({v})" for k, v in vols.most_common()))
    print(f"考点覆盖 {len(lessons)} 课")
    for k, v in lessons.most_common():
        if v > 1:
            print(f"     ⚠ 同课多题：{k} × {v}")
    head_slot = []
    for i, r in enumerate(recs, 1):
        if str(r["fm"].get("题型", "")).strip() != "选择题":
            continue
        ln, _ = split_stem_opts(extract_images(r["body"])[0])
        if any("（　　）" in l for l in ln):
            continue
        sl = find_answer_slot(ln, r.get("括号"))
        if sl >= 0 and sl != len(ln) - 1:
            head_slot.append(i)
    if head_slot:
        print(f"     ⚠ {len(head_slot)} 道题的作答括号自动补在**非末行**（设问在材料之前），"
              f"成卷前请核对：第 {'、'.join(map(str, head_slot))} 题")
    seq = [effective_answer(r) for r in ch]
    run_same, run_start, prev = 1, 0, (seq[0] if seq else "")
    for i in range(1, len(seq)):
        if seq[i] and seq[i] == prev:
            run_same += 1
        else:
            if run_same >= 3:
                print(f"     ⚠ 选择题第 {run_start + 1}—{i} 题答案连续同为 {prev}")
            run_same, run_start = 1, i
        prev = seq[i]
    if run_same >= 3:
        print(f"     ⚠ 选择题第 {run_start + 1}—{len(seq)} 题答案连续同为 {prev}")
    if ans:
        spread = max(ans.values()) - min(ans.values()) if len(ans) > 1 else 0
        if spread > max(1, len(ch) * 0.15):
            print(f"     → 分布偏斜（极差 {spread}）；可按下列方案加「选项序」，或直接跑 `--均衡选项`")
            print_balance(recs)


# ============================================================================
# 八、兼容：按卷导出（原用法）
# ============================================================================

def find_volume(vol: str, stage: str = None, category: str = None) -> Path:
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
    if mode == "id":
        m = re.search(r"(\d+)", p.stem)
        return (0, int(m.group(1))) if m else (1, p.stem)
    return (0, p.stem)


def split_nums(s: str) -> list:
    nums = [x.strip() for x in re.split(r"[,，、\s]+", s) if x.strip()]
    if not nums:
        raise SystemExit("[失败] --only 里有空的题号组")
    seen, out = set(), []
    for n in nums:
        k = norm_no(n)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def parse_specs(vol_arg: str, only_arg: str) -> list:
    vols = [v.strip() for v in re.split(r"[,，]", vol_arg) if v.strip()]
    if not vols:
        raise SystemExit("[失败] --卷 不能为空")
    dup = sorted({v for v in vols if vols.count(v) > 1})
    if dup:
        raise SystemExit(f"[失败] --卷 里同一卷出现多次：{'、'.join(dup)}")
    if not only_arg:
        return [(v, None) for v in vols]
    groups = [s.strip() for s in re.split(r"[;；]", only_arg) if s.strip()]
    if not groups:
        raise SystemExit("[失败] --only 里没有任何题号组")
    named = [bool(re.search(r"[:：]", g)) for g in groups]
    if any(named):
        if not all(named):
            raise SystemExit("[失败] --only 混用两种写法（带卷名与不带卷名），请统一")
        specs = []
        for g in groups:
            name, nums = re.split(r"[:：]", g, maxsplit=1)
            specs.append((name.strip(), split_nums(nums)))
        unknown = [n for n, _ in specs if n not in vols]
        if unknown:
            raise SystemExit(f"[失败] --only 里的卷名不在 --卷 列表内：{'、'.join(unknown)}")
        return specs
    if len(groups) == 1:
        return [(v, split_nums(groups[0])) for v in vols]
    if len(groups) == len(vols):
        return [(v, split_nums(g)) for v, g in zip(vols, groups)]
    raise SystemExit(f"[失败] --only 有 {len(groups)} 组、--卷 有 {len(vols)} 卷，数量不匹配")


def collect(vol_dir: Path, qtype: str = None, only: list = None):
    files = [p for p in vol_dir.rglob("*.md") if is_question_file(p)]
    if qtype:
        # 目录名兼容（2026-09 题型统一）：论述题的新目录为 {卷名}_论述题，存量仍为 {卷名}_小论文
        names = [f"_{qtype}"] + [f"_{t}" for t in TYPE_LEGACY_DIRS.get(qtype, ())]
        files = [p for p in files if any(n in p.parent.name for n in names)]
    if only:
        wanted = {norm_no(x) for x in only}
        files = [p for p in files if norm_no(p.stem) in wanted]
    return files


def export_by_volume(args) -> int:
    specs = parse_specs(args.卷, args.only)
    multi = len(specs) > 1
    if multi and not args.输出:
        raise SystemExit("[失败] 跨卷组卷必须显式指定 --输出")
    units, total = [], 0
    for vol, nums in specs:
        try:
            vol_dir = find_volume(vol, args.学段, args.类别)
        except FileNotFoundError as e:
            raise SystemExit(f"[失败] {e}")
        files = collect(vol_dir, args.题型, nums)
        if nums:
            idx = {norm_no(x): i for i, x in enumerate(nums)}
            files.sort(key=lambda p: idx.get(norm_no(p.stem), len(idx)))
            found = {norm_no(p.stem) for p in files}
            missing = [n for n in nums if norm_no(n) not in found]
            if missing:
                print(f"[警告] {vol}：题号未找到 → {'、'.join(missing)}")
        else:
            files.sort(key=lambda p: sort_key(p, args.排序))
        units.append((vol, vol_dir, files))
        total += len(files)
    if total == 0:
        print("[失败] 未选到任何题目", file=sys.stderr)
        return 1
    if args.模式 == "命题说明":
        print("[失败] 命题说明须用清单驱动：--清单 <组卷清单.yaml> --模式 命题说明",
              file=sys.stderr)
        return 1
    out = Path(args.输出) if args.输出 else (
        units[0][1] / f"{units[0][1].name}_学生版.md")
    blocks = []
    for vol, _d, files in units:
        for p in files:
            fm, body, err = split_frontmatter(p.read_text(encoding="utf-8"))
            body = strip_comments(body)
            if not body:
                continue
            body = rewrite_img(body, vol, ns=multi)   # 单卷不加命名空间；多形态引用统一转标准 md 形态
            if args.来源标注:
                body += f"\n\n**来源**：{vol}（原第 {p.stem} 题）"
            blocks.append(body)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n---\n\n".join(blocks) + "\n", encoding="utf-8")
    n_img = 0
    for vol, _d, files in units:
        img_dir = (out.parent / "images" / img_ns(vol)) if multi else (out.parent / "images")
        for p in files:
            n_img += _copy_img_refs(
                p.read_text(encoding="utf-8"), p.parent, img_dir)
    if n_img:
        print(f"[图片] 已打包 {n_img} 张")
    print(f"[导出] {out}（{len(blocks)} 题，学生卷）")
    for vol, _d, files in units:
        print(f"  · {vol}：{len(files)} 题 → " + "、".join(p.stem for p in files))
    return 0


# ============================================================================
# 九、入口
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="历史题库组卷导出器（清单驱动）")
    ap.add_argument("--清单", help="组卷清单（.yaml/.yml/.json；.txt 一行一个 id）")
    ap.add_argument("--卷", help="[兼容] 卷名；多卷用逗号分隔")
    ap.add_argument("--only", help="[兼容] 题号；跨卷用分号分组，可写 卷名:题号")
    ap.add_argument("--学段", choices=["初中", "高中"])
    ap.add_argument("--类别", choices=["中考真题", "中考模拟题", "阶段测试", "高考真题", "高考模拟题"])
    ap.add_argument("--题型")
    ap.add_argument("--模式", choices=["学生", "命题说明"], default="学生",
                    help="学生=干净学生卷（默认）；命题说明=题目来源与考察角度"
                         "（内部资料：卷面结构／单元配比／能力维度／难度与答案分布／"
                         "命题细目表／参考答案／组卷处理说明）")
    ap.add_argument("--输出")
    ap.add_argument("--排序", choices=["路径", "id"], default="id")
    ap.add_argument("--来源标注", action="store_true", help="[兼容] 命题说明附原卷来源（现恒附）")
    ap.add_argument("--标来源", action="store_true",
                    help="学生卷在末行附来源括注（近5年中考真题等）")
    ap.add_argument("--docx", help="另出 Word 卷到该路径")
    ap.add_argument("--图片宽", type=float, default=0.0,
                    help="Word 卷单图最大宽度 mm（默认 0＝自动：按图片宽高比在版心 155mm×105mm 内装箱）")
    ap.add_argument("--图靠右", action="store_true",
                    help="单图改《试卷排版规范》§10.1 式样：**四周型环绕＋图靠右、题干绕左**"
                         "（图锚在题干首段，图注落图下居中）；不给则上下型环绕＋居中")
    ap.add_argument("--答题卡", help="另出答题卡到该路径（给了 --docx 时默认同目录同出）")
    ap.add_argument("--无答题卡", action="store_true", help="给了 --docx 也不出答题卡")
    ap.add_argument("--答题卡每行", type=int, default=10, help="选择题答题栏每行题数（默认 10）")
    ap.add_argument("--答题卡行数", type=int, default=0,
                    help="非选择题留白行数；0＝按分值自动（约2分/行、下限2行，默认），>0＝固定行数")
    ap.add_argument("--答题卡位置", choices=["独立", "卷末"], default="独立",
                    help="独立=单独答题卡文件（默认，月考/期中期末）；"
                         "卷末=答题区附在学生卷 docx 末尾（小测试/周测，不出独立文件）")
    ap.add_argument("--体检", action="store_true", help="只打印卷面体检，不导出")
    ap.add_argument("--均衡选项", action="store_true",
                    help="打印答案分布均衡方案（各题应设的「选项序」），写回清单后重出即生效")
    args = ap.parse_args()

    if not args.清单:
        if not args.卷:
            raise SystemExit("[失败] 请给 --清单 <清单文件>（或兼容用法 --卷）")
        return export_by_volume(args)

    man = load_manifest(Path(args.清单))
    items = norm_items(man)
    if not items:
        raise SystemExit("[失败] 清单里没有任何题目")
    index = build_index()
    recs, miss = resolve_items(items, index)
    if not recs:
        raise SystemExit("[失败] 全部条目均未定位到题目")
    for r in recs:
        if r["yaml_err"]:
            print(f"[警告] {r['path'].name}：{r['yaml_err']}")
    print(f"[清单] {len(recs)} 题已定位" + (f"，{len(miss)} 题未定位" if miss else ""))

    checkup(man, recs)
    if args.均衡选项:
        plan = balance_options(recs)
        if not plan:
            print("\n[均衡] 选择题答案分布已均衡，无需重排")
        else:
            print(f"\n[均衡] 建议对 {len(plan)} 题重排"
                  f"（在清单对应项下加「选项序」，答案自动换算）：")
            for no, old, new, perm in plan:
                print(f"        第 {no} 题\u3000{old} → {new}"
                      f"\u3000选项序: [{', '.join(map(str, perm))}]")
        return 0
    if args.体检:
        return 0

    is_spec = args.模式 == "命题说明"
    vol_title = man.get("卷名") or "组卷"
    out = Path(args.输出) if args.输出 else None
    if out is None:
        base = Path(args.清单).parent
        out = base / (f"{vol_title}_命题说明.md" if is_spec else f"{vol_title}_学生版.md")
    # 图片：md 与 docx 各自所在目录都要有（两者目录可能不同）；命题说明纯文字，不打包
    if not is_spec:
        targets = {out.parent}
        if args.docx:
            targets.add(Path(args.docx).parent)
        n_img = 0
        for t in targets:
            n_img += bundle_images(recs, t)
        if n_img:
            print(f"[图片] 已打包 {n_img} 张 → "
                  + "；".join(str(t / 'images') for t in targets))
    out.parent.mkdir(parents=True, exist_ok=True)
    if is_spec:
        out.write_text("\n".join(spec_lines(man, recs)).rstrip() + "\n", encoding="utf-8")
    else:
        out.write_text(render_md(man, recs, args.模式, args.标来源), encoding="utf-8")
    print(f"[导出] {out}（{len(recs)} 题，{args.模式}）")

    inline = args.答题卡位置 == "卷末" and not is_spec
    if args.docx:
        dx = Path(args.docx)
        if is_spec:
            render_spec_docx(man, recs, dx)
        else:
            render_docx(man, recs, args.模式, dx, args.标来源, args.图片宽, args.图靠右,
                        card_inline=inline,
                        card_per_row=args.答题卡每行, card_blanks=args.答题卡行数)
        print(f"[导出] {dx}（{len(recs)} 题，{args.模式}）")

    card = Path(args.答题卡) if args.答题卡 else None
    if card is None and args.docx and not args.无答题卡 and not inline and not is_spec:
        card = Path(args.docx).parent / f"{vol_title}_答题卡.docx"
    if card:
        render_answer_card(man, recs, card, args.答题卡每行, args.答题卡行数)
        blank_desc = f"每问 {args.答题卡行数} 行（固定）" if args.答题卡行数 else "留白按分值自动"
        print(f"[导出] {card}（答题卡，每行 {args.答题卡每行} 题 / {blank_desc}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
