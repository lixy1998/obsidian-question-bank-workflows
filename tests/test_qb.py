# -*- coding: utf-8 -*-
"""tests/test_qb.py — Obsidian历史题库搭建与组卷 的回归测试。

运行（在 skill 根目录）：
    python tests/test_qb.py                 # 全部
    python tests/test_qb.py -v              # 逐条显示
    python -m unittest discover -s tests    # 等价

设计原则：
- **不碰真实题库**：所有会读写 vault 的测试都在临时目录里自建一个小题库，
  通过子进程设置 TNOTE_VAULT 指向它（qb_schema 在导入期解析 vault，故必须用子进程）。
- **离线**：不依赖 MinerU / Obsidian / 网络。
- 覆盖：字段与词表纯函数、题目文件判定、scan 全链路、go→export→backtrace 往返、脚本可用性。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

# 带 argparse 的 CLI 脚本（`--help` 必须可用）。qb_schema.py / qb_bootstrap.py 是库模块、
# 没有 argparse，**不要**对它们执行（历史事故：无 argparse 的脚本会把 `--help` 当普通参数执行 main）。
CLI_SCRIPTS = [
    "backtrace_paper.py", "check_note_links.py", "dedupe_questions.py",
    "export_student_paper.py", "fix_arabic_numerals.py", "fix_image_formats.py",
    "gen_index.py", "gen_problem.py", "linkify_citations.py", "pull_pdf_images.py",
    "read_docx.py", "scan_lesson.py", "scan_missed.py", "scan_questions.py",
    "scan_volume.py", "setup.py", "verify_against_source.py", "verify_paper_layout.py",
]
LIB_MODULES = ["qb_schema.py", "qb_bootstrap.py"]

# export_student_paper.py 拆出的内部模块包（新增/删除模块必须同步这里）
PAPERKIT_MODULES = [
    "__init__.py", "paths.py", "manifest.py", "images.py", "markup.py",
    "slots.py", "sections.py", "spec.py", "md_render.py", "balance.py",
    "checkup.py", "docx_kit.py",
]

# 测试临时工作区放在 skill 内（tests/_tmp）：沙箱的系统临时目录在退出清理时会触发
# WinError 5；放工作区内可确保可写可删（目录已入 .gitignore）。
TMP_ROOT = HERE / "_tmp"

# 解释器：优先当前解释器；测试脚本自身用 subprocess 跑 CLI 时也用它
PY = sys.executable


def sub_env(vault=None):
    """子进程环境：强制 UTF-8，保证测试不依赖调用方的 shell 编码设置。

    脚本在**管道输出**时会自切 UTF-8，但如果调用方 shell 已设 PYTHONIOENCODING=gbk 或
    其他值，子进程会沿用而与本测试的 `encoding="utf-8"` 解码冲突。这里显式钉死，
    测试结果才与运行环境无关。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if vault is not None:
        env["TNOTE_VAULT"] = str(vault)
    return env


def _docx_all_text(path):
    """docx 全文（拼所有 XML 部件后去标签）——用于断言渲染内容不丢。

    直接读 zip 内 XML，不依赖 python-docx，便于跨环境稳定。
    """
    import zipfile
    parts = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".xml") and ("document" in name or "header" in name
                                          or "footer" in name):
                parts.append(z.read(name).decode("utf-8", "replace"))
    joined = "".join(parts)
    # <w:t> 里的文字才是可见文本；再整体去标签以兜底
    texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", joined, re.S)
    return re.sub(r"<[^>]+>", "", "".join(texts)) if texts else re.sub(r"<[^>]+>", "", joined)


def q(序号="01", 题型="选择题", **over):
    """构造一条最小合法题目记录（可覆盖任意字段）。"""
    rec = {
        "学段": "初中", "类别": "2中考模拟题", "卷名": "2026测试卷", "序号": 序号,
        "册别": ["7上"], "单元": ["第2单元 夏商周时期"], "课": ["第4课 夏商西周王朝的更替"],
        "小节": ["商朝的统治"], "知识点": ["青铜器"], "核心素养": ["史料实证-实物史料的价值"],
        "素养层次水平": ["史料实证-层次1-辨识可信史料"], "关键能力": ["A2-说明"],
        "题型": 题型, "难度": "中", "分值": 1,
        "来源": ["2026测试卷", "模拟卷"], "年份": 2026, "考区": "河南",
        "答案": "C", "解析": "测试用解析。",
        "题干": "司母戊鼎是迄今世界上出土的最重的青铜器，它主要反映了(　　)",
        "选项": ["精益求精的态度", "拼搏奉献的风貌", "集体协作的精神", "国家权力的强大"],
    }
    rec.update(over)
    return rec


class TempVaultMixin:
    """每条测试一个全新最小题库，并把 TNOTE_VAULT 指向它（测试间互不干扰）。"""

    def setUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        # ⚠ 不要用 tempfile.mkdtemp：它按 0o700 建目录，在受限工作区里该目录下
        # 再建子目录会被拒（PermissionError WinError 5）；os.makedirs 无此问题。
        self.vault = TMP_ROOT / ("vault_" + uuid.uuid4().hex[:10])
        os.makedirs(self.vault, exist_ok=True)
        for d in ("初中-1中考真题", "初中-2中考模拟题", "初中-3阶段测试",
                  "高中-1高考真题", "高中-2高考模拟题", "高中-3阶段测试", "0-Template-模板"):
            (self.vault / d).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def make_question(self, 序号="01", **over):
        """用 gen_problem.py 把一条记录写进临时题库，返回题目文件路径。

        `**over` 直接透传给 `q()`，用于覆盖册别/单元/课/答案等字段。
        """
        rec = q(序号=序号, **over)
        tmpjson = self.vault / f"_in_{rec['卷名']}_{rec['题型']}_{rec['序号']}.json"
        tmpjson.write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([PY, str(SCRIPTS / "gen_problem.py"), str(tmpjson)],
                           capture_output=True, text=True, encoding="utf-8",
                           env=sub_env(self.vault))
        self.assertEqual(r.returncode, 0, "gen_problem 失败：%s\n%s" % (r.stdout, r.stderr))
        tmpjson.unlink(missing_ok=True)
        d = (self.vault / f"{rec['学段']}-{rec['类别']}" / rec["卷名"]
             / f"{rec['卷名']}_{rec['题型']}")
        f = d / f"{rec['序号']}.md"
        self.assertTrue(f.exists(), "未生成题目文件：%s" % f)
        return f

    def run_cli(self, script, *args, cwd=None, expect=None):
        r = subprocess.run([PY, str(SCRIPTS / script), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           cwd=str(cwd or SCRIPTS),
                           env=sub_env(self.vault))
        if expect is not None:
            self.assertEqual(r.returncode, expect,
                             "%s 退出码 %s ≠ %s\nstdout=%s\nstderr=%s"
                             % (script, r.returncode, expect, r.stdout[-800:], r.stderr[-800:]))
        return r


class TestSchemaPure(unittest.TestCase):
    """qb_schema 的纯函数与常量（最稳的一层，回归全靠它）。"""

    def setUp(self):
        import qb_schema
        self.s = qb_schema

    def test_enums(self):
        self.assertEqual(self.s.TYPES, ["选择题", "材料解析题", "论述题", "综合探究题", "填空题"])
        self.assertEqual(self.s.DIFFICULTIES, ["易", "中", "难"])
        self.assertEqual(len(self.s.BOOKS), 13)
        self.assertIn("选修1", self.s.BOOKS)
        self.assertIn("选修2", self.s.BOOKS)
        self.assertEqual(self.s.TYPE_ALIASES["小论文"], "论述题")
        self.assertNotIn("小论文", self.s.TYPES)

    def test_as_list(self):
        self.assertEqual(self.s.as_list(None), [])
        self.assertEqual(self.s.as_list(""), [])
        self.assertEqual(self.s.as_list("7上"), ["7上"])
        self.assertEqual(self.s.as_list("7上;8上"), ["7上", "8上"])
        self.assertEqual(self.s.as_list(["7上", "8上"]), ["7上", "8上"])
        self.assertEqual(self.s.as_list(["7上", "  "]), ["7上"])

    def test_norm_for_dedupe_去标点与图片(self):
        n = self.s.norm_for_dedupe
        self.assertEqual(n("司母戊鼎，重达832.84千克！"), "司母戊鼎重达83284千克")
        self.assertEqual(n("![](images/a.jpg)题干（2分）"), "题干")
        self.assertEqual(n("ＡＢＣ"), "abc")          # 全角转半角 + 小写

    def test_authority_key_真题优于模拟优于阶段测试(self):
        z = {"册别": ["7上"], "年份": 2020, "卷名": "2020河南中考"}
        m = {"册别": ["7上"], "年份": 2021, "卷名": "2021洛阳一模"}
        j = {"册别": ["7上"], "年份": 2022, "卷名": "2022洛阳期末"}
        kz = self.s.authority_key(z, "x/初中-1中考真题/2020河南中考/a/01.md")
        km = self.s.authority_key(m, "x/初中-2中考模拟题/2021洛阳一模/a/01.md")
        kj = self.s.authority_key(j, "x/初中-3阶段测试/2022洛阳期末/a/01.md")
        self.assertLess(kz, km)
        self.assertLess(km, kj)

    def test_source_pairs_成对平铺(self):
        rec = {"来源": ["A卷", "真题", "B卷", "模拟卷"]}
        self.assertEqual(self.s.source_pairs(rec), [("A卷", "真题"), ("B卷", "模拟卷")])
        # 奇数长度不丢信息：末项单独成对、类型留空
        self.assertEqual(self.s.source_pairs({"来源": ["A卷", "真题", "B卷"]}),
                         [("A卷", "真题"), ("B卷", "")])

    def test_split_frontmatter_含特殊字符与无fm(self):
        fm, body, err = self.s.split_frontmatter(
            "---\nid: 2026测试卷-01\n解析: |\n  第一行\n  第二行：「引号」与: 冒号\n---\n正文一\n正文二\n")
        self.assertIsNone(err)
        self.assertEqual(fm["id"], "2026测试卷-01")
        self.assertIn("第二行", fm["解析"])
        self.assertEqual(body.strip(), "正文一\n正文二")
        fm2, body2, err2 = self.s.split_frontmatter("没有 frontmatter 的正文")
        self.assertEqual(fm2, {})
        self.assertIsNone(err2)
        self.assertEqual(body2, "没有 frontmatter 的正文")

    def test_split_frontmatter_非法YAML返回错误而非抛出(self):
        fm, body, err = self.s.split_frontmatter("---\nid: [未闭合\n---\n正文\n")
        self.assertEqual(fm, {})
        self.assertIsNotNone(err)
        self.assertIn("YAML", err)

    def test_strip_comments(self):
        self.assertEqual(self.s.strip_comments("正文<!-- 模板说明 -->尾部").strip(), "正文尾部")

    def test_build_body_选择题加选项前缀(self):
        body = self.s.build_body(q())
        self.assertIn("司母戊鼎", body)
        for sym in ("(A)", "(B)", "(C)", "(D)"):
            self.assertIn(sym, body)

    def test_yaml往返(self):
        rec = q()
        text = self.s.dump_yaml(rec)
        fm, _body, err = self.s.split_frontmatter("---\n" + text + "---\n正文\n")
        self.assertIsNone(err)
        self.assertEqual(fm["册别"], rec["册别"])
        self.assertEqual(fm["课"], rec["课"])
        self.assertEqual(str(fm["分值"]), str(rec["分值"]))

    def test_question_path_路径规则(self):
        p = self.s.question_path(q())
        self.assertTrue(str(p).endswith(
            os.path.join("初中-2中考模拟题", "2026测试卷", "2026测试卷_选择题", "01.md")),
            str(p))

    def test_load_book_index_覆盖13册且高中含选修(self):
        idx = self.s.load_book_index()
        for b in self.s.BOOKS:
            self.assertIn(b, idx, "词表缺册别 %s" % b)
            self.assertTrue(idx[b], "词表册别 %s 无课名" % b)
        self.assertEqual(len(idx["选修1"]), 7)
        self.assertEqual(len(idx["选修2"]), 7)
        self.assertTrue(any(c.startswith("专题") for c in idx["选修1"]))
        self.assertTrue(any(c.startswith("第") for c in idx["7上"]))

    def test_load_lesson_units_层级可解析(self):
        lu = self.s.load_lesson_units()
        self.assertIn(("7上", "第4课 夏商西周王朝的更替"), lu)
        self.assertEqual(lu[("7上", "第4课 夏商西周王朝的更替")], "第2单元 夏商周时期")

    def test_load_method_names_15个(self):
        self.assertEqual(len(self.s.load_method_names()), 15)


class TestIsQuestionFile(unittest.TestCase):
    """is_question_file 的路径判据（防「题库外文件被当题」回归）。"""

    def setUp(self):
        import qb_schema
        self.s = qb_schema

    def test_题库目录内合法文件为真(self):
        self.assertTrue(self.s.is_question_file("初中-1中考真题/2026河南中考/2026河南中考_选择题/01.md"))

    def test_模板目录文件为假(self):
        # 历史回归点：曾把 0-Template-模板/历史试题模板.md 当成题目，全库统计虚增、示例 id 撞车
        self.assertFalse(self.s.is_question_file("0-Template-模板/历史试题模板.md"))
        self.assertFalse(self.s.is_question_file("3-试题研究/某报告.md"))
        self.assertFalse(self.s.is_question_file("1-MoC-索引/9上-试题索引.md"))

    def test_辅助与导出文件为假(self):
        self.assertFalse(self.s.is_question_file("初中-1中考真题/卷/卷_选择题/_重复题清单.md"))
        self.assertFalse(self.s.is_question_file("初中-1中考真题/卷/卷_选择题/README.md"))
        self.assertFalse(self.s.is_question_file("初中-1中考真题/卷/卷_选择题/01_学生版.md"))
        self.assertFalse(self.s.is_question_file("初中-1中考真题/卷/卷_选择题/01_命题说明.md"))

    def test_库内回收站归档件仍为真(self):
        # check_note_links.py 靠它扫归档件、反推旧 id → 必须保持 True
        self.assertTrue(self.s.is_question_file(
            ".trash/初中-2中考模拟题/2026南阳一模/2026南阳一模_选择题/04.md"))


class TestEndToEnd(TempVaultMixin, unittest.TestCase):
    """走真实 CLI：gen → scan → export → backtrace（临时题库，不碰真库）。"""

    def test_01_录入后全库校验通过(self):
        self.make_question(序号="01")
        self.make_question(序号="02", 答案="A")
        r = self.run_cli("scan_questions.py", expect=0)
        self.assertIn("✓ 无问题", r.stdout)
        self.assertIn("题目总数：2", r.stdout.replace(" ", ""))

    def test_02_scan_json_结构完整(self):
        self.make_question(序号="01")
        r = self.run_cli("scan_questions.py", "--json", expect=0)
        data = json.loads(r.stdout)
        for key in ("total", "stats", "problems", "locator_hits", "pair_hits", "merged"):
            self.assertIn(key, data)
        self.assertGreaterEqual(data["total"], 1)

    def test_03_多值配对_册别重复被报出(self):
        self.make_question(序号="01", 册别=["7上", "7上"], 单元=["第2单元 夏商周时期"])
        r = self.run_cli("scan_questions.py", "--json", expect=0)
        data = json.loads(r.stdout)
        self.assertTrue(any("册别有重复项" in "".join(h["册别异常"]) for h in data["pair_hits"]),
                        "册别重复项未被报出：%s" % data["pair_hits"])

    def test_04_多值配对_单元多于课被报出(self):
        self.make_question(序号="01",
                           单元=["第2单元 夏商周时期", "第3单元 秦汉时期", "第1单元 史前时期"],
                           课=["第4课 夏商西周王朝的更替"])
        r = self.run_cli("scan_questions.py", "--json", expect=0)
        data = json.loads(r.stdout)
        self.assertTrue(data["pair_hits"], "单元多于课未被报出")

    def test_05_合法的单元多于课_不误报(self):
        # 单元卷常把上级单元一并写入（单元数=课数 属合法）；此处专门验证不产生假阳性
        self.make_question(序号="01",
                           单元=["第2单元 夏商周时期"],
                           课=["第4课 夏商西周王朝的更替"])
        r = self.run_cli("scan_questions.py", "--json", expect=0)
        data = json.loads(r.stdout)
        self.assertEqual(data["pair_hits"], [], "合法录入被误报：%s" % data["pair_hits"])

    def test_06_录入_课名错字被词表拦下(self):
        """词表校验在**录入环节**就拦住错字（gen_problem 即告警），不靠事后 scan。"""
        rec = q(序号="01", 课=["第4课 夏商西周王朝的更替（错字版）"])
        j = self.vault / "_in.json"
        j.write_text(json.dumps([rec], ensure_ascii=False), encoding="utf-8")
        r = self.run_cli("gen_problem.py", str(j), expect=0)
        self.assertIn("不在 BOOK_INDEX 词", r.stdout)
        # scan 侧仍应把该题报成问题级，并以退出码 1 阻断流水线
        r2 = self.run_cli("scan_questions.py")
        self.assertIn("课名词表", r2.stdout, "scan 未报课名问题；stdout=%s" % r2.stdout[-500:])
        self.assertEqual(r2.returncode, 1, "scan 有问题时应以退出码 1 阻断；stdout=%s" % r2.stdout[-300:])

    def test_07_组卷清单出卷_再反查回id(self):
        self.make_question(序号="01")
        self.make_question(序号="02", 答案="B",
                           题干="甲骨文主要出土于河南安阳，它主要反映了(　　)")
        manifest = self.vault / "清单.yaml"
        manifest.write_text(
            "卷名: 测试卷\n标题: 测试卷\n题目:\n  - 2026测试卷-01\n  - 2026测试卷-02\n",
            encoding="utf-8")
        out = self.vault / "out_学生.md"
        self.run_cli("export_student_paper.py", "--清单", str(manifest),
                     "--模式", "学生", "--输出", str(out), expect=0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("司母戊鼎", text)
        self.assertNotIn("答案：C", text)          # 学生卷不得带答案
        bt = self.vault / "back.yaml"
        r = self.run_cli("backtrace_paper.py", "--卷", str(out), "--输出", str(bt), expect=0)
        self.assertTrue(bt.exists(), "反查未输出清单：%s" % r.stdout)
        got = bt.read_text(encoding="utf-8")
        # 两道题必须各自定回自己的 id（顺序即卷面顺序）
        self.assertLess(got.index("2026测试卷-01"), got.index("2026测试卷-02"),
                        "反查清单顺序或 id 不对：\n%s" % got)

    def test_08_题目文件判定_模板不进统计(self):
        self.make_question(序号="01")
        (self.vault / "0-Template-模板" / "历史试题模板.md").write_text(
            "---\nid: 2026测试卷-01\n册别:\n  - 7上\n---\n模板正文\n", encoding="utf-8")
        r = self.run_cli("scan_questions.py", "--json", expect=0)
        self.assertEqual(json.loads(r.stdout)["total"], 1, "模板文件被计入题目总数")


class TestDocxRender(TempVaultMixin, unittest.TestCase):
    """Word 卷渲染护栏（拆分 export_student_paper.py 前的安全网）。

    拆分前后这些断言必须始终成立：能出卷、题干不丢、学生卷无答案、版式校验通过。
    """

    def _manifest(self):
        self.make_question(序号="01")
        self.make_question(序号="02", 答案="B",
                           题干="甲骨文主要出土于河南安阳，它主要反映了(　　)")
        mf = self.vault / "清单.yaml"
        mf.write_text("卷名: 测试卷\n标题: 2026—2027学年 测试卷\n"
                      "题目:\n  - 2026测试卷-01\n  - 2026测试卷-02\n", encoding="utf-8")
        return mf

    def test_docx_学生卷_出卷且版式合规(self):
        mf = self._manifest()
        out = self.vault / "卷_学生版.docx"
        self.run_cli("export_student_paper.py", "--清单", str(mf),
                     "--模式", "学生", "--docx", str(out), expect=0)
        self.assertTrue(out.exists(), "未生成 Word 卷")
        self.assertGreater(out.stat().st_size, 5000, "Word 卷过小，疑渲染失败")
        r = self.run_cli("verify_paper_layout.py", "--docx", str(out), "--清单", str(mf))
        self.assertEqual(r.returncode, 0,
                         "版式校验不通过（拆分引入渲染回归）：\n%s" % r.stdout[-1500:])
        self.assertIn("合规", r.stdout)

    def test_docx_命题说明卷_出卷且版式合规(self):
        mf = self._manifest()
        out = self.vault / "卷_命题说明.docx"
        self.run_cli("export_student_paper.py", "--清单", str(mf),
                     "--模式", "命题说明", "--docx", str(out), expect=0)
        self.assertTrue(out.exists(), "未生成命题说明 Word")
        r = self.run_cli("verify_paper_layout.py", "--docx", str(out))
        self.assertEqual(r.returncode, 0, "命题说明版式校验不通过：\n%s" % r.stdout[-1500:])

    def test_docx_与md同源_题干不丢且学生卷无答案(self):
        """同一清单出 md 与 docx，题干关键词必须都在；学生卷不得出现答案。"""
        mf = self._manifest()
        md = self.vault / "卷_学生版.md"
        dx = self.vault / "卷_学生版.docx"
        self.run_cli("export_student_paper.py", "--清单", str(mf),
                     "--模式", "学生", "--输出", str(md), expect=0)
        self.run_cli("export_student_paper.py", "--清单", str(mf),
                     "--模式", "学生", "--docx", str(dx), expect=0)
        md_text = md.read_text(encoding="utf-8")
        docx_text = _docx_all_text(dx)
        for kw in ("司母戊鼎", "甲骨文", "集体协作的精神"):
            self.assertIn(kw, md_text, "md 卷缺关键词 %s" % kw)
            self.assertIn(kw, docx_text, "docx 卷缺关键词 %s（渲染丢题）" % kw)
        self.assertNotIn("答案：C", docx_text, "学生卷 Word 里出现了答案")

    def test_docx_答题卡_随卷产出(self):
        """默认随 --docx 同出**独立**答题卡文件（`{卷名}_答题卡.docx`）。"""
        mf = self._manifest()
        out = self.vault / "卷_学生版.docx"
        r = self.run_cli("export_student_paper.py", "--清单", str(mf),
                         "--模式", "学生", "--docx", str(out), expect=0)
        cards = list(self.vault.glob("*_答题卡.docx"))
        self.assertEqual(len(cards), 1, "应产出 1 个独立答题卡；stdout=%s" % r.stdout[-400:])
        text = _docx_all_text(cards[0])
        self.assertIn("答题卡", text)
        self.assertIn("姓名", text)                      # 答题卡必有姓名栏


class TestPaperkitPure(unittest.TestCase):
    """paperkit 纯函数单测——拆分的主要收益：原先藏在闭包里的计算现在能直接测。"""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        from paperkit import docx_kit
        from paperkit.sections import is_choice, sub_scores
        from paperkit.spec import effective_answer, short_lesson
        self.kit = docx_kit
        self.is_choice = is_choice
        self.sub_scores = sub_scores
        self.effective_answer = effective_answer
        self.short_lesson = short_lesson

    # ---------- docx_kit：图片装箱计算 ----------
    def _img(self, w, h):
        """在 tests/_tmp 造一张纯色 PNG，返回路径（Pillow 不可用则跳过测试）。"""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow 不可用")
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        p = TMP_ROOT / ("img_%dx%d_%s.png" % (w, h, uuid.uuid4().hex[:6]))
        Image.new("RGB", (w, h), "white").save(p)
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        return p

    def test_宽高比与读不到时兜底(self):
        wide = self._img(1200, 300)
        self.assertAlmostEqual(self.kit.aspect_ratio(wide), 4.0, places=2)
        self.assertEqual(self.kit.aspect_ratio("不存在的文件.png"), 1.5)

    def test_装箱宽度按高度上限收缩(self):
        wide = self._img(1200, 300)          # ar = 4
        # 框 155×30：按宽算需高 155/4=38.75 > 30 → 收缩到 30*4=120
        self.assertAlmostEqual(self.kit.fit_width(wide, 155.0, 30.0), 120.0, places=1)
        # 框 155×100：宽度不受限 → 155
        self.assertAlmostEqual(self.kit.fit_width(wide, 155.0, 100.0), 155.0, places=1)
        # 下限 10mm
        self.assertGreaterEqual(self.kit.fit_width(wide, 155.0, 1.0), 10.0)

    def test_单图宽度_宽幅图铺满普通图回落基准(self):
        wide = self._img(1200, 300)          # ar=4 ≥ 2.6
        normal = self._img(400, 300)         # ar≈1.33 < 2.6
        self.assertAlmostEqual(self.kit.auto_single_w(wide, 60.0),
                               self.kit.MAX_W, places=1)
        self.assertAlmostEqual(self.kit.auto_single_w(normal, 60.0),
                               self.kit.BASE_W, places=1)

    def test_单图宽度_命令行固定宽优先(self):
        wide = self._img(1200, 300)
        self.assertAlmostEqual(self.kit.auto_single_w(wide, 60.0, img_width_mm=100.0),
                               100.0, places=1)

    def test_靠右环绕宽度_不超过上限(self):
        wide = self._img(1200, 300)
        normal = self._img(400, 300)
        self.assertLessEqual(self.kit.auto_right_w(wide), self.kit.RIGHT_MAX_W + 0.01)
        self.assertLessEqual(self.kit.auto_right_w(normal), self.kit.RIGHT_W + 0.01)
        self.assertGreaterEqual(self.kit.auto_right_w(normal, max_h=1.0), 20.0)

    def test_relativeHeight递增(self):
        a, b = self.kit.next_rel_h(), self.kit.next_rel_h()
        self.assertEqual(b, a + 1)

    # ---------- sections / spec ----------
    def test_选择题判定与分值均分(self):
        self.assertTrue(self.is_choice({"fm": {"题型": "选择题"}}))
        self.assertFalse(self.is_choice({"fm": {"题型": "材料解析题"}}))
        self.assertEqual(self.sub_scores("（1）甲（2分）（2）乙（3分）", 5, 2), [2, 3])
        self.assertEqual(self.sub_scores("无分值设问", 5, 2), [3, 2])   # 均分且余数给前

    def test_答案换算与课名缩写(self):
        rec = {"fm": {"答案": "A"}, "选项序": [2, 1, 3, 4]}
        self.assertEqual(self.effective_answer(rec), "B")             # A 位换到第 2 位
        self.assertEqual(self.effective_answer({"fm": {"答案": "A"}}), "A")
        self.assertEqual(self.short_lesson("第4课 夏商西周王朝的更替"), "第4课")
        self.assertEqual(self.short_lesson("活动课 新时代社会生活的巨大变化"),
                         "活动课 新时代社会生活的巨大变化")



    """脚本可用性：CLI 自检 + 库模块可编译（防「导入期求值 / 语法错误」回归）。"""

    def test_每个CLI脚本能打印help(self):
        bad = []
        for name in CLI_SCRIPTS:
            r = subprocess.run([PY, str(SCRIPTS / name), "--help"], capture_output=True,
                               text=True, encoding="utf-8", cwd=str(SCRIPTS))
            if r.returncode != 0 or "usage" not in r.stdout:
                bad.append("%s (exit=%s)" % (name, r.returncode))
        self.assertEqual(bad, [], "以下脚本 --help 失败：%s" % bad)

    def test_脚本清单与实际文件一致(self):
        """CLI_SCRIPTS + LIB_MODULES 必须覆盖 scripts/ 下全部 .py（新增脚本要登记）。"""
        actual = sorted(p.name for p in SCRIPTS.glob("*.py"))
        listed = sorted(CLI_SCRIPTS + LIB_MODULES)
        self.assertEqual(actual, listed, "scripts/ 与测试清单不一致（新增/删除脚本请同步本文件）")

    def test_paperkit模块清单与实际一致(self):
        """paperkit/ 下的模块同样要登记（拆分产生的新模块不能悄悄溜过）。"""
        actual = sorted(p.name for p in (SCRIPTS / "paperkit").glob("*.py"))
        self.assertEqual(actual, sorted(PAPERKIT_MODULES),
                         "paperkit/ 与实际不符（新增/删除模块请同步本文件）")

    def test_paperkit每个模块能编译(self):
        files = [str(SCRIPTS / "paperkit" / n) for n in PAPERKIT_MODULES]
        r = subprocess.run([PY, "-m", "py_compile", *files],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_paperkit可独立导入(self):
        """包内模块不得因循环导入而炸（依赖方向是单向的）。"""
        code = ("import sys; sys.path.insert(0, r'%s');"
                "import importlib;"
                "[importlib.import_module('paperkit.' + m) for m in %r];"
                "print('ok')" % (str(SCRIPTS), [n[:-3] for n in PAPERKIT_MODULES]))
        r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                           encoding="utf-8", cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_每个脚本能编译(self):
        r = subprocess.run([PY, "-m", "py_compile", *[str(SCRIPTS / n) for n in CLI_SCRIPTS + LIB_MODULES]],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_未配置vault时_help仍可用(self):
        """无配置时 --help 必须仍可用（历史回归点：qb_schema 在导入期求值 vault）。"""
        env = sub_env()
        env["TNOTE_VAULT"] = ""          # 置空＝未配置（空串为假值）
        for name in ("scan_questions.py", "scan_lesson.py", "scan_volume.py",
                     "linkify_citations.py", "check_note_links.py"):
            r = subprocess.run([PY, str(SCRIPTS / name), "--help"], capture_output=True,
                               text=True, encoding="utf-8", cwd=str(SCRIPTS), env=env)
            self.assertEqual(r.returncode, 0,
                             "%s 在未配置 vault 时 --help 失败：%s" % (name, r.stderr[-300:]))


class TestIndexGeneration(unittest.TestCase):
    """gen_index.py：13 册索引（初中 6 + 高中 7）都能从词表生成。

    ⚠ 词表只有一份（skill 内 `scripts/BOOK_INDEX.md`），gen_index 没有 --book-index 参数；
    本测试只把 **vault** 指向临时目录，词表仍读真实词表——因此它也顺带校验了
    「真实词表的每个册别节都能被 load_books 解析出来」。
    """

    @classmethod
    def setUpClass(cls):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.vault = TMP_ROOT / ("idx_" + uuid.uuid4().hex[:10])
        os.makedirs(cls.vault / "1-MoC-索引", exist_ok=True)
        cls.r = subprocess.run([PY, str(SCRIPTS / "gen_index.py")],
                               capture_output=True, text=True, encoding="utf-8",
                               cwd=str(SCRIPTS), env=sub_env(cls.vault))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.vault, ignore_errors=True)

    def test_命令成功(self):
        self.assertEqual(self.r.returncode, 0,
                         "gen_index 失败：%s\n%s" % (self.r.stdout[-500:], self.r.stderr[-500:]))
        self.assertNotIn("[跳过]", self.r.stdout, "有册别节未被解析：%s" % self.r.stdout)

    def test_生成13个册别索引(self):
        import qb_schema
        want = list(qb_schema.BOOK_INDEX_VERSION) + list(qb_schema.GAOZHONG_SECTIONS.values())
        self.assertEqual(len(want), 13)
        got = {p.stem.replace("-试题索引", "")
               for p in (self.vault / "1-MoC-索引").glob("*-试题索引.md")}
        self.assertEqual(set(want), got, "缺失或多余：%s" % (set(want) ^ got))

    def test_高中索引查高中目录(self):
        text = (self.vault / "1-MoC-索引" / "纲要上-试题索引.md").read_text(encoding="utf-8")
        self.assertIn('FROM "高中-1高考真题" OR "高中-2高考模拟题" OR "高中-3阶段测试"', text)
        self.assertNotIn("初中-", text)
        self.assertIn('contains(册别, "纲要上")', text)
        self.assertIn("### 第1课", text)              # 课块来自词表
        self.assertNotIn("①", text)                  # 子目行不应生成查询块

    def test_初中索引查初中目录(self):
        text = (self.vault / "1-MoC-索引" / "7上-试题索引.md").read_text(encoding="utf-8")
        self.assertIn('FROM "初中-1中考真题" OR "初中-2中考模拟题" OR "初中-3阶段测试"', text)
        self.assertNotIn("高中-", text)

    def test_选修索引用专题课名(self):
        text = (self.vault / "1-MoC-索引" / "选修1-试题索引.md").read_text(encoding="utf-8")
        self.assertIn("### 专题1.1", text)
        self.assertIn('contains(课, "专题1.1', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
