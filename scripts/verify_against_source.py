# -*- coding: utf-8 -*-
"""verify_against_source.py —— 录入后回归自检：题库正文 vs 源文本（MinerU 导出 md / 文字版 md）

用途：录入完成后，逐题比对「已入库的题库正文」与「源文档文本」，归一化后输出相似度与差异片段。
      差异应只出现在：题号、图注/表号、以及录入时刻意修正的 OCR 错字（选项前缀已归一化，不再产生差异）。
      出现其他差异 → 回查该题。

用法：
  python verify_against_source.py --源 "<MinerU导出.md>" --卷 2026河北高考 [--vault D:/Documents/TNote]
                                 [--阈值 0.985] [--差异数 8] [--全部]

  --卷   卷名（须与题库目录名一致）
  --vault 题库根（默认读本 skill 的 scripts/skill_config.json，其次环境变量 TNOTE_VAULT）
  --全部 打印所有差异（默认每题最多 --差异数 条）

退出码：存在相似度低于阈值的题 → 1（可用于流水线阻断），否则 0。
"""
import argparse
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)


def load_vault(cli_vault=None):
    if cli_vault:
        return cli_vault
    env = os.environ.get('TNOTE_VAULT')
    if env:
        return env
    cfg = os.path.join(HERE, 'skill_config.json')
    if os.path.exists(cfg):
        try:
            data = json.load(open(cfg, encoding='utf-8'))
            if data.get('vault'):
                return data['vault']
        except Exception:
            pass
    print('!! 未配置题库库（skill_config.json / TNOTE_VAULT / --vault）', file=sys.stderr)
    sys.exit(2)


def split_source(text):
    """把源 md 按题号切成 {题号: 文本}；题号行形如 `1. xxx`，块止于下一个「结束性」栏目标题。

    ⚠ `## 材料` / `## 材料一` 等是**题目内部标题**（解析册常见），不得当作块结束，
      否则长材料题（17~20）会被截断成一句话，相似度暴跌（河北卷实测题20 仅 0.06）。
    """
    END_TITLES = ('参考答案', '考查目标', '试题分析', '试题亮点', '试卷原题',
                  '答案', '解析', '命题立意', '解题思路')
    src = {}
    cur, buf = None, []
    for ln in text.split('\n'):
        s = ln.strip()
        m = re.match(r'^(\d{1,2})[.．]\s*(.*)$', s)
        if m and not s.startswith('##'):
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
        for f in files:
            if not f.endswith('.md'):
                continue
            if os.path.basename(root).startswith(juan + '_'):
                hits.append(os.path.join(root, f))
    return sorted(hits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--源', dest='source', required=True, help='源文本 md（MinerU 导出或文字版）')
    ap.add_argument('--卷', dest='juan', required=True, help='卷名（与题库目录一致）')
    ap.add_argument('--vault', dest='vault', default=None)
    ap.add_argument('--阈值', dest='thr', type=float, default=0.985)
    ap.add_argument('--差异数', dest='ndiff', type=int, default=8)
    ap.add_argument('--全部', dest='all_diff', action='store_true')
    ap.add_argument('--忽略', dest='ignore', default='',
                    help='已声明差异的题号（逗号分隔），如 03,04,05 —— 仍打印差异但不计入不达标')
    args = ap.parse_args()
    ignore = set()
    for part in re.split(r'[,，\s]+', args.ignore.strip()):
        if part.isdigit():
            ignore.add(int(part))

    vault = load_vault(args.vault)
    src = split_source(open(args.source, encoding='utf-8').read())
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
