# -*- coding: utf-8 -*-
"""verify_against_source.py —— 录入后回归自检：题库正文 vs 源文本（MinerU 导出 md / 文字版 md）

用途：录入完成后，逐题比对「已入库的题库正文」与「源文档文本」，归一化后输出相似度与差异片段。
      差异应只出现在：题号、图注/表号、以及录入时刻意修正的 OCR 错字（选项前缀已归一化，不再产生差异）。
      出现其他差异 → 回查该题。

用法：
  python verify_against_source.py --源 "<MinerU导出.md>" --卷 2026河北高考 [--vault <题库库路径>]
                                 [--阈值 0.985] [--差异数 8] [--全部] [--忽略 03,04]
  python verify_against_source.py --源 "<含多卷的.md>" --列出卷   # 先勘察源文件里有哪些卷

  --卷   卷名（须与题库目录名一致）；**源文件含多卷时必填卷次数字以定位**，如「…综合检测卷3」→ 取源标题「综合测试(三)」
  --vault 题库根（优先级：--vault > 环境变量 TNOTE_VAULT > scripts/skill_config.json）
  --全部 打印所有差异（默认每题最多 --差异数 条）

多卷源文件（v1.7.1 起）：单文件含多卷时各卷题号都从 1 重新开始，本脚本**自动按一级标题
  `# 卷名` 分段并定位到 --卷 对应的那一卷**，不再需要手工拆源。
  ⚠ 定位失败会报错退出（不再静默回退）——静默回退正是旧版相似度暴跌 0.05~0.25 的根源。

卷定位的两级策略（v1.10.1 起，见根目录 LOCAL-PATCH.md）：
  ① **卷名带卷次数字**（「…卷N」形态，如「…综合检测卷2」；学年/年级数字不算卷次）
     → 严格按卷次匹配源标题，命中不唯一即报错；
  ② **卷名无卷次数字**（常规阶段测试卷 `{地点}{学年}学年{X}{上|下}{期中|期末}`）→
     无可比对的卷次，此时按「排除答案卷」兜底：剔除标题含「参考答案/答案/评分标准」的卷后，
     剩余 1 卷即采用；剩余多卷（试卷正文被拆成多个一级标题，如「XX质量检测」+「九年级历史试卷(闭卷)」）
     则**合并**这些卷的正文。②为常规「原卷 ＋ 答案同文件」场景，避免每次都要手工拆源。

退出码：存在相似度低于阈值的题 → 1（可用于流水线阻断），否则 0。
"""
import argparse
import difflib
import json
import os
import re
import sys

from qb_bootstrap import bootstrap

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)


def load_vault(cli_vault=None):
    """vault 定位：--vault > TNOTE_VAULT > skill_config.json（统一走 qb_bootstrap）。

    2026-09-30 前本文件有一套私有实现（优先级次序与文档相反），已收敛到
    `qb_bootstrap.resolve_vault`，与其余脚本同一套优先级与退出码（未配置 = 2）。
    """
    from qb_bootstrap import resolve_vault

    return resolve_vault(cli_vault)


def split_volumes(text):
    """把源 md 按**卷级一级标题**切成有序列表 [{标题: str, 正文: str}, ...]。

    ⚠ 单文件含多卷时（如「综合检测卷8套」），各卷题号**都从 1 重新开始**。
      若直接按题号收集，后卷会整体覆盖前卷，相似度暴跌至 0.05~0.25（假阳性）。
      故必须先按卷分隔，再在卷内切题。

    识别规则：行首 `# 标题`（1 个 #）。若全文没有一级标题 → 返回单卷（标题为 ''）。
    """
    lines = text.split('\n')
    heads = []
    for i, ln in enumerate(lines):
        if re.match(r'^#\s+\S', ln):
            heads.append((i, ln.lstrip('#').strip()))
    if not heads:
        return [{'标题': '', '正文': text}]
    vols = []
    for idx, (pos, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        vols.append({'标题': title, '正文': '\n'.join(lines[pos + 1:end])})
    return vols


_CN_DIGIT = {'〇': 0, '一': 1, '二': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def cn2int(s):
    """中文数字 → int；无法解析返回 None。

    显式字表映射，**不依赖字符码或 `int()` 转换**（LOCAL-PATCH 记载的硬约束）。
    支持：一~九／十／十一~十九／二十~九十九／纯数字串（如「二三」→23）。
    """
    if not s:
        return None
    if s == '十':
        return 10
    if all(ch in _CN_DIGIT for ch in s):
        return int(''.join(str(_CN_DIGIT[c]) for c in s))
    if '十' in s:
        hi_s, _, lo_s = s.partition('十')
        if hi_s and hi_s not in _CN_DIGIT:
            return None
        if lo_s and lo_s not in _CN_DIGIT:
            return None
        return (_CN_DIGIT[hi_s] if hi_s else 1) * 10 + (_CN_DIGIT[lo_s] if lo_s else 0)
    return None


def match_juan(vol_title, juan):
    """判断源卷标题是否对应目标卷名。返回 (命中布尔, 说明)。

    按「卷名尾部的卷次数字」与「源标题中的中文/阿拉伯数字」比对。
    匹配不上时返回 False，由调用方决定是报错还是放宽。

    ⚠ 目标卷名侧的卷次**只认紧跟「卷」字的数字**（综合检测卷2 / 模拟卷12 …）。
      v1.10.1 修正：旧版泛取卷名末尾数字串，会把学年（2023-2024）或年级（9上）误当卷次——
      实测「洛阳市涧西区2023-2024学年9上期中」被取成卷次 9，进而与源答案卷标题
      「九年级期中考试答案」（中文「九」→9）**误匹配**，自检整卷错位。
      非「…卷N」形态的卷名一律返回 False → 交由调用方走「排除答案卷」兜底。
    """
    # 目标卷名尾部数字：卷1 / 卷12 / 检测卷3 …（取最后一个连续数字串）
    mj = re.search(r'卷\s*(\d{1,2})', juan)
    if not mj:
        return False, '卷名中未找到卷次数字（非「…卷N」形态）'
    tgt = int(mj.group(1))
    # 源标题中的数字：阿拉伯或中文（中文走显式字表 cn2int）
    # ⚠ 排除年份（≥3 位连续数字）与年级标识（「九年级」「九上」）——只认真正的卷次。
    #   v1.10.1 修正：旧版 `(\d{1,2})` 会把「河南解析与检测2026历史上册 综合测试(二)」
    #   的年份截成 20，导致「…综合检测卷2」永远匹配不上。
    ma = re.search(r'(?<!\d)(\d{1,2})(?!\d)', vol_title)
    if ma:
        got = int(ma.group(1))
    else:
        got = None
        for mc in re.finditer(r'([〇一二三四五六七八九十]{1,3})', vol_title):
            tail = vol_title[mc.end():mc.end() + 2]
            if tail.startswith('年级') or tail[:1] in ('上', '下'):
                continue                      # 「九年级」「九上」等年级标识，跳过
            got = cn2int(mc.group(1))
            if got is None:
                return False, '源卷次「%s」无法解析为数字' % mc.group(1)
            break
        if got is None:
            return False, '源卷标题中未找到卷次数字'
    return (got == tgt), '源卷次 %d vs 目标 %d' % (got, tgt)


# 卷标题中的「答案类」标识：用于在多卷源文件里剔除答案卷（兜底定位用）
ANS_VOL_KEYS = ('参考答案', '答案', '评分标准', '评分细则')


def is_answer_vol(title):
    """卷标题是否为答案卷（参考答案 / 九年级期中考试答案 / 评分标准 …）。"""
    t = title or ''
    return any(k in t for k in ANS_VOL_KEYS)


def juan_has_serial(juan):
    """目标卷名是否带卷次数字（「…卷N」形态）——决定是否强制按卷次匹配。"""
    return bool(re.search(r'卷\s*\d{1,2}', juan or ''))


def _fallback_volumes(vols, want_juan):
    """卷名无卷次数字时的兜底定位（见本文件头「卷定位的两级策略」）。

    返回 (命中列表, 说明)；命中列表为空时，说明为失败原因。
    """
    if juan_has_serial(want_juan):
        return [], '卷名含卷次数字，源中无一卷匹配'
    rest = [v for v in vols if not is_answer_vol(v['标题'])]
    if not rest:
        return [], '源中除答案卷外无试卷正文卷'
    if len(rest) == 1:
        return rest, '排除答案卷后定位到唯一试卷卷'
    return rest, '排除答案卷后合并 %d 段试卷标题' % len(rest)


def split_source(text, want_juan=None):
    """把源 md 按题号切成 {题号: 文本}；题号行形如 `1. xxx`，块止于下一个「结束性」栏目标题。

    ⚠ `## 材料` / `## 材料一` 等是**题目内部标题**（解析册常见），不得当作块结束，
      否则长材料题（17~20）会被截断成一句话，相似度暴跌（河北卷实测题20 仅 0.06）。

    ⚠ 多卷源文件（陷阱 10）：必须传 want_juan（目标卷名），本函数会先按一级标题定位对应卷，
      再在该卷内切题；定位失败则**报错退出**，绝不静默回退（静默回退正是旧版的假阳性根源）。
      卷定位的两级策略（卷次严格匹配 → 卷名无卷次数字时排除答案卷兜底）见本文件头说明。
    """
    END_TITLES = ('参考答案', '考查目标', '试题分析', '试题亮点', '试卷原题',
                  '答案', '解析', '命题立意', '解题思路')
    vols = split_volumes(text)
    if want_juan is not None and len(vols) > 1:
        hit = [v for v in vols if match_juan(v['标题'], want_juan)[0]]
        if len(hit) > 1:
            print('!! 源文件中有 %d 卷匹配 --卷 "%s"，请勿混淆' % (len(hit), want_juan),
                  file=sys.stderr)
            sys.exit(2)
        if hit:
            text = hit[0]['正文']
            print('>> 多卷源文件：已定位卷「%s」（共 %d 卷）' % (hit[0]['标题'], len(vols)))
        else:
            # 兜底：卷名无卷次数字时，按「排除答案卷」定位（常规「原卷 ＋ 答案同文件」场景）
            picked, why = _fallback_volumes(vols, want_juan)
            if not picked:
                print('!! 源文件含 %d 卷，但无一卷匹配 --卷 "%s"（%s）'
                      % (len(vols), want_juan, why), file=sys.stderr)
                print('   各卷标题：%s' % ' | '.join(v['标题'] for v in vols), file=sys.stderr)
                print('   请核对卷次，或改用拆分后的单卷源文件。', file=sys.stderr)
                sys.exit(2)
            text = '\n'.join(v['正文'] for v in picked)
            print('>> 多卷源文件：卷名无卷次数字 → %s（共 %d 卷）' % (why, len(vols)))
            print('   采用卷标题：%s' % ' | '.join(v['标题'] for v in picked))
    elif want_juan is not None and len(vols) == 1 and vols[0]['标题']:
        ok, why = match_juan(vols[0]['标题'], want_juan)
        if not ok:
            if not juan_has_serial(want_juan) and not is_answer_vol(vols[0]['标题']):
                print('>> 卷名无卷次数字，单卷源直接采用（源标题「%s」）' % vols[0]['标题'])
            else:
                print('!! 单卷源文件标题「%s」与 --卷 "%s" 卷次不符（%s）'
                      % (vols[0]['标题'], want_juan, why), file=sys.stderr)
                sys.exit(2)

    src = {}
    cur, buf = None, []
    for ln in text.split('\n'):
        s = ln.strip()
        # ⚠ 题号行可能被 MinerU 识别成 markdown 标题（如 `## 17. 阅读材料…`）——
        #   2026-09-28 四川卷实测：不容许标题前缀时 17/19 题整块丢失、16/18 题被后续内容撑爆
        #   （相似度 0.24/0.42 假阳性）。故题号匹配容许 `#`~`######` 前缀。
        m = re.match(r'^(?:#{1,6}[ \t]*)?(\d{1,2})[.．]\s*(.*)$', s)
        if m:
            if cur is not None:
                src[cur] = '\n'.join(buf)
            cur, buf = int(m.group(1)), [m.group(2)]
            continue
        if s.startswith('## ') and cur is not None:
            title = s.lstrip('#').strip()
            if any(title.startswith(k) for k in END_TITLES):
                src[cur] = '\n'.join(buf)
                cur, buf = None, []
            else:
                buf.append(s.lstrip('#').strip())
            continue
        if cur is not None:
            buf.append(ln)
    if cur is not None:
        src[cur] = '\n'.join(buf)
    return src


def norm(t):
    """归一化：去图片引用/图注/表号/题号/HTML 标签/空白，选项前缀 `(A)`→`A.`，全角标点转半角。"""
    t = re.sub(r'!\[.*?\]\(.*?\)', '', t)
    t = re.sub(r'图\s*\d+\S*', '', t)
    t = re.sub(r'表\s*\d+', '', t)
    t = t.replace('（　）', '').replace('（ ）', '')
    t = re.sub(r'^\s*\d+[.．]', '', t, flags=re.M)
    t = re.sub(r'^\s*#{1,6}\s*', '', t, flags=re.M)
    t = re.sub(r'<[^>]+>', '', t)
    t = re.sub(r'\(([A-Da-d])\)', r'\1.', t)          # 生成侧 (A) → A.，与源侧统一
    t = re.sub(r'[\s\u3000]+', '', t)
    for a, b in [('（', '('), ('）', ')'), ('，', ','), ('。', '.'), ('：', ':'),
                 ('；', ';'), ('“', '"'), ('”', '"'), ('、', ','), ('？', '?'), ('！', '!')]:
        t = t.replace(a, b)
    return t


def read_body(path):
    t = open(path, encoding='utf-8').read()
    if t.startswith('---'):
        parts = t.split('---', 2)
        return parts[2] if len(parts) >= 3 else t
    return t


def find_answer_files(vault, juan):
    """在 vault 下定位 {卷名}/{卷名}_{题型}/*.md"""
    hits = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]   # 跳过隐藏目录（.trash/.obsidian/.dedupe…）
        for f in files:
            if not f.endswith('.md'):
                continue
            if os.path.basename(root).startswith(juan + '_'):
                hits.append(os.path.join(root, f))
    return sorted(hits)


def main():
    bootstrap()
    ap = argparse.ArgumentParser()
    ap.add_argument('--源', dest='source', required=True, help='源文本 md（MinerU 导出或文字版）')
    ap.add_argument('--卷', dest='juan', default=None, help='卷名（与题库目录一致）；--列出卷 时可省')
    ap.add_argument('--vault', dest='vault', default=None)
    ap.add_argument('--阈值', dest='thr', type=float, default=0.985)
    ap.add_argument('--差异数', dest='ndiff', type=int, default=8)
    ap.add_argument('--全部', dest='all_diff', action='store_true')
    ap.add_argument('--忽略', dest='ignore', default='',
                    help='已声明差异的题号（逗号分隔），如 03,04,05 —— 仍打印差异但不计入不达标')
    ap.add_argument('--列出卷', dest='list_vols', action='store_true',
                    help='只列出源文件中的卷级标题后退出（多卷源文件勘察用）')
    args = ap.parse_args()
    ignore = set()
    for part in re.split(r'[,，\s]+', args.ignore.strip()):
        if part.isdigit():
            ignore.add(int(part))

    if args.list_vols:
        vols = split_volumes(open(args.source, encoding='utf-8').read())
        print('源文件共 %d 卷：' % len(vols))
        for i, v in enumerate(vols, 1):
            print('  %2d. %s' % (i, v['标题'] or '(无标题)'))
        sys.exit(0)
    if not args.juan:
        ap.error('--卷 为必填（仅 --列出卷 模式可省）')

    vault = load_vault(args.vault)
    src = split_source(open(args.source, encoding='utf-8').read(), want_juan=args.juan)
    files = find_answer_files(vault, args.juan)
    if not files:
        print('!! 未找到卷 %s 的题库文件（vault=%s）' % (args.juan, vault), file=sys.stderr)
        sys.exit(2)

    bad, missing_img = [], []
    for fp in files:
        m = re.search(r'(\d{2,})\.md$', os.path.basename(fp))
        if not m:
            continue
        no = int(m.group(1))
        gen = norm(read_body(fp))
        ori = norm(src.get(no, ''))
        if not ori:
            print('-- %02d 源文本未找到该题号，跳过' % no)
            continue
        sm = difflib.SequenceMatcher(None, gen, ori)
        ratio = sm.ratio()
        diffs = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != 'equal':
                diffs.append('%s 生成[%s] ↔ 源[%s]' % (tag, gen[i1:i2][:40], ori[j1:j2][:40]))
        flag = 'OK ' if ratio >= args.thr or no in ignore else '!! '
        if ratio < args.thr and no not in ignore:
            bad.append((no, ratio))
        print('%s%02d 相似度 %.4f' % (flag, no, ratio))
        shown = diffs if args.all_diff else diffs[:args.ndiff]
        for d in shown:
            print('      ', d)
        for mm in re.finditer(r'!\[\]\(images/([^)]+)\)', read_body(fp)):
            ip = os.path.join(os.path.dirname(fp), 'images', mm.group(1))
            if not os.path.exists(ip):
                missing_img.append('%s -> %s' % (fp, ip))

    print('\n─── 自检小结 ───')
    print('题数：%d' % len(files))
    if bad:
        print('相似度低于 %.3f 的题：%s' % (args.thr, ', '.join('%02d(%.3f)' % (n, r) for n, r in bad)))
        print('提示：确认差异是否全部属于「选项前缀/题号/图注表号/刻意修正的 OCR 错字」，否则回查。')
    else:
        print('相似度全部达标（阈值 %.3f）' % args.thr)
    print('图片缺失：', missing_img if missing_img else '无')
    sys.exit(1 if bad or missing_img else 0)


if __name__ == '__main__':
    main()
