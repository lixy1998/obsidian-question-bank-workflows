# -*- coding: utf-8 -*-
"""import_from_md.py — 源头 md → 录入 JSON：批量回填「题干」「选项」。

用法：
  python import_from_md.py --源 <提取出的 md> --json <录入 JSON> [--skip 7,18] [--dry-run]

**链路B（文字版 docx/md）批量录入的第二步**——先用 `read_docx.py` 把试卷提取成 md，
再用本脚本按题号把题干与选项回填进录入 JSON，免去逐题复制粘贴；JSON 的其余字段
（册别/单元/课/答案/解析/图片…）保持不动。

自动处理（均为录入实测踩过的坑）：
  · **剔除卷首「注意事项」段**——其「1. 2. 3.」编号行会被正则误认成题号，
    按「首次出现」建块时会覆盖真正的第 1~3 题。终止判据认**三种节标题形态**
    （`一、选择题` / `选择题(共20小题,20分)` / `第一部分 选择题`，容许 `#`、`**`、`>` 前缀）；
    **整卷无节标题时降级**为按说明行特征剔除并告警——判据失配**不等于**无需剔除；
  · **截断答案区**——按「此资料来源于 / 历史试题参考答案 / 参考答案 / 【参考答案】/
    历史答案 / 试题答案 / 答案与解析 / 评分标准」等标记与紧凑答案行（`1-5:BABDA`）中
    **最靠前者**截断（试卷区恒在答案区之前），并**回退到该行行首**
    （标题把标记夹在中间时，硬切会在正文尾部残留半行标题）；
  · **块内答案兜底**——答案紧跟试题、**无独立标题**的源文件（省级《学科评价报告》形态）：
    答案题号写作「19（15分）」「(1)【答案】」，既不匹配建块正则、又不含上述标记，
    于是整段答案并入试卷区最后一个块 → 按块内特征词（`【答案】` `【解析】` `替代答案`
    `酌情给分` 等）截断到最后一个「（N分）」之后；
  · **选择题**切出题干与 A–D 选项：容忍两项紧邻、字母后无空格（`理性C.人物…`）；
  · **非选择题**删块首整题分值（`（20分）`，可位于行首或行尾），**保留各小问分值**；
  · **图片引用**按题号规范为 `images/{题号}_{n}.{ext}`，**保留原扩展名**
    （库内不可显示格式另有 `fix_image_formats.py` 兜底）；**题干与选项中的图引用都处理**，
    编号在两者之间接续；**选项内出现图引用时告警**——多因原卷图被排到上一题的选项行内，
    须人工核对图片归属。

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

ANSWER_MARKERS = ("此资料来源于", "历史试题参考答案", "参考答案", "【参考答案】",
                  "答案与解析", "【答案与解析】", "历史答案", "试题答案",
                  "评分标准", "【评分标准】")
# 块内答案痕迹：题型分析类源文件（如省级《学科评价报告》）常把「题干 + 答案 + 解析」**逐题混排**，
# 且部分题答案**完全不带标记**（如「（1）原因：…」「示例一：…」），故除特征词外还需结构判据兜底。
ANSWER_TAIL_KWS = ("【答案】", "【解析】", "【考查意图】", "【答题情况】", "【教学建议】",
                   "替代答案", "酌情给分", "（不作为评分依据）", "本卷为")
ANSWER_TAIL_PATTERNS = (
    r"\n\s*示例[一二三四五六七]\s*[：:]",                        # 论述/开放题的参考答案示例
    r"\n\s*升华\s*[：:]",                                        # 命题人「升华」段
    r"\n\s*[（(]\s*\d+\s*[）)]\s*[^\n：:，,。；;]{1,12}[：:]",   # 「（1）原因：」式答案短标签
)
ANSWER_ROW_RE = re.compile(r"\n[ \t]*\d{1,2}[-–]\d{1,2}[：:]")
NOTE_RE = re.compile(r"[注註]意事项")
# 大题 / 节标题。三种实测形态：
#   ① 「一、选择题」（序号 + 顿号，其后可带题型名与分值）
#   ② 「选择题(共20小题,20分)」（无序号：题型名 + 括号分值说明）
#   ③ 「第一部分 选择题」
# 容许行首 md 前缀（#、**、>）与行尾加粗；**必须整行命中**，否则正文里的「选择题」
# 三字、材料引文里的「一、」都会误切块。
_SECT_NAMES = (r"单项选择题|选择题|非选择题|材料解析题|材料分析题|材料题|论述题|"
               r"综合探究题|探究题|填空题|判断题|简答题")
_SECT_PFX = r"[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*[ \t]*)?(?:>[ \t]*)?(?:\*[ \t]*)?"
_SECT_BODY = (r"(?:[一二三四五六七八九十]+\s*、[^\n]*"                       # ①
              r"|(?:第[一二三四五六七八九十]部分\s*[·、．.]?\s*)?(?:"
              + _SECT_NAMES + r")(?:[（(][^）)\n]{0,40}[）)])?)")             # ②③
_SECT_TAIL = r"[ \t]*(?:\*\*)?[ \t]*\r?"
SECT_HEAD_RE = re.compile(r"^" + _SECT_PFX + _SECT_BODY + _SECT_TAIL + r"$")
SECTION_RE = re.compile(r"\n" + _SECT_PFX + _SECT_BODY + _SECT_TAIL + r"(?=\n|$)")
QNO_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?(\d{1,2})[.．][ \t]*(.*)$")
OPT_START_RE = re.compile(r"(?:^|\n)[ \t]*A[.．]")
IMG_RE = re.compile(r"!\[[^\]]*\]\(images/[^)]+\)")
SCORE_RE = re.compile(r"(?<!\d)[（(]\s*\d+\s*分\s*[）)]")   # 排除「17（12分）」这类答案区题号+分值

# 卷首「注意事项」只在前 N 行内识别——正文材料引文里的「注意事项」三字不得触发剔除
NOTE_WINDOW_LINES = 20
# 卷首说明行的用语特征（无节标题时降级剔除用；取自各卷实测用语）
NOTE_LINE_KWS = ("答题前", "答题卡", "考生", "姓名", "考号", "准考证", "2B", "铅笔",
                 "涂卡", "涂黑", "考试结束", "交回", "只交", "试卷上", "无效",
                 "本卷共", "满分", "分值", "建议用时", "务必", "注意")
# 题干里绝不该出现的卷首说明字样（回填后自检告警用）
NOTE_STEM_KWS = ("答题前", "答题卡", "2B", "涂卡", "准考证", "只交", "交回", "考试结束")


def strip_answer_area(md: str) -> str:
    """截断答案区（取各标记中首次出现的最靠前者），并回退到该行行首。

    ⚠ 必须回退到行首：答案区标题常把标记夹在中间（如「浙江省2025年1月历史首考参考答案」），
    按标记位置硬切会在正文尾部残留半行标题（实测末题题干尾部多出「浙江省2025年1月历史首考」）。
    """
    cands = [md.find(m) for m in ANSWER_MARKERS]
    m = ANSWER_ROW_RE.search(md)
    if m:
        cands.append(m.start())
    cands = [c for c in cands if c > 0]
    if not cands:
        return md
    pos = min(cands)
    nl = md.rfind("\n", 0, pos)
    return md[: nl if nl > 0 else pos]


def trim_answer_tail(block: str) -> str:
    """块内答案痕迹兜底截断（截到最后一个「（N分）」之后）。

    适用于**答案紧跟试题、且无独立标题**的源文件——省级《学科评价报告》常把
    「题干 + 答案 + 解析」**逐题混排**，答案题号写作「19（15分）」「(1)【答案】」，
    既不匹配 QNO_RE 建块、又不含 ANSWER_MARKERS，于是整段答案被并入同一题块。

    判据按「取最早命中」组合：① 特征词（ANSWER_TAIL_KWS）；
    ② 结构（ANSWER_TAIL_PATTERNS：示例N / 升华 / 「（1）原因：」式短标签）。
    命中后截到该点之前**最后一个「（N分）」之后**，保证设问完整。
    ⚠ 仍属启发式：判据全不命中时原样返回，须靠回填后自查（选项数 / 正文含答案词）兜底。
    """
    cands = [block.find(k) for k in ANSWER_TAIL_KWS if k in block]
    for pat in ANSWER_TAIL_PATTERNS:
        m = re.search(pat, block)
        if m:
            cands.append(m.start())
    pos = min(cands, default=-1)
    if pos < 0:
        return block
    head = block[:pos]
    # 只有 head 中除「整题分值」外**还有小问分值**时才回退截断；否则整段保留
    # （否则会把题干截成只剩首行「阅读材料，完成下列要求。（12分）」）
    matches = list(SCORE_RE.finditer(head))
    if len(matches) >= 2:
        return head[: matches[-1].end()]
    return head.rstrip()


def _looks_like_question(s: str) -> bool:
    """行是否像题干/选项（降级剔除时用来判断说明段到哪里结束）。"""
    return ("（　　）" in s or "（）" in s or "()" in s
            or bool(re.match(r"^[ABCD][.．]", s))
            or len(s) > 60)


def strip_notes_area(md: str) -> str:
    """删除卷首「注意事项」段。

    终止判据（取最早命中）：
      ① **节标题**——`一、选择题`／`选择题(共20小题,20分)`／`第一部分 选择题`
         （SECT_HEAD_RE，容许 `#`/`**`/`>` 前缀）；
      ② **降级**——整卷找不到节标题时，从「注意事项」起按「非题目行」逐行剔除，
         到第一个像题目的行为止，并**必报告警**。

    ⚠ 两条铁律：
      · **判据失配 ≠ 无需剔除**。旧实现 `if not m2: return md` 把「找不到终止标记」
        当成「没有说明段」，实测 4 卷共 9 题的真题干被卷首说明顶掉（2026-10-26，
        该批卷节标题写作「选择题(共20小题,20分)」而非「一、选择题」）。
      · 「注意事项」只在**前 NOTE_WINDOW_LINES 行**内识别，正文引文里的同名字样不触发。
    """
    lines = md.split("\n")
    idx = next((i for i, l in enumerate(lines[:NOTE_WINDOW_LINES])
                if NOTE_RE.search(l)), None)
    if idx is None:
        return md

    # ① 节标题边界：删「注意事项」起到该标题行之前
    for j in range(idx + 1, len(lines)):
        if SECT_HEAD_RE.match(lines[j]):
            return "\n".join(lines[:idx] + lines[j:])

    # ② 降级：无节标题 → 按说明行特征剔除（保守：命中不足 2 行视为无法定位）
    end, dropped = idx + 1, 0
    for j in range(idx, min(len(lines), idx + 40)):
        s = lines[j].strip()
        if not s:
            end = j + 1
            continue
        if _looks_like_question(s):
            break
        end, dropped = j + 1, dropped + 1
    if dropped < 2:
        print("[卷首] ⚠ 命中「注意事项」但未能定位说明段边界——"
              "请人工核对第 1~3 题题干不是说明文字")
        return md
    print(f"[卷首] 未找到节标题，按说明行特征降级剔除 {dropped} 行——"
          f"必核第 1~3 题题干未被说明文字顶替")
    return "\n".join(lines[:idx] + lines[end:])


def load_blocks(md: str) -> dict:
    """按题号切块（保留**首次**出现的块——试卷区在答案区之前），并对块内答案痕迹兜底截断。"""
    blocks, cur = {}, None
    for line in md.split("\n"):
        m = QNO_RE.match(line)
        if m:
            cur = int(m.group(1))
            if cur not in blocks:
                blocks[cur] = m.group(2)
        elif cur is not None:
            blocks[cur] = blocks[cur] + "\n" + line
    return {k: trim_answer_tail(v) for k, v in blocks.items()}


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


def renumb_imgs(text: str, qno: int, start: int = 0):
    """把 text 中的图片引用规范为 images/{qno}_{n}.{ext}（n 从 start+1 起）。

    返回 (新文本, 结束编号)——编号需在「题干 → 选项」之间**接续**，
    调用方拿到上一步的结束编号后传给下一步。
    """
    counter = [start]

    def _img(_m):
        counter[0] += 1
        ext = _m.group(0).rsplit(".", 1)[-1].rstrip(")").strip().lower()
        return f"![](images/{qno}_{counter[0]}.{ext})"

    return IMG_RE.sub(_img, text), counter[0]


def norm_body(text: str, qno: int, strip_total_score: bool = False):
    """整题分值剔除 + 图片引用规范化 + 空行收敛。返回 (文本, 结束编号)。"""
    s = text.strip()
    if strip_total_score:
        lines = s.split("\n")
        if lines:
            head = re.sub(r"^（\d+分）\s*", "", lines[0])
            head = re.sub(r"\s*（\d+分）\s*$", "", head)
            lines[0] = head
            s = "\n".join(lines).strip()
    s, n = renumb_imgs(s, qno, 0)
    return re.sub(r"\n{3,}", "\n\n", s).strip(), n


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
            stem_n, n = norm_body(stem, qno)
            rec["题干"] = stem_n
            new_opts = []
            for o in opts:                          # 选项内的图引用同样编号（与题干接续，避免重名）
                o2, n = renumb_imgs(o, qno, start=n)
                new_opts.append(o2)
            rec["选项"] = new_opts
            if not opts:
                print(f"  ⚠ 序号 {qno}：未切出选项，请人工核对")
            elif len(new_opts) != 4:
                print(f"  ⚠ 序号 {qno}：切出 {len(new_opts)} 个选项（应为 4）——"
                      f"多为选项并排/漏项/串题，回查源文")
            if any("images/" in o for o in new_opts):
                print(f"  ⚠ 序号 {qno}：选项内含图片引用——多见于原卷图被排到上一题的选项行内，"
                      f"请人工核对图片归属（不属本题的图须移到正确题号）")
            filled += 1
        else:
            rec["题干"] = norm_body(blk, qno, strip_total_score=True)[0]
            filled += 1

        # 第二道防线：题干含卷首说明字样 → 说明「注意事项」段没剔干净、顶掉了真题干
        # （选项数自检查不出这种情况，scan 也查不出，只能在此告警）
        hit = [k for k in NOTE_STEM_KWS if k in rec.get("题干", "")]
        if hit:
            print(f"  ⚠ 序号 {qno}：题干含卷首说明字样（{'/'.join(hit)}）——"
                  f"疑为「注意事项」段顶掉真题干，请人工重写本题")

    print(f"\n回填 {filled} 题｜跳过 {skipped} 题｜源中缺块 {missing} 题")
    if args.dry_run:
        print("（--dry-run，未写回 JSON）")
        return
    tgt.write_text(json.dumps(data if not single else data[0], ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[写出] {tgt}")


if __name__ == "__main__":
    main()
