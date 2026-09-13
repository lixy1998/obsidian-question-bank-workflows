# Obsidian 历史题库搭建

> 面向中学历史教师的 Obsidian 题库建设方案——「**每道题一个文件**」：正文只放题干/选项/材料，**答案与解析藏在 YAML frontmatter**，学生卷导出安全由此保证。

管理历史题库的三大流程：**存题**（扫描件/文字版文件批量入库）、**校题**（BOOK_INDEX 课名词表自动校验）、**组卷**（按册别/单元/课/知识点检索 → 用户圈定 → 导出学生/教师双版）。

## 特性

- ✅ **答案与解析分离**：学生卷导出时自动剥离 frontmatter，答案永不外泄
- ✅ **目录即数据库**：按「学段-类别-卷」组织，跨册/跨课筛选靠 YAML 多值字段 + Obsidian Bases/Dataview
- ✅ **课名受控词表**：BOOK_INDEX 按教材版本（2024/2025/2026 版人教版）逐册校验，杜绝错字漏字
- ✅ **素养双轴标注**：核心素养（课标 5 素养）+ 关键能力（郑林 9 要素 A1-C3），受控词表保证标签一致
- ✅ **双链路录入**：扫描件走 MinerU（可选），文字版 docx/PPT/md 直读免 OCR
- ✅ **一题多卷去重**（v1.3.0）：同一道题被多卷收录时只存权威版本一份，`id` 多值记录全部出处；别名题归档留痕，可回滚
- ✅ **试卷版式合规**（v1.4.0）：依 **GB 40070-2021**（强制国标，第 7 章专管考试试卷）核验字号/行空/字体，内置**河南中考卷实测版式原则**（含图文混排）与校验器 `verify_paper_layout.py`
- ✅ **脚本全自动化**：JSON → 生成单题 md → 全库校验 → 组卷导出，一条命令链

## 目录结构

```
obsidian-question-bank-workflows/
├── SKILL.md                    # 技能主文档（三流程操作手册）
├── README.md
├── LICENSE                     # CC BY-NC-SA 4.0
├── templates/
│   └── 历史试题模板.md           # 单题模板（初始化时复制到 vault）
├── workflow/
│   ├── 00-init.md              # 首次使用引导
│   ├── 01-input.md             # 流程一 录入（含查重与合并）
│   ├── 02-compose.md           # 流程二 组卷/出题（含版式合规自检）
│   └── 03-check.md             # 流程三 校验与词表维护（含查重体检）
├── references/
│   ├── fields.md               # 字段详解 + JSON 模板 + 卷名命名约定 + 一题多卷与去重
│   ├── book_index.md           # BOOK_INDEX 结构与维护
│   ├── 素养能力词表.md           # 素养/层次水平/能力受控词表（唯一权威）
│   ├── 试卷排版规范.md           # 成卷版式（国标合规层 + 原卷惯例层 + 图文混排）
│   ├── 标准-试卷排版依据.md       # 标准依据（强制/推荐/惯例三档 + 河南中考实测）
│   └── 试卷版式核验矩阵.md        # 条款 ↔ 依据 ↔ 实现 ↔ 核验方式
└── scripts/
    ├── qb_schema.py            # 公共模块：词表 + 路径规则 + 校验 + 权威序
    ├── gen_problem.py          # JSON → 单题 md
    ├── scan_questions.py       # 全库校验（必填/枚举/课名/正文非空/id 多值）
    ├── dedupe_questions.py     # 查重 + 一题多卷合并（scan/check/merge/rollback）
    ├── verify_paper_layout.py  # 试卷版式校验器（docx 字号行距 / 卷结构层）
    ├── export_student_paper.py # 组卷导出（学生/教师双版）
    ├── gen_index.py            # 由 BOOK_INDEX 生成六册试题索引
    ├── read_docx.py            # docx 结构化提取（免 MinerU）
    ├── pull_pdf_images.py      # 从官方 PDF 裁剪补图
    ├── fix_arabic_numerals.py  # 中文数字 → 阿拉伯数字批量转换
    └── BOOK_INDEX.md           # 课名受控词表（六册：7上~9下）
```

## 快速开始

### 1. 初始化

```bash
# ① 设置题库库根目录环境变量（Obsidian vault）
setx TNOTE_VAULT "D:/Documents/TNote"        # Windows（新开终端生效）
# export TNOTE_VAULT=/path/to/TNote           # macOS / Linux

# ② 安装 Python 依赖
pip install pyyaml                             # 核心流程必需
pip install python-docx pymupdf python-pptx    # 可选：docx 直读 / PDF 补图 / PPT 提取

# ③ 复制单题模板到 vault 模板目录
cp templates/历史试题模板.md "$TNOTE_VAULT/0-Template-模板/"

# ④ 建库：在 vault 根下创建类别目录
#    初中-1中考真题 / 初中-2中考模拟题 / 初中-3阶段测试
#    高中-1高考真题 / 高中-2高考模拟题 / 高中-3阶段测试
```

> **可选**：扫描版 PDF/图片录入需要 MinerU 服务（`mineru-open-api` CLI + token）。无 token 时文字版文件（docx/PPT/md）可直接走免 OCR 链路，不影响核心使用。

### 2. 录入（批量）

```bash
cd scripts
python dedupe_questions.py --check 卷_选择题.json   # ① 入库前查重（逐题返回库内疑似 Top-3）
python gen_problem.py 卷_选择题.json               # ② JSON → 单题 md
python gen_problem.py 卷_非选择题.json
python scan_questions.py --卷 <卷名>               # ③ 输出「✓ 无问题」才交付
```

- **查重命中怎么办**：判为同题 → `dedupe_questions.py --merge <库内主卷.md> --with <本题.md> --apply`（本题不重复入库，`id` 多值记录两卷出处）；判为不同题 → 照常入库
- JSON 字段规范见 `references/fields.md`（含选择题/材料题/小论文模板与多值示例）

### 3. 组卷

```bash
python scan_questions.py --dump               # 全量题目元数据（按条件过滤候选）
python export_student_paper.py --卷 <来源卷> --only 01,03,05 --模式 学生   # 学生版
python export_student_paper.py --卷 <来源卷> --only 01,03,05 --模式 教师   # 教师版
python verify_paper_layout.py --docx "<成卷.docx>"   # 版式校验（有「✗ 不合规」不得交付）
```

> `--only` 只导出用户圈定的题号；学生版自动剥离答案与解析。成卷后按 `references/试卷排版规范.md` 定版：正文 **宋体 12pt（小四）+ 固定行距 24pt** 方可满足 GB 40070-2021 的强制下限；页面可选 A4 打印模式或 185×260mm 中考仿真模式。

## 文档

- **操作手册**：`SKILL.md` — 录入/组卷/校验三流程、铁律、陷阱清单
- **字段规范**：`references/fields.md` — YAML 字段表、JSON 模板、卷名命名约定、**一题多卷与去重（判定流水线 + 合并规则）**
- **词表维护**：`references/book_index.md` — BOOK_INDEX 结构、修订联动
- **素养词表**：`references/素养能力词表.md` — 核心素养/层次水平/关键能力受控词表
- **成卷版式**：`references/试卷排版规范.md` — 卷头/大题/选项/图文混排/合规字号行距/交付前自检
- **排版依据**：`references/标准-试卷排版依据.md` — GB 40070-2021（强制）条文与数值、河南中考卷实测、纸张与折页规格
- **核验矩阵**：`references/试卷版式核验矩阵.md` — 条款↔依据↔实现↔核验方式（脚本/人工/印制）

## 来源与许可

- 课名词表（BOOK_INDEX）依据人民教育出版社 2024/2025/2026 版初中历史教材（按教材首印定版年份命名版本节；9 下部分子目标注 `[?]` 待实物确认）
- 素养词表依据《义务教育历史课程标准（2022年版）》《普通高中历史课程标准（2017年版2020年修订）》及郑林「历史学科能力 3 层次 9 要素」、徐奉先 3×3 能力框架整理
- 代码与文档采用 **CC BY-NC-SA 4.0**（署名-非商业性使用-相同方式共享），详见 `LICENSE`

## FAQ

**Q：不用 Obsidian 能用吗？** 核心流程（录入→校验→组卷）只依赖 Python + 题库目录，Obsidian 仅用于人工浏览/检索（Bases/Dataview 查询）与手工补录（Templater 模板）。

**Q：需要配 MinerU 吗？** 仅扫描版 PDF/图片需要。文字版 docx/PPT/md 走直读链路，无需任何外部服务。

**Q：原创题/改编题怎么存？** 不新建目录，靠「来源」多值标签区分（如 `[8上教材第1课, 教材改编]`），改编题「年份」写改编年份。

**Q：同一道题被两套卷收录（如中考真题被期末卷原样收录）怎么办？** 走「一题多卷」：`dedupe_questions.py --check` 预检 → 裁定 → `--merge` 把两卷 id 并入库内主卷（`id` 多值、`来源` 成对平铺），别名题归档到 `_归档/重复题/` 并留卷内 `_重复题清单.md`，全程日志可 `--rollback`。判定与合并规则见 `references/fields.md`「一题多卷与去重」。

**Q：词表怎么维护？** 词表条目就地修订后按 `references/book_index.md` 的「修订联动」清单排查（存量题课字段重映射 + 重跑 gen_index），只改词表不动关联项 = 不完整修订。
