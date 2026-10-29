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
    "edit_fields.py", "export_student_paper.py",
    "find_better_images.py", "fix_arabic_numerals.py",
    "fix_image_formats.py", "fix_item_spacing.py", "fix_quote_to_plain.py", "fix_table_render.py",
    "gen_index.py", "gen_problem.py", "import_from_md.py",
    "linkify_citations.py", "normalize_id.py", "pull_pdf_images.py",
    "read_docx.py", "record_usage.py", "scan_lesson.py", "scan_missed.py", "scan_questions.py",
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

    def test_build_body_图片选项题出表格(self):
        """选项含图片引用时改出 md 表格（第一行图片、第二行 A. 文字）。"""
        rec = q(选项=[
            "![](images/01_1.jpg) 司母戊鼎",
            "![](images/01_2.jpg) 铜冰鉴",
            "![](images/01_3.jpg) 三彩釉陶载乐骆驼",
            "![](images/01_4.jpg) 定窑孩儿枕",
        ])
        body = self.s.build_body(rec)
        lines = body.splitlines()
        self.assertIn("| ![](images/01_1.jpg) |", lines[2])
        self.assertEqual(lines[3], "|:---:|:---:|:---:|:---:|")
        self.assertIn("| A. 司母戊鼎 |", lines[4])
        self.assertIn("D. 定窑孩儿枕", lines[4])
        self.assertNotIn("(A)", body)  # 不再走行内拼接

    def test_build_body_纯图选项题出表格(self):
        """选项只有图、无文字时，表格第二行只写字母。"""
        rec = q(选项=[f"![](images/02_{i}.jpg)" for i in range(1, 5)])
        body = self.s.build_body(rec)
        self.assertIn("| A. | B. | C. | D. |", body)

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
        # 单元层级（2026-10-06 起）：方法名白名单＝「初中·学史方法（全六册）」总表。
        # 2026-10-06 校正：原误列 3 项（如何区分史实与观点/如何编写历史剧本/如何开展历史调查）
        # 经核实该 3 册各只 2 个方法，已删；全六册共 15 个（7上3/7下2/8上3/8下3/9上2/9下2）。
        names = self.s.load_method_names()
        self.assertEqual(len(names), 15)
        self.assertIn("如何从出土文物中获取信息", names)
        self.assertNotIn("如何区分史实与观点", names)
        self.assertNotIn("如何编写历史剧本", names)
        self.assertNotIn("如何开展历史调查", names)
        self.assertTrue(all("[?]" not in n for n in names), "方法名不应残留 [?] 标记")

    def test_load_method_units_挂载到各册(self):
        # 各册版本节内「- 学史方法」单元下的课行 → (册别, 方法条目)；共 15 条。
        mu = self.s.load_method_units()
        self.assertEqual(len(mu), 15)
        self.assertIn(("7上", "如何阅读历史地图"), mu)
        self.assertIn(("9下", "如何写作历史小论文"), mu)
        # 每册条数：7上3/7下2/8上3/8下3/9上2/9下2
        expect = {"7上": 3, "7下": 2, "8上": 3, "8下": 3, "9上": 2, "9下": 2}
        for b, n in expect.items():
            self.assertEqual(sum(1 for x in mu if x[0] == b), n, "%s 学史方法条目数应为 %d" % (b, n))

    def test_load_lesson_units_学史方法归属单元(self):
        # 学史方法为单元层级：unit 解析为「学史方法」
        lu = self.s.load_lesson_units()
        self.assertEqual(lu.get(("7上", "如何阅读历史地图")), "学史方法")
        self.assertEqual(lu.get(("9下", "如何写作历史小论文")), "学史方法")

    def test_validate_lesson_学史方法新写法与旧前缀兼容(self):
        # 新写法：课名＝方法条目本身；旧前缀写法兼容（存量迁移期）
        self.s.validate_lesson({"册别": ["9下"], "课": ["如何写作历史小论文"]})
        self.s.validate_lesson({"册别": ["9下"], "课": ["学史方法-如何写作历史小论文"]})
        with self.assertRaises(self.s.SchemaError):
            self.s.validate_lesson({"册别": ["9下"], "课": ["如何写作历史小论文儿"]})


class TestAreaSimilarPure(unittest.TestCase):
    """考区取值相似检测（scan_questions.area_similar_hits）——纯函数，不依赖 vault。

    要点：区分「合法近邻」（河南/海南、湖南/湖北… 双方都是已知合法考区）与
    「同义异写」（洛阳市、郑洲、洛阳涧西… 出现集合外变体）。
    """

    def setUp(self):
        import scan_questions
        from collections import Counter
        self.f = scan_questions.area_similar_hits
        self.Counter = Counter

    def _pairs(self, d):
        return {tuple(sorted((h[0], h[2]))) for h in self.f(self.Counter(d))}

    def test_包含关系被报出(self):
        self.assertIn(("洛阳", "洛阳市"), self._pairs({"洛阳": 640, "洛阳市": 3}))
        self.assertIn(("洛阳", "洛阳涧西"), self._pairs({"洛阳": 640, "洛阳涧西": 5}))
        self.assertIn(("开封", "开封市"), self._pairs({"开封": 54, "开封市": 2}))

    def test_形近错写被报出(self):
        self.assertIn(("郑州", "郑洲"), self._pairs({"郑州": 393, "郑洲": 1}))
        # 整卷写错时错值频次同样很高——按「低频」过滤的旧思路会漏，故改用合法值集合判定
        self.assertIn(("郑州", "郑洲"), self._pairs({"郑州": 393, "郑洲": 25}))
        self.assertIn(("洛阳", "洛阴"), self._pairs({"洛阳": 640, "洛阴": 4}))

    def test_合法近邻不报(self):
        for a, b in [("河南", "海南"), ("河南", "河北"), ("湖南", "湖北"),
                     ("广东", "广西"), ("江苏", "江西"), ("安阳", "南阳"), ("濮阳", "洛阳")]:
            self.assertEqual(self._pairs({a: 100, b: 50}), set(), "%s/%s 不应报" % (a, b))

    def test_无地域属性规范值都在合法集合内(self):
        # 缺一个就会把该规范值当「集合外变体」，与近邻值凑成假阳性告警
        import scan_questions
        for v in ("全国", "原创", "案例"):
            self.assertIn(v, scan_questions.KNOWN_AREAS, "%s 应在 KNOWN_AREAS 内" % v)

    def test_快照_真实库取值零命中(self):
        # 2026-10-03 全库 29 个考区取值的快照；数据新增后若命中，先人工确认是否真为异写再更新快照
        real = {"河南": 875, "洛阳": 640, "郑州": 393, "海南": 121, "南阳": 74, "广东": 73,
                "山东": 63, "江西": 60, "开封": 54, "河北": 40, "四川": 38, "全国": 30,
                "安阳": 25, "新乡": 25, "濮阳": 25, "焦作": 25, "鹤壁": 25, "平顶山": 24,
                "驻马店": 23, "云南": 20, "广西": 20, "贵州": 20, "陕晋宁青": 20, "江苏": 19,
                "湖南": 19, "天津": 18, "重庆": 18, "湖北": 16, "原创": 7}
        self.assertEqual(self.f(self.Counter(real)), [])


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

    def test_09_frontmatter_YAML解析失败_单列报出且不刷缺字段(self):
        """块标量缩进丢失 → 整块 frontmatter 失效：应单列成节报出（带「第 N 行」定位）并以
        退出码 1 阻断；且**不得**再逐项刷「缺字段 X」——此时 fm 恒为 {}，那是同源噪音，
        会把「缩进丢了」这个真根因淹掉。

        判据来源（2026-10-07 实录）：安阳文峰区龙安区2024-2025学年7下期末 选择题/08.md
        的 `解析: |` 内容顶格未缩进，一度刷出 11 条「缺字段」掩盖 1 条根因。
        """
        self.make_question(序号="01")              # 一道正常题，确保题库非空
        d = self.vault / "初中-3阶段测试" / "坏例卷" / "坏例卷_选择题"
        d.mkdir(parents=True, exist_ok=True)
        (d / "01.md").write_text(
            "---\nid: [坏例卷-01]\n册别: [7上]\n单元: [第2单元 夏商周时期]\n"
            "课: [第4课 夏商西周王朝的更替]\n题型: 选择题\n难度: 中\n分值: 1\n"
            "年份: 2026\n考区: 河南\n答案: C\n解析: |\n【考点】故意不缩进\n---\n\n题干\n",
            encoding="utf-8")

        r = self.run_cli("scan_questions.py", "--json")
        self.assertEqual(r.returncode, 1,
                         "YAML 解析失败应以退出码 1 阻断；stdout=%s" % r.stdout[-300:])
        data = json.loads(r.stdout)
        self.assertEqual(len(data["yaml_errors"]), 1, "未单列报出：%s" % data["yaml_errors"])
        self.assertIn("行", data["yaml_errors"][0]["错误"], "报错缺少行号定位")
        self.assertEqual([p for p in data["problems"] if "缺字段" in p], [],
                         "frontmatter 失效后仍刷「缺字段」噪音：%s" % data["problems"])

        # 人类可读输出也要有该节（只看 stdout 的人不能漏），且坏文件不得计入题目统计
        r2 = self.run_cli("scan_questions.py")
        self.assertIn("frontmatter YAML 解析失败", r2.stdout, "stdout 缺少该节：%s" % r2.stdout[-400:])
        self.assertRegex(r2.stdout, r"第 \d+ 行第 \d+ 列", "stdout 缺少「第 N 行第 M 列」定位")
        self.assertIn("题目总数：1", r2.stdout.replace(" ", ""), "坏文件不应计入题目总数")


class TestDedupe权威序(TempVaultMixin, unittest.TestCase):
    """一题多卷的合并方向必须服从权威序（真题 > 模拟 > 阶段测试，同级年份早者优先）。

    2026-10-22 实案：`2023全国新课标卷`（高考真题）被当作别名并入 `2026洛阳涧西区一模`
    （中考模拟）。当时 `--merge` 对权威序倒挂**只告警不阻断**，违规合并得以落地。
    现改为写盘前硬阻断（`--allow-inferior` 才放行），并提供 `--check-authority` 体检。
    """

    def setUp(self):
        super().setUp()
        self.sim = self.make_question(序号="18", 卷名="2026测试模拟卷",
                                      来源=["2026测试模拟卷", "模拟卷"], 年份=2026)
        self.gaokao = self.make_question(
            序号="34", 学段="高中", 类别="1高考真题", 卷名="2023测试高考卷",
            册别=["纲要下"], 单元=["第4单元 资本主义制度的确立"],
            课=["第9课 资产阶级革命与资本主义制度的确立"], 小节=["资本主义的扩展"],
            知识点=["明治维新"], 核心素养=["历史解释-史事的意义性阐释"],
            素养层次水平=["历史解释-水平2-运用史料描述、解释相关历史现象及规律，"
                     "说明史事间的联系，关照现实问题"],
            关键能力=["A3-概括"], 来源=["2023测试高考卷", "真题"], 年份=2023,
            考区="全国", 分值=4)

    def test_倒挂方向被拒绝(self):
        r = self.run_cli("dedupe_questions.py", "--merge", str(self.sim),
                         "--with", str(self.gaokao), expect=1)
        self.assertIn("权威序倒挂", r.stdout)
        self.assertIn("拒绝执行", (r.stderr or "") + r.stdout)
        self.assertTrue(self.gaokao.exists(), "被拒后别名文件不得被移动")

    def test_正方向合并成功(self):
        self.run_cli("dedupe_questions.py", "--merge", str(self.gaokao),
                     "--with", str(self.sim), "--apply", expect=0)
        head = self.gaokao.read_text(encoding="utf-8").split("---")[1]
        self.assertIn("2026测试模拟卷-18", head, "别名 id 未追加到真题主卷")
        self.assertFalse(self.sim.exists(), "别名应已离开卷内")
        self.assertTrue((self.vault / ".trash" / self.sim.relative_to(self.vault)).exists(),
                        "别名未归档到 .trash")
        # 卷内清单（2026-10-30）：须记「本卷分值」——还原时按本卷 id 取到的是主卷分值，
        # 两者可能不同（本例主题 4 分 / 别名 1 分），故必须单列留存
        cks = list(self.vault.rglob("_重复题清单.md"))
        self.assertEqual(len(cks), 1, "别名卷未生成 _重复题清单.md")
        txt = cks[0].read_text(encoding="utf-8")
        self.assertIn("本卷分值", txt, "清单表头缺「本卷分值」列：\n%s" % txt)
        self.assertRegex(
            txt, r"\| 18 \| 2026测试模拟卷-18 \| 2023测试高考卷-34 \| \d{4}-\d{2}-\d{2} \| 1 \|",
            "清单行未按**本卷**分值记录（应 1 分，非主题的 4 分）：\n%s" % txt)
        self.assertIn("按「本卷 id」取题", txt, "清单头部未写明还原取题方式")

    def test_旧四列清单表头自动升级(self):
        """2026-10-30：清单新增「本卷分值」列。旧四列清单（含 Obsidian 自动重排的列宽形态）
        在下次合并时应就地升级表头，**旧行保留**（少一格渲染为空，前四列不错位）。"""
        vol = self.sim.parent.parent
        (vol / "_重复题清单.md").write_text(
            "# 重复题清单 · 2026测试模拟卷\n\n"
            "| 本卷题号 | 本卷 id                     | 主题 id                     | 合并日期       |\n"
            "| ---- | ------------------------- | ------------------------- | ---------- |\n"
            "| 09 | 2026测试模拟卷-09 | 2023测试高考卷-33 | 2026-10-01 |\n",
            encoding="utf-8")
        self.run_cli("dedupe_questions.py", "--merge", str(self.gaokao),
                     "--with", str(self.sim), "--apply", expect=0)
        txt = (vol / "_重复题清单.md").read_text(encoding="utf-8")
        self.assertIn("| 本卷题号 | 本卷 id | 主题 id | 合并日期 | 本卷分值 |", txt,
                      "旧表头未升级：\n%s" % txt)
        self.assertIn("2026测试模拟卷-09", txt, "升级表头时丢了旧记录")
        self.assertRegex(txt, r"2026测试模拟卷-18[^\n]*\| 1 \|", "新行未记本卷分值")

    def test_体检报出既成倒挂(self):
        d = self.vault / "初中-2中考模拟题" / "2026测试模拟卷" / "2026测试模拟卷_选择题"
        d.mkdir(parents=True, exist_ok=True)
        (d / "18.md").write_text("---\nid: [2026测试模拟卷-18, 2023测试高考卷-34]\n"
                                 "题型: 选择题\n年份: 2026\n---\n\n题干\n", encoding="utf-8")
        a = (self.vault / ".trash" / "高中-1高考真题" / "2023测试高考卷"
             / "2023测试高考卷_选择题")
        a.mkdir(parents=True, exist_ok=True)
        (a / "34.md").write_text("---\nid: [2023测试高考卷-34]\n题型: 选择题\n年份: 2023\n"
                                 "---\n\n题干\n", encoding="utf-8")
        r = self.run_cli("dedupe_questions.py", "--check-authority", expect=0)
        self.assertIn("权威序倒挂 1 处", r.stdout, "体检未报出：\n%s" % r.stdout[-800:])


class Test卷结构_题号口径(TempVaultMixin, unittest.TestCase):
    """序号＝原卷面题号（v1.29.0）：合卷跳号不判不合规；跨题型重号仍判不合规。"""

    def test_合卷跳号_判需人工而非不合规(self):
        self.make_question(序号="24")
        self.make_question(序号="41", 题型="材料解析题")
        r = self.run_cli("verify_paper_layout.py", "--卷", "2026测试卷", expect=0)
        self.assertIn("跳号", r.stdout, "合卷跳号未报出：\n%s" % r.stdout[-800:])
        self.assertIn("需人工", r.stdout)

    def test_跨题型重号_判不合规(self):
        self.make_question(序号="24")
        self.make_question(序号="24", 题型="材料解析题")
        r = self.run_cli("verify_paper_layout.py", "--卷", "2026测试卷", expect=1)
        self.assertIn("重号", r.stdout, "重号未报出：\n%s" % r.stdout[-800:])


class Test按卷导出_图片打包(TempVaultMixin, unittest.TestCase):
    """回归：--卷 出 md 时的图片打包（2026-10-20 实测 NameError：_copy_img_refs 未 import）。"""

    def test_带图题导出_图片打包不崩且落盘(self):
        p = self.make_question(序号="01",
                               题干="观察下图，回答问题(　　)\n\n![](images/01_1.png)")
        img_dir = p.parent / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "01_1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
        out = self.vault / "卷_学生版.md"
        r = self.run_cli("export_student_paper.py", "--卷", "2026测试卷",
                         "--输出", str(out), expect=0)
        self.assertIn("[图片]", r.stdout, "未报告图片打包：\n%s" % r.stdout[-800:])
        self.assertTrue((out.parent / "images" / "01_1.png").exists(), "图片未复制到输出目录")


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
        # 活动课（方案A）不缩为「第N课」，否则与同号常规课撞名
        self.assertEqual(self.short_lesson("第22课 活动课 唱响《国际歌》"),
                         "第22课 活动课 唱响《国际歌》")
        # 兼容历史无编号写法
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

    def test_册索引含学史方法单元节(self):
        """学史方法改为教材单元层级（2026-10-06 起）：册索引须生成 `## 学史方法` 节 +
        每方法条目 `### 方法名` + Dataview 块（带单元限定，避免课名撞车）。"""
        for name in ("7上-试题索引.md", "9下-试题索引.md"):
            text = (self.vault / "1-MoC-索引" / name).read_text(encoding="utf-8")
            self.assertIn("## 学史方法", text, "%s 缺学史方法单元节" % name)
            self.assertIn('contains(单元, "学史方法")', text)

    def test_册索引含活动课节(self):
        """活动课方案A（2026-10-06）：各册活动课写作 `第N课 活动课 名称`（顶格），
        索引须为该行生成独立 `## 第N课 活动课 名称` 节，且 Dataview 查询课名与该行逐字一致。"""
        for name, lesson in (("7上-试题索引.md", "第21课 活动课 从考古发现看中华文明的起源"),
                             ("9上-试题索引.md", "第22课 活动课 唱响《国际歌》"),
                             ("9下-试题索引.md", "第22课 活动课 时事溯源——英国脱欧")):
            text = (self.vault / "1-MoC-索引" / name).read_text(encoding="utf-8")
            self.assertIn(f"## {lesson}", text, "%s 缺活动课节 %s" % (name, lesson))
            self.assertIn(f'contains(课, "{lesson}")', text,
                          "%s 活动课 Dataview 查询值不符" % name)
        t7 = (self.vault / "1-MoC-索引" / "7上-试题索引.md").read_text(encoding="utf-8")
        self.assertIn("### 如何阅读历史地图", t7)
        self.assertIn('contains(课, "如何阅读历史地图")', t7)
        self.assertIn("### 如何从出土文物中获取信息", t7)
        self.assertIn("### 如何掌握历史纪年法", t7)

    def test_选修索引用专题课名(self):
        text = (self.vault / "1-MoC-索引" / "选修1-试题索引.md").read_text(encoding="utf-8")
        self.assertIn("### 专题1.1", text)
        self.assertIn('contains(课, "专题1.1', text)

    def test_不生成附录节(self):
        """「附录 朝代表/大事年表」这类非课内容不建索引节（用户裁定：不需要）。

        词表里已删除该行；gen_index 另有守卫，防止以后重新加回来又冒出一堆空节。
        """
        for name in ("纲要上-试题索引.md", "纲要下-试题索引.md"):
            text = (self.vault / "1-MoC-索引" / name).read_text(encoding="utf-8")
            self.assertNotIn("## 附录", text, "%s 里出现了附录节" % name)


class TestReadmeTree(unittest.TestCase):
    """README 的「目录结构」树必须与真实文件树一致（防文档漂移）。

    历史问题：该树曾把 `tests/` 画成 `└──`（看着像 scripts/ 的子目录，实际两者平级）、
    `paperkit/` 是 12 个模块却只列了一行、`_snapshot.py`/`_baseline.json`/`.gitignore`
    从未登记、`test_qb.py` 的用例数停在 35（实际 51）。
    """

    README = SKILL / "README.md"

    # 本机文件：README 树里列出（帮用户知道它在哪），但**已 gitignore**，
    # 因此新克隆的仓库里合法地不存在——校验「树→磁盘」时豁免。
    LOCAL_ONLY = {"skill_config.json"}

    def _tree_entries(self):
        text = self.README.read_text(encoding="utf-8")
        m = re.search(r"## 目录结构\n\n```\n(.*?)```", text, re.S)
        self.assertIsNotNone(m, "README 里找不到「目录结构」代码块")
        entries = []
        for line in m.group(1).splitlines()[1:]:        # 跳过根目录名那一行
            body = re.sub(r"^[│├└─\s]+", "", line).split("#")[0].strip()
            if body:
                entries.append(body)
        return entries

    def _actual_entries(self):
        # `.local-backup` 是仓库侧的本地备份目录（已入 .gitignore），不随分发、也不登记进 README 树
        skip = {"__pycache__", "_tmp", "_snap", "_dbg", "_dbg2", "_audit_tmp",
                "_acl-recovery", ".git", ".local-backup"}
        return {str(p.relative_to(SKILL)).replace("\\", "/")
                for p in SKILL.rglob("*") if not any(s in p.parts for s in skip)}

    def test_树里列出的条目都存在(self):
        actual = self._actual_entries()
        leaves = {a.split("/")[-1] for a in actual}
        dirs = {a.split("/")[-1] for a in actual}
        missing = []
        for e in self._tree_entries():
            name = e.rstrip("/")
            if name in self.LOCAL_ONLY:
                continue                     # 本机文件，未初始化时不存在属正常
            pool = dirs if e.endswith("/") else leaves
            if name not in pool:
                missing.append(e)
        self.assertEqual(missing, [], "README 目录树列了磁盘上没有的条目：%s" % missing)

    def test_真实条目都在树里(self):
        entries = {e.rstrip("/") for e in self._tree_entries()}
        untold = sorted(a for a in self._actual_entries()
                        if a.split("/")[-1] not in entries)
        self.assertEqual(untold, [],
                         "磁盘上有、但 README 目录树未登记的条目：%s" % untold)

    def test_顶层目录连接符正确(self):
        """非末位的顶层目录要用 ├──，末位用 └──（曾把 tests/ 画成 └── 而它后面还有内容）。"""
        text = self.README.read_text(encoding="utf-8")
        m = re.search(r"## 目录结构\n\n```\n(.*?)```", text, re.S)
        lines = m.group(1).splitlines()
        top = [l for l in lines[1:] if l and not l[0].isspace() and l[0] in "├└"]
        self.assertTrue(top, "没解析出顶层条目")
        self.assertTrue(top[0].startswith("├──"), "首个顶层条目应为 ├──：%s" % top[0][:20])
        for l in top[:-1]:
            self.assertTrue(l.startswith("├──"), "非末位顶层条目应为 ├──：%s" % l[:24])
        self.assertTrue(top[-1].startswith("└──"), "末位顶层条目应为 └──：%s" % top[-1][:24])


class Test录入与整治辅助(TempVaultMixin, unittest.TestCase):
    """import_from_md.py（源 md → 录入 JSON）与 edit_fields.py（字段批量整治）。

    ⚠ 两个模块顶层 `from qb_schema import …`，而 qb_schema 在导入期解析 vault，
    故一律走**子进程 + TNOTE_VAULT 指向临时库**，不在测试进程内直接 import。
    """

    def _write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_import_from_md_回填题干与选项(self):
        md = self._write("_src.md", (
            "××中学 2026 届历史\n\n"
            "注意事项：\n\n1. 答题前请填好姓名、考号。\n\n2. 选择题用 2B 铅笔涂卡。\n\n"
            "3. 考试结束后交回试卷。\n\n"
            "一、选择题\n\n"
            "1．秦朝统一后在全国推行郡县制。这表明（　　）\n\n"
            "A．分封制瓦解　B．中央集权加强\n\n"
            "2．下图为某文物。\n\n![](images/001.jpg)\n\nA．甲　B．乙　C．丙　D．丁\n\n"
            "参考答案\n\n1．B 2．A\n"))
        js = self._write("_in.json", json.dumps(
            [{"序号": "01", "题型": "选择题"}, {"序号": "02", "题型": "选择题"}], ensure_ascii=False))
        r = self.run_cli("import_from_md.py", "--源", str(md), "--json", str(js), expect=0)
        out = json.loads(js.read_text(encoding="utf-8"))
        # 注意事项段的「1. 2. 3.」编号行不得覆盖真题块
        self.assertEqual(out[0]["题干"], "秦朝统一后在全国推行郡县制。这表明（　　）")
        self.assertEqual(out[0]["选项"], ["分封制瓦解", "中央集权加强"])
        # 图片按题号规范且保留原扩展名
        self.assertIn("![](images/2_1.jpg)", out[1]["题干"])
        self.assertEqual(out[1]["选项"], ["甲", "乙", "丙", "丁"])
        # 答案区不入选
        self.assertNotIn("参考答案", json.dumps(out, ensure_ascii=False))
        self.assertIn("回填 2 题", r.stdout)

    def test_import_from_md_无序号节标题仍剔除注意事项(self):
        """节标题写作「选择题(共20小题,20分)」（无「一、」序号）时，注意事项仍须剔除。

        回归：2026-10-26 四卷实测——旧实现只认「一、/二、」式节标题，判据失配即
        `return md` 整体放弃剔除，卷首「1. 2. 3.」编号行顶掉真题干（共 9 题），
        且原题选项串进说明块；选项数自检只在「一项都没切出」时告警，故静默通过。
        """
        md = self._write("_src3.md", (
            "××中学 2026 届历史\n\n"
            "注意事项：\n\n1. 答题前请填好姓名、考号。\n\n2. 选择题用 2B 铅笔涂卡。\n\n"
            "3. 考试结束后交回试卷。\n\n"
            "选择题(共20小题,20分)\n\n"
            "1．秦朝统一后在全国推行郡县制。这表明（　　）\n\n"
            "A．分封制瓦解　B．中央集权加强　C．郡国并行　D．王位世袭\n\n"
            "2．下图为某文物。\n\n![](images/001.jpg)\n\nA．甲　B．乙　C．丙　D．丁\n\n"
            "参考答案\n\n1．B 2．A\n"))
        js = self._write("_in3.json", json.dumps(
            [{"序号": "01", "题型": "选择题"}, {"序号": "02", "题型": "选择题"}], ensure_ascii=False))
        r = self.run_cli("import_from_md.py", "--源", str(md), "--json", str(js), expect=0)
        out = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(out[0]["题干"], "秦朝统一后在全国推行郡县制。这表明（　　）")
        self.assertEqual(out[0]["选项"], ["分封制瓦解", "中央集权加强", "郡国并行", "王位世袭"])
        self.assertEqual(len(out[1]["选项"]), 4)
        self.assertNotIn("注意事项", json.dumps(out, ensure_ascii=False))
        # 剔除干净时不得出现「题干含卷首说明字样」告警
        self.assertNotIn("题干含卷首说明字样", r.stdout)

    def test_import_from_md_无节标题降级剔除(self):
        """整卷连节标题都没有 → 降级按说明行特征剔除，并打印告警要求人工核对。"""
        md = self._write("_src4.md", (
            "××中学 2026 届历史\n\n"
            "注意事项：\n\n1. 答题前请填好姓名、考号。\n\n2. 考试结束后交回试卷。\n\n"
            "1．秦朝统一后在全国推行郡县制。这表明（　　）\n\n"
            "A．分封制瓦解　B．中央集权加强　C．郡国并行　D．王位世袭\n"))
        js = self._write("_in4.json", json.dumps([{"序号": "01", "题型": "选择题"}], ensure_ascii=False))
        r = self.run_cli("import_from_md.py", "--源", str(md), "--json", str(js), expect=0)
        out = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(out[0]["题干"], "秦朝统一后在全国推行郡县制。这表明（　　）")
        self.assertIn("降级剔除", r.stdout)
        self.assertIn("必核第 1~3 题", r.stdout)

    def test_import_from_md_无标记答案区兜底(self):
        """答案紧跟试题、无独立标题（省级《学科评价报告》形态）时，末题不得吞入答案。

        回归：2025 安徽卷（答案写作「1. C【解析】…」「(1)【答案】…」）、2025 浙江6月卷
        （答案区题号写作「19（15分）」）——两处均不含 ANSWER_MARKERS、且不匹配建块正则，
        整段答案被并入试卷区最后一个块。
        """
        md = self._write("_src2.md", (
            "一、选择题\n\n"
            "1．秦朝统一后在全国推行郡县制。这表明（　　）\n\n"
            "A．分封制瓦解　B．中央集权加强\n\n"
            "二、非选择题\n\n"
            "17．阅读材料，回答问题。（12分）\n\n"
            "材料一  某史料。\n\n"
            "（1）概括其内容。（6分）\n\n"
            "（2）说明其影响。（6分）\n\n"
            "18．阅读材料，完成下列要求。（14分）\n\n"
            "材料  另一史料。\n\n"
            "（1）分析其原因。（6分）\n\n"
            "（2）谈谈你的认识。（8分）\n\n"
            "17（12分）\n\n"
            "（1）【答案】内容为甲、乙、丙。\n\n"
            "【解析】据材料一可知……\n\n"
            "18（14分）\n\n"
            "（1）【答案】原因是……\n\n"
            "【解析】据材料可知……\n"))
        js = self._write("_in2.json", json.dumps(
            [{"序号": "01", "题型": "选择题"},
             {"序号": "17", "题型": "材料解析题"},
             {"序号": "18", "题型": "材料解析题"}], ensure_ascii=False))
        self.run_cli("import_from_md.py", "--源", str(md), "--json", str(js), expect=0)
        out = json.loads(js.read_text(encoding="utf-8"))
        d = {r["序号"]: r["题干"] for r in out}
        self.assertNotIn("【答案】", d["17"])
        self.assertNotIn("【解析】", d["17"])
        self.assertTrue(d["17"].rstrip().endswith("（6分）"))
        # 末题不得吞入答案区
        self.assertNotIn("【答案】", d["18"])
        self.assertNotIn("【解析】", d["18"])
        self.assertTrue(d["18"].rstrip().endswith("（8分）"))

    def test_import_from_md_答案区标题回退行首(self):
        """答案区标题把标记夹在中间时，正文尾部不得残留半行标题。"""
        md = self._write("_src3.md", (
            "1．题干一（　　）\n\nA．甲　B．乙\n\n"
            "浙江省2025年1月历史首考参考答案\n\n"
            "1．A\n"))
        js = self._write("_in3.json", json.dumps(
            [{"序号": "01", "题型": "选择题"}], ensure_ascii=False))
        self.run_cli("import_from_md.py", "--源", str(md), "--json", str(js), expect=0)
        out = json.loads(js.read_text(encoding="utf-8"))
        self.assertTrue(out[0]["题干"].rstrip().endswith("（　　）"))
        self.assertNotIn("首考", out[0]["题干"])

    def test_import_from_md_结构判据兜底(self):
        """答案**完全不带特征词**时，靠结构判据兜底：「（1）原因：」式短标签 / 「示例一：」。"""
        md = self._write("_src4.md", (
            "18．阅读材料，回答问题。（14分）\n\n"
            "材料  另一史料。\n\n"
            "（1）简述其原因。（9分）\n\n"
            "（2）说明其局限。（6分）\n\n"
            "（1）原因：资本主义经济的发展……（四点8分）\n\n"
            "（2）局限：加剧社会不平等……\n\n"
            "19．阅读材料，完成下列要求。（12分）\n\n"
            "材料  某史料。\n\n"
            "就材料的整体或部分论述，任选角度，自拟论题，加以阐释。（要求：论题明确，观点正确）\n\n"
            "示例一：\n\n角度：以研究辛亥革命为例……\n\n"
            "示例二：\n\n中国早期现代化是一个由浅入深的过程……\n"))
        js = self._write("_in4.json", json.dumps(
            [{"序号": "18", "题型": "材料解析题"},
             {"序号": "19", "题型": "论述题"}], ensure_ascii=False))
        self.run_cli("import_from_md.py", "--源", str(md), "--json", str(js), expect=0)
        out = json.loads(js.read_text(encoding="utf-8"))
        d = {r["序号"]: r["题干"] for r in out}
        # 「（1）原因：」式答案短标签
        self.assertTrue(d["18"].rstrip().endswith("（6分）"))
        self.assertNotIn("原因：", d["18"])
        # 「示例一：」式参考答案示例（该题 head 中只有整题分值 → 不截到分号，整段保留）
        self.assertTrue(d["19"].rstrip().endswith("观点正确）"))
        self.assertNotIn("示例", d["19"])

    def test_edit_fields_去重与清单模式(self):
        f1 = self._write("初中-1中考真题/2026郑州一模/2026郑州一模_选择题/01.md", (
            "---\nid: 2026郑州一模-01\n册别: [8上, 8上]\n"
            "单元: [第1单元 中国开始沦为半殖民地半封建社会, 第2单元 早期现代化的初步探索和民族危机加剧]\n"
            "课: [第5课 甲午中日战争与列强瓜分中国狂潮]\n题型: 选择题\n---\n\n题干（　　）\n"))
        f2 = self._write("初中-1中考真题/2026郑州一模/2026郑州一模_选择题/02.md", (
            "---\nid: 2026郑州一模-02\n册别:\n- 9下\n- 9下\n"
            "单元:\n- 第5单元 第二次世界大战后的世界变化\n"
            "课:\n- 第16课 冷战的发生与两极格局的形成\n题型: 选择题\n---\n\n题干（　　）\n"))
        bk = TMP_ROOT / ("bk_" + uuid.uuid4().hex[:8])
        # ① --去重：流式与块状各一
        self.run_cli("edit_fields.py", "--去重", "册别", "--apply", "--backup", str(bk), expect=0)
        t1 = f1.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("册别: [8上]\n", t1)
        t2 = f2.read_text(encoding="utf-8").replace("\r\n", "\n")
        # 块状去重后，紧跟的字段名不得被吸附到末项（历史事故：漏换行 → `- 9下册别…`）
        self.assertIn("册别:\n- 9下\n单元:", t2)
        self.assertNotIn("- 9下册别", t2)
        # 正文零改动
        self.assertIn("题干（　　）\n", t1)
        # ② --op 清单：删冗余单元 + 补漏标课
        ops = [{"文件": "初中-1中考真题/2026郑州一模/2026郑州一模_选择题/01.md",
                "删": {"单元": ["第1单元 中国开始沦为半殖民地半封建社会"]},
                "加": {"课": ["第4课 洋务运动和边疆危机"]}}]
        opf = self._write("_ops.json", json.dumps(ops, ensure_ascii=False))
        self.run_cli("edit_fields.py", "--op", str(opf), "--apply", "--backup", str(bk), expect=0)
        t1 = f1.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("第1单元 中国开始沦为半殖民地半封建社会", t1)
        self.assertIn("第4课 洋务运动和边疆危机", t1)
        self.assertIn("第2单元 早期现代化的初步探索和民族危机加剧", t1)

    def test_edit_fields_新增字段模式(self):
        """缺字段时「加」应新建字段：位置按规范顺序、风格随前驱、块状前驱不吸附下一字段。"""
        f1 = self._write("初中-1中考真题/2026郑州一模/2026郑州一模_选择题/03.md", (
            "---\nid: [2026郑州一模-03]\n册别: [7上]\n单元: [第3单元 秦汉时期]\n"
            "课: [第15课 秦汉时期的科技与文化]\n知识点: [《九章算术》]\n题型: 选择题\n---\n\n题干（　　）\n"))
        f2 = self._write("初中-1中考真题/2026郑州一模/2026郑州一模_选择题/04.md", (
            "---\nid:\n- 2026郑州一模-04\n册别:\n- 9上\n单元:\n- 第5单元 走向近代\n"
            "课:\n- 第13课 西欧经济和社会的发展与文艺复兴运动\n知识点:\n- 文艺复兴\n题型: 选择题\n---\n\n题干（　　）\n"))
        bk = TMP_ROOT / ("bk_" + uuid.uuid4().hex[:8])
        ops = [
            {"文件": "初中-1中考真题/2026郑州一模/2026郑州一模_选择题/03.md",
             "加": {"小节": ["医学、数学与农学的成就"]}},
            {"文件": "初中-1中考真题/2026郑州一模/2026郑州一模_选择题/04.md",
             "加": {"小节": ["意大利文艺复兴运动"]}},
        ]
        opf = self._write("_ops_new.json", json.dumps(ops, ensure_ascii=False))
        self.run_cli("edit_fields.py", "--op", str(opf), "--apply", "--backup", str(bk), expect=0)
        # ① 流式前驱：插在「课」之后、「知识点」之前，风格随前驱
        t1 = f1.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("课: [第15课 秦汉时期的科技与文化]\n小节: [医学、数学与农学的成就]\n知识点:", t1)
        # ② 块状前驱：新字段块状，末项不得吸附下一字段名（历史事故：漏换行 → `- X知识点:`）
        t2 = f2.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("课:\n- 第13课 西欧经济和社会的发展与文艺复兴运动\n小节:\n- 意大利文艺复兴运动\n知识点:", t2)
        self.assertNotIn("- 意大利文艺复兴运动知识点", t2)
        # 正文零改动
        self.assertIn("题干（　　）\n", t1)

    def test_edit_fields_新增字段须只给加(self):
        """字段不存在且同时给「删」→ 不新建、仅报提示（避免误建字段）。"""
        f = self._write("初中-1中考真题/2026郑州一模/2026郑州一模_选择题/05.md", (
            "---\nid: [2026郑州一模-05]\n册别: [8上]\n课: [第1课 鸦片战争]\n题型: 选择题\n---\n\n题干\n"))
        bk = TMP_ROOT / ("bk_" + uuid.uuid4().hex[:8])
        ops = [{"文件": "初中-1中考真题/2026郑州一模/2026郑州一模_选择题/05.md",
                "删": {"小节": ["X"]}, "加": {"小节": ["Y"]}}]
        opf = self._write("_ops_new2.json", json.dumps(ops, ensure_ascii=False))
        self.run_cli("edit_fields.py", "--op", str(opf), "--apply", "--backup", str(bk), expect=0)
        self.assertNotIn("小节", f.read_text(encoding="utf-8"))

    def test_edit_fields_字段白名单外拒绝(self):
        r = self.run_cli("edit_fields.py", "--去重", "答案")
        self.assertNotEqual(r.returncode, 0, "白名单外字段应拒绝执行")
        self.assertIn("白名单", r.stderr + r.stdout)


class Test组卷使用登记(TempVaultMixin, unittest.TestCase):
    """record_usage.py：本校考试卷登记（题目 id/来源 追加卷名 + 卷目录放 _组卷清单.md）。

    设计要点（2026-10-24 定）：本校卷不另存题目，靠「一题多卷」语义留痕——
    id 追加 `{卷名}-{卷面题号}`、来源成对追加 `(卷名, 类型标签)`；
    故本类同时校验「不动首项」「来源不成对不猜」「幂等」「撤销可复原」。
    """

    VOL = "2026-2027-7上月考1"

    def _清单(self, *ids):
        p = self.vault / "_组卷清单.yaml"
        body = "卷名: 测试月考\n题目:\n" + "".join(f"  - {i}\n" for i in ids)
        p.write_text(body, encoding="utf-8")
        return p

    def _fm(self, path):
        """取题目文件的 id / 来源 两行（原样字符串）。"""
        text = path.read_text(encoding="utf-8")
        return {k: [l for l in text.splitlines() if l.startswith(k + ":")][0].split(": ")[1]
                for k in ("id", "来源")}

    def test_登记_追加id与来源并生成组卷清单(self):
        f1 = self.make_question(序号="01")
        f2 = self.make_question(序号="02", 题型="材料解析题", 分值=6)
        cl = self._清单("2026测试卷-01", "2026测试卷-02")
        r = self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL,
                         "--日期", "2026-10-11", "--范围", "7上第1-2单元", "--apply", expect=0)
        self.assertIn("id 追加 2 题", r.stdout)
        # 首项（主卷）不动，本卷 id 追加在末尾
        self.assertEqual(self._fm(f1)["id"], "[2026测试卷-01, 2026-2027-7上月考1-01]")
        # 来源成对追加（卷名, 月考卷）
        self.assertEqual(self._fm(f1)["来源"],
                         "[2026测试卷, 模拟卷, 2026-2027-7上月考1, 月考卷]")
        # 正文一字不动
        self.assertIn("司母戊鼎是迄今世界上出土的最重的青铜器", f1.read_text(encoding="utf-8"))
        # 卷目录：只有 _组卷清单.md，且带 25 题都不怕的题表与 wikilink
        ck = self.vault / "初中-3阶段测试" / self.VOL / "_组卷清单.md"
        self.assertTrue(ck.exists(), "未生成组卷清单")
        ck_text = ck.read_text(encoding="utf-8")
        self.assertIn("| 01 |", ck_text)
        self.assertIn("\\|2026测试卷-01]]", ck_text)       # 表格内管道符已转义
        self.assertIn(f"题量: 2", ck_text)

    def test_来源不成对时只追加id不猜类型(self):
        f = self.make_question(序号="01", 来源=["2026测试卷"])   # 单项来源＝不成对
        cl = self._清单("2026测试卷-01")
        r = self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL,
                         "--apply", expect=0)
        self.assertIn("跳过（原来源不成对）", r.stdout)
        self.assertIn("不猜类型", r.stdout)
        fm = self._fm(f)
        self.assertEqual(fm["id"], "[2026测试卷-01, 2026-2027-7上月考1-01]")
        self.assertEqual(fm["来源"], "[2026测试卷]")            # 来源原样保留

    def test_幂等_重复跑不重复追加(self):
        f = self.make_question(序号="01")
        cl = self._清单("2026测试卷-01")
        for _ in range(2):
            self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL,
                         "--apply", "--覆盖", expect=0)
        fm = self._fm(f)
        self.assertEqual(fm["id"].count("2026-2027-7上月考1-01"), 1)
        self.assertEqual(fm["来源"].count("2026-2027-7上月考1"), 1)

    def test_已用_按学年排出已用题(self):
        self.make_question(序号="01")
        cl = self._清单("2026测试卷-01")
        self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL, "--apply", expect=0)
        r = self.run_cli("record_usage.py", "--已用", "2026-2027", "--json", expect=0)
        out = json.loads(r.stdout)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["主卷"], "2026测试卷-01")
        self.assertEqual(out[0]["被用于"], [self.VOL])
        # 别的学年应为空
        r2 = self.run_cli("record_usage.py", "--已用", "2025-2026", expect=0)
        self.assertIn("尚无已用题目", r2.stdout)

    def test_撤销_还原id与来源并清理空卷目录(self):
        f = self.make_question(序号="01")
        cl = self._清单("2026测试卷-01")
        self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL, "--apply", expect=0)
        self.run_cli("record_usage.py", "--卷", self.VOL, "--撤销", "--apply", expect=0)
        fm = self._fm(f)
        self.assertEqual(fm["id"], "[2026测试卷-01]")
        self.assertEqual(fm["来源"], "[2026测试卷, 模拟卷]")
        self.assertFalse((self.vault / "初中-3阶段测试" / self.VOL).exists(),
                         "撤销后应移除空卷目录")
        self.run_cli("record_usage.py", "--体检", expect=0)

    def test_体检_档案漏记能检出(self):
        self.make_question(序号="01")
        self.make_question(序号="02")
        cl = self._清单("2026测试卷-01", "2026测试卷-02")
        self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL, "--apply", expect=0)
        ck = self.vault / "初中-3阶段测试" / self.VOL / "_组卷清单.md"
        # 人为从档案题目表里删掉一题，模拟「题目侧有、档案漏记」
        text = ck.read_text(encoding="utf-8")
        ck.write_text(text.replace("题目: [2026测试卷-01, 2026测试卷-02]",
                                   "题目: [2026测试卷-01]"), encoding="utf-8")
        r = self.run_cli("record_usage.py", "--体检", expect=1)
        self.assertIn("档案漏记", r.stdout)

    def test_卷名不合规被拒(self):
        r = self.run_cli("record_usage.py", "--卷", "月考1", "--查题目", expect=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不合格式", r.stderr + r.stdout)

    def test_卷名缺类型词被拒(self):
        cl = self._清单("2026测试卷-01")
        r = self.run_cli("record_usage.py", "--清单", str(cl), "--卷", "2026-2027-第1次",
                         expect=None)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("类型词", r.stderr + r.stdout)

    def test_清单带序号时题号按序号(self):
        """成品卷反查的清单常有缺口（改编题匹配不上），题号须按「序号」而非清单顺序。"""
        f1 = self.make_question(序号="01")
        f2 = self.make_question(序号="02")
        p = self.vault / "_带号.yaml"
        p.write_text("卷名: 测试\n题目:\n"
                     "- id: 2026测试卷-01\n  序号: 4\n"
                     "- id: 2026测试卷-02\n  序号: 7\n", encoding="utf-8")
        self.run_cli("record_usage.py", "--清单", str(p), "--卷", self.VOL, "--apply", expect=0)
        self.assertIn(f"{self.VOL}-04", self._fm(f1)["id"])
        self.assertIn(f"{self.VOL}-07", self._fm(f2)["id"])
        # 档案须标注缺号（题库外/已改编题）
        ck = self.vault / "初中-3阶段测试" / self.VOL / "_组卷清单.md"
        self.assertIn("题库外题目", ck.read_text(encoding="utf-8"))

    def test_备份保留相对路径结构(self):
        """备份只存文件名会让多卷同题号互相覆盖（实测 15.md 撞车）——须保留库内相对路径。"""
        import re as _re
        f1 = self.make_question(序号="01")
        f2 = self.make_question(序号="02")
        cl = self._清单("2026测试卷-01", "2026测试卷-02")
        r = self.run_cli("record_usage.py", "--清单", str(cl), "--卷", self.VOL,
                         "--apply", expect=0)
        bak = _re.search(r"备份 (\S.*)$", r.stdout, _re.M).group(1).strip()
        bdir = Path(bak)
        self.assertTrue(bdir.is_dir(), "备份目录不存在：%s" % bak)
        rels = [p.relative_to(bdir).as_posix() for p in bdir.rglob("*.md")]
        # 备份里必须能区分同卷不同题，且保留 初中-*/卷名/… 结构
        self.assertIn("初中-2中考模拟题/2026测试卷/2026测试卷_选择题/01.md", rels)
        self.assertIn("初中-2中考模拟题/2026测试卷/2026测试卷_选择题/02.md", rels)


class Test反查卷首剔除(unittest.TestCase):
    """backtrace_paper.drop_header_notes：卷首「1.本卷为…／2.每小题只有…」不得被当成第 1、2 题。

    回归：2026-10-24 实测 50 题的周测被切成 50 段，真第 1 题落到第 4 段——
    匹配全对但**题号整体错 3 位**，登记时会写错 alias id。
    """

    def test_编号注意事项被剔除而正文不动(self):
        sys.path.insert(0, str(SCRIPTS))
        import backtrace_paper as bt
        lines = ["1.本卷为选择题卷，共50 小题，每小题2 分，共100 分。",
                 "2.每小题只有一个选项符合题意，请将所选答案填入卷末答题卡内。",
                 "3.答题前请将姓名、班级填写在第6 页答题卡位置。",
                 "1．你跟随国际旅行团来到一个景点，导游介绍……",
                 "A．亚洲　　B．非洲　　C．欧洲　　D．美洲",
                 "2．下列知识卡片是关于古代农业文明的描述。"]
        kept = bt.drop_header_notes(lines)
        self.assertEqual(len(kept), 3, "只应剔除 3 行卷首注意事项")
        self.assertTrue(kept[0].startswith("1．你跟随"), "真第 1 题必须保留")

    def test_正文中的编号行不受影响(self):
        sys.path.insert(0, str(SCRIPTS))
        import backtrace_paper as bt
        lines = ["材料一  1．本卷为某校史料汇编。", "（1）概括其内容。（6分）"]
        self.assertEqual(len(bt.drop_header_notes(lines)), 2,
                         "行首不是数字编号的正文行不得误删")


if __name__ == "__main__":
    unittest.main(verbosity=2)
