# Obsidian历史题库搭建与组卷

> 面向中学历史教师的 Obsidian 题库建设方案——「**每道题一个文件**」：正文只放题干/选项/材料，**答案与解析藏在 YAML frontmatter**，学生卷导出安全由此保证。

覆盖历史题库**全生命周期的五个流程**：**存**（扫描件/文字版批量录入，含查重去重）、**校**（BOOK_INDEX 课名词表 + 定位字段对齐校验）、**取**（按册别/单元/课/知识点检索 → 用户圈定 → 导出学生卷＋命题说明＋答题卡）、**研**（多维度考查方向分析 / 册别考试范围分析 → 教学取舍与复习优先级报告）。

## 特性

- ✅ **答案与解析分离**：学生卷导出时自动剥离 frontmatter，答案永不外泄；教师侧的《命题说明》（题目来源与考察角度）为独立内部资料
- ✅ **答案分布自动均衡**：`--均衡选项` 诊断 A/B/C/D 分布与连续同项，直接给出各题「选项序」重排方案
- ✅ **命题说明一键生成**：`--模式 命题说明` 出卷面结构／单元配比／能力维度／难度与答案分布／命题细目表／参考答案／组卷处理说明，全部由题库元数据实时生成
- ✅ **目录即数据库**：按「学段-类别-卷」组织，跨册/跨课筛选靠 YAML 多值字段 + Obsidian Bases/Dataview
- ✅ **课名受控词表**：BOOK_INDEX 按教材版本（2024/2025/2026 版人教版）逐册校验，杜绝错字漏字
- ✅ **素养双轴标注**：核心素养（课标 5 素养）+ 关键能力（郑林 9 要素 A1-C3），受控词表保证标签一致
- ✅ **双链路录入**：扫描件走 MinerU（可选），文字版 docx/md 直读免 OCR；**PPT/pptx 不提供内置脚本**，需自行转成 docx/PDF 或写提取代码（见录入流程链路B）
- ✅ **一题多卷去重**：同一道题被多卷收录时只存权威版本一份，`id` 多值记录全部出处；别名题归档留痕，可回滚
- ✅ **本校考试卷可登记**：组卷后一条命令把「这次考试用过哪些题」记进题库——卷面每题的 `id` 追加 `{学年}-{卷名}-{题号}`、`来源` 成对追加，卷目录只放 `_组卷清单.md`（**不重复存题**）；`--已用 <学年>` 直接排出已用题目供出卷去重，`--撤销` 全部回滚
- ✅ **试卷版式合规**：依 **GB 40070-2021**（强制国标，第 7 章专管考试试卷）核验字号/行空/字体，内置**河南中考卷实测版式原则**（含图文混排）与校验器 `verify_paper_layout.py`
- ✅ **定位字段多值优先**：`册别/单元/课` 标全所有涉及的课（**首项＝命题落点，其后为兼涉项**，不设项数上限），支持**主考／兼涉／全涉**三口径检索；`scan_questions.py` 内置「课 ↔ 单元/册别」对齐提示、**多值配对提示**（单元多于课、册别重复项自动报出）与 **frontmatter 解析失败单列**（带行号定位、退出码阻断）
- ✅ **考区可多值（仅全国卷）**：普通卷写单值考区；**全国卷写当年适用省份清单**（行政区划代码升序），**不再写「全国」**——一题多省属「同一适用面」，`id`/`来源` 不变；统计时**每省各计一次**（考区分布合计 > 题目总数，属预期）
- ✅ **五个流程闭环**：存（录入）→ 校（校验）→ 取（组卷）→ 研（**多维度考查方向分析** / **册别考试范围分析**）+ **反查**（只有成品卷时还原组卷清单），分析流程自带取题、漏标反查与聚合统计脚本
- ✅ **图片格式守卫**：**Obsidian 不渲染 TIFF/EMF/WMF**（存了就是破图）。录入侧 `read_docx.py`（docx 内嵌图）、`pull_pdf_images.py`（PDF 裁剪）已自动挡格式、转 PNG 落盘；存量图片用 `fix_image_formats.py` 巡检修复——转 jpg/png（连续调→JPG 更小、真透明/二值→PNG）、**按路径同步改 md 引用**、原图移入回收站，默认只读、`--fix --apply` 才动手
- ✅ **依赖声明与启动引导**：`requirements.txt` 声明全部依赖，脚本入口经 `qb_bootstrap.py` 做依赖预检（缺包给中文提示，退出码 3）、管道输出强制 UTF-8、vault 定位统一（--vault > `TNOTE_VAULT` > 配置文件）
- ✅ **册别索引全覆盖**：`gen_index.py` 由词表生成 **13 册**试题索引（初中 6 + 高中 7，含纲要上下/选必1-3/选修1-2），每课一个 Dataview 查询块，新题自动流入
- ✅ **脚本全自动化**：JSON → 生成单题 md → 全库校验 → 组卷导出，一条命令链

## 目录结构

```
Obsidian历史题库搭建与组卷/          # 仓库工作区：D:\WorkDaily-工作\...\obsidian-question-bank-workflows
├── SKILL.md                      # 技能主文档（按场景分文件加载的入口）
├── README.md                     # 本文件
├── requirements.txt              # Python 依赖（核心 pyyaml + 可选 docx/pymupdf/numpy/cv2/Pillow）
├── LOCAL-PATCH.md                # 本地改造记录（每条含根因 / 改法 / 回归证据）
├── LICENSE                       # CC BY-NC-SA 4.0
├── .gitignore                    # 忽略 __pycache__ / *.bak-* / 测试临时目录 / skill_config.json
├── scripts/                      # 可执行脚本（扁平：每个 .py 都是可逐条调用的入口）
│   ├── qb_schema.py              # 公共模块：词表（课名+单元层级）+ 路径规则 + 校验 + 权威序
│   ├── qb_bootstrap.py           # 启动引导：依赖预检 / 管道 UTF-8 / vault 定位
│   ├── setup.py                  # 首次配置引导（写入 vault 配置 + 建库）
│   ├── gen_problem.py            # JSON → 单题 md
│   ├── scan_questions.py         # 全库校验（必填/枚举/课名/正文/id 多值/定位字段/多值配对提示/frontmatter YAML 失效单列）
│   ├── dedupe_questions.py       # 查重 + 一题多卷合并（scan/check/merge/rollback）
│   ├── export_student_paper.py   # 组卷导出 CLI（清单驱动 → md/Word 卷 + 答题卡 + 命题说明 + 卷面体检）
│   ├── backtrace_paper.py        # 从成品卷反查题库 id、还原组卷清单（--带题号 保原卷面题号；卷首注意事项自动剔除）
│   ├── verify_paper_layout.py    # 试卷版式校验器（docx 字号行距 / 卷结构层）
│   ├── verify_against_source.py  # 录入后回归自检（入库正文 ↔ 源文本逐题比对，支持单文件多卷）
│   ├── check_note_links.py       # 笔记引用体检（断链 / 指向 .trash 的不合规引用，可改指主卷）
│   ├── linkify_citations.py      # 报告补链（出处表述 → 库内 wikilink，默认只读）
│   ├── gen_index.py              # 由 BOOK_INDEX 生成试题索引（初中 6 册 + 高中 7 册 = 13 册）
│   ├── read_docx.py              # docx 结构化提取（免 MinerU；只接 .docx）
│   ├── import_from_md.py         # 源 md → 录入 JSON：批量回填题干/选项（链路B 第二步）
│   ├── pull_pdf_images.py        # 从官方 PDF 裁剪补图（输出格式守卫：TIFF 自动转 PNG）
│   ├── find_better_images.py     # 低质图筛查 + 开放图库候选检索（只读辅助，不自动替换图片）
│   ├── fix_image_formats.py      # 图片格式巡检修复（不可显示格式 → jpg/png，同步改引用，原图入回收站）
│   ├── fix_arabic_numerals.py    # 中文数字 → 阿拉伯数字批量转换
│   ├── fix_table_render.py       # md 表格不渲染两类根因修复（补空行 / 引用块内转 HTML）
│   ├── fix_quote_to_plain.py     # 报告真题引用块去 `>` 降为普通段落 + HTML 表格还原 md
│   ├── fix_item_spacing.py       # 真题块段落间距压缩（题内紧连、题间留白、表格前空行保护）
│   ├── edit_fields.py            # 定位/标注字段批量编辑（删/加/去重/新增字段，保持 YAML 原写法）
│   ├── normalize_id.py           # id 写法归一（标量/块式 → 流式单行；--check / --apply / --rollback）
│   ├── record_usage.py           # 本校考试卷登记（题目 id/来源 追加卷名 + 卷目录放 _组卷清单.md；出卷去重）
│   ├── scan_lesson.py            # 流程四：按课/单元/专题取题统计（--口径 主考/兼涉/全涉）
│   ├── scan_missed.py            # 流程四：关键词反查（正文含词但未标该课）
│   ├── scan_volume.py            # 流程五：册别/卷别聚合（范围判定 + 课次热度三梯队）
│   ├── BOOK_INDEX.md             # 课名受控词表（初中六册 + 高中纲要/选必 + 选修1-2 共 14 专题）
│   ├── skill_config.json         # 本机 vault 路径（引导写入；已 gitignore，不随分发）
│   └── paperkit/                 # 组卷导出的内部模块（仅供 export_student_paper.py 使用）
│       ├── paths.py              # 题库索引、卷名/题号规范化
│       ├── manifest.py           # 清单加载 / 条目归一 / 题号定位
│       ├── images.py             # 五类图片引用识别、打包、改写
│       ├── markup.py             # 文本净化、表格/图片摘除、选项拆分
│       ├── slots.py              # 作答空位、选项重排
│       ├── sections.py           # 题型判定、分值、大题标题、分节标记
│       ├── spec.py               # 命题说明数据加工
│       ├── md_render.py          # md 学生卷 / 教师卷渲染
│       ├── balance.py            # 答案分布诊断与均衡
│       ├── checkup.py            # 卷面体检
│       ├── docx_kit.py           # Word 布局常量 + 图片装箱计算
│       └── __init__.py           # 包说明与依赖方向
├── workflow/                     # 流程文件（按场景加载）
│   ├── 00-init.md                # 首次使用引导
│   ├── 01-input.md               # 流程一 录入（含查重与合并）
│   ├── 02-compose.md             # 流程二 组卷/出题（含版式合规自检）
│   ├── 03-check.md               # 流程三 校验与词表维护（含定位字段/多值配对提示）
│   ├── 04-angle-analysis.md      # 流程四 多维度考查方向分析
│   └── 05-range-analysis.md      # 流程五 册别/学段考试范围分析
├── references/                   # 参考文档（字段 / 词表 / 版式 / 基线）
│   ├── fields.md                 # 字段详解 + JSON 模板 + 卷名命名约定 + 多值录入判据 + 一题多卷与去重
│   ├── book_index.md             # BOOK_INDEX 结构与维护（含单元层级与修订联动）
│   ├── 素养能力标签体系.md             # 素养/层次水平/能力受控词表（唯一权威）
│   ├── 学史方法正文.md             # 新教材「学史方法」栏目正文要点（含主考/兼涉两档纳入口径）
│   ├── 专题定义-河南中考14专题.md       # 河南中考 14 专题受控定义（课名组/单元组/关键词组）
│   ├── 试卷排版规范.md             # 成卷版式（国标合规层 + 原卷惯例层 + 图文混排）
│   ├── 标准-试卷排版依据.md         # 标准依据（强制/推荐/惯例三档 + 河南中考实测）
│   ├── 试卷版式核验矩阵.md          # 条款 ↔ 依据 ↔ 实现 ↔ 核验方式
│   ├── 参考-图片与图注修复.md        # 录入链路图片问题修复（图注丢失/空 alt/孤儿图）
│   └── 分析基线-洛阳.md            # 实测对照基线（课次热度/卷面结构/分值口径）
├── templates/                    # 模板（初始化时复制到 vault）
│   ├── 历史试题模板.md             # 单题模板
│   ├── 试题研究笔记.md             # A 档：单题/单卷研究
│   ├── 试题报告笔记.md             # B 档：课/单元/专题/册别报告
│   └── 组卷清单.yaml              # 组卷清单模板
└── tests/                        # 离线回归测试（自建临时题库，不碰真库）
    ├── test_qb.py                # 74 条用例：字段/词表、题目文件判定、考区取值相似检测、frontmatter 解析失败、录入→校验→出卷→反查、答案区截断、13 册索引、docx 版式
    ├── _snapshot.py              # 基线工具：出 6 个产物并逐部件比对 docx（拆分/重构时用）
    └── _baseline.json            # 上述工具的基线记录（随仓库保留）
```

## 快速开始

### 1. 初始化

```bash
# ① 一键初始化（写入 vault 配置 + 建六类别目录 + 复制单题模板）
python scripts/setup.py --vault "<你的vault路径>" --init    # 推荐：写入配置并建库
python scripts/setup.py                                    # 或交互式引导

# ② 安装 Python 依赖（一条命令装齐；只跑核心流程则 pip install pyyaml 即可）
pip install -r requirements.txt
```

> **依赖预检**：脚本启动时会检查依赖，缺包时打印中文安装提示并以退出码 3 结束（不再吐裸 traceback）。
> ⚠ 系统 `python` 未必装了 `pyyaml`——2026-09-30 实测本机系统 Python 3.14 只有 `python-docx`，缺 `pyyaml` 会让 `scan_questions.py` 等核心脚本当场失败；受管环境一般带自带解释器（含 pyyaml / python-docx / pymupdf / numpy / cv2 / Pillow），可用它跑全部脚本。
> **编码**：脚本在管道输出（AI 读取、重定向到文件）时自动切 UTF-8，Windows 控制台下 `--单元/--卷/--口径` 等中文参数不再显示为乱码。

> 兼容通道（可选）：环境变量 `TNOTE_VAULT` 指定库根目录，优先级最高——Windows `setx TNOTE_VAULT "<你的vault路径>"`（新开终端生效）；macOS/Linux `export TNOTE_VAULT=/path/to/vault`。手动建库时在 vault 根下创建六类别目录：`初中-1中考真题 / 初中-2中考模拟题 / 初中-3阶段测试`、`高中-1高考真题 / 高中-2高考模拟题 / 高中-3阶段测试`，并复制 `templates/历史试题模板.md` 到 `{vault}/0-Template-模板/`。

> **可选**：扫描版 PDF/图片录入需要 MinerU 服务（`mineru-open-api` CLI + token）。无 token 时文字版文件（docx/md）可直接走免 OCR 链路，不影响核心使用。

### 2. 录入（批量）

```bash
cd scripts
python dedupe_questions.py --check 卷_选择题.json   # ① 入库前查重（逐题返回库内疑似 Top-3）
python gen_problem.py 卷_选择题.json               # ② JSON → 单题 md
python gen_problem.py 卷_非选择题.json
python scan_questions.py --卷 <卷名>               # ③ 输出「✓ 无问题」才交付
```

- **查重命中怎么办**：判为同题 → `dedupe_questions.py --merge <库内主卷.md> --with <本题.md> --apply`（本题不重复入库，`id` 多值记录两卷出处）；判为不同题 → 照常入库
- JSON 字段规范见 `references/fields.md`（含选择题/材料题/论述题模板与多值示例）

### 3. 组卷

```bash
python scan_questions.py --dump                      # 全量题目元数据（按条件过滤候选）
# 组卷：写一份「组卷清单」（有序题目引用，可跨卷），一条命令出卷
python export_student_paper.py --清单 组卷清单.yaml --体检            # 先看卷面结构
python export_student_paper.py --清单 组卷清单.yaml --均衡选项         # 答案分布诊断 + 重排方案
python export_student_paper.py --清单 组卷清单.yaml --模式 学生 --docx 卷_学生版.docx
python export_student_paper.py --清单 组卷清单.yaml --模式 命题说明 --docx 卷_命题说明.docx
python verify_paper_layout.py --docx "<成卷.docx>"   # 版式校验（有「✗ 不合规」不得交付）
```

> 清单模板见 `templates/组卷清单.yaml`：题目只写 id，可加 `分值/来源/角度/选项序/括号/小问分值`。
> **顺序即卷面顺序**，脚本自动重编号；`--均衡选项` 给出重排方案，写回清单 `选项序: [3,1,2,4]` 即生效（答案自动换算）；
> 每次出卷自动打印**卷面体检**（答案分布/考点覆盖/同课多题告警）。成卷后按 `references/试卷排版规范.md` 定版：
> 正文 **宋体 12pt（小四）+ 固定行距 24pt** 方可满足 GB 40070-2021 的强制下限；页面可选 A4 打印模式或 185×260mm 中考仿真模式。

### 4. 研题（考查方向 / 考试范围分析）

```bash
# 流程四 · 单课考查方向（取题 → 统计 → 归纳方向 → 出报告）
python scripts/scan_lesson.py --lesson "第5课 古代希腊" --口径 主考     # 口径：主考 / 兼涉 / 全涉
python scripts/scan_lesson.py --lesson "第5课 古代希腊" --body          # 附题干全文
python scripts/scan_missed.py "第5课 古代埃及" "金字塔|法老|尼罗河"      # 关键词反查：正文含词但未标该课

# 流程五 · 册别/学段考试范围（范围判定 + 卷面结构 + 课次热度三梯队 + 能力素养分布）
python scripts/scan_volume.py --册 9上
python scripts/scan_volume.py --册 9上 --卷 "洛阳市2025-2026学年9上期末"
```

> 两个分析流程的**报告落点统一为 vault `3-试题研究/`**；对照基线（洛阳各册实测结论、分值口径）见 `references/分析基线-洛阳.md`。报告笔记的属性字段、命名与模板见 `references/fields.md`「试题研究笔记属性」（模板：`templates/试题报告笔记.md`）。流程细节：`workflow/04-angle-analysis.md`、`workflow/05-range-analysis.md`。

## 文档

- **操作手册**：`SKILL.md` — 存/校/取/研五流程、铁律、陷阱清单
- **字段规范**：`references/fields.md` — YAML 字段表、JSON 模板、卷名命名约定、**多值录入判据（首项＝命题落点）**、**一题多卷与去重（判定流水线 + 合并规则）**
- **词表维护**：`references/book_index.md` — BOOK_INDEX 结构、修订联动
- **素养词表**：`references/素养能力标签体系.md` — 核心素养/层次水平/关键能力受控词表
- **成卷版式**：`references/试卷排版规范.md` — 卷头/大题/选项/图文混排/合规字号行距/交付前自检
- **排版依据**：`references/标准-试卷排版依据.md` — GB 40070-2021（强制）条文与数值、河南中考卷实测、纸张与折页规格
- **核验矩阵**：`references/试卷版式核验矩阵.md` — 条款↔依据↔实现↔核验方式（脚本/人工/印制）
- **分析基线**：`references/分析基线-洛阳.md` — 洛阳各册实测结论与分值口径（对照用，逐年更新）

## 来源与许可

- **技能名与仓库名**：本技能显示名与本地目录名同为 `Obsidian历史题库搭建与组卷`；GitHub 仓库名为英文 [lixy1998/obsidian-question-bank-workflows](https://github.com/lixy1998/obsidian-question-bank-workflows)（旧址 `obsidian-history-question-bank` 301 重定向）
- 课名词表（BOOK_INDEX）依据人民教育出版社 2024/2025/2026 版初中历史教材（按教材首印定版年份命名版本节；**9 下已于 2026-08-27 据教材实物核对，子目全部确认**）与统编高中教材（纲要上下 + 选必 1-3，仅用于定位字段校验）
- 素养词表依据《义务教育历史课程标准（2022年版）》《普通高中历史课程标准（2017年版2020年修订）》及郑林「历史学科能力 3 层次 9 要素」、徐奉先 3×3 能力框架整理
- 代码与文档采用 **CC BY-NC-SA 4.0**（署名-非商业性使用-相同方式共享），详见 `LICENSE`
- 作者：**教历史的史知行**（微信公众号「教历史的史知行」）

## FAQ

**Q：不用 Obsidian 能用吗？** 核心流程（录入→校验→组卷→分析）只依赖 Python + 题库目录，Obsidian 仅用于人工浏览/检索（Bases/Dataview 查询）与手工补录（Templater 模板）。

**Q：能回答「这一课考什么」「这本书期末考什么」吗？** 能，且是基于题库实证而非经验判断——见「4. 研题」：`scan_lesson.py`（单课，可切主考/兼涉/全涉口径）+ `scan_volume.py`（册别，含范围判定与课次热度三梯队），流程与报告结构见 `workflow/04-angle-analysis.md`、`workflow/05-range-analysis.md`。

**Q：需要配 MinerU 吗？** 仅扫描版 PDF/图片需要。文字版 docx/md 走直读链路，无需任何外部服务。**Q：PPT 能直接录入吗？** 不能——本 skill 无 pptx 提取脚本，请先另存为 docx 或导出 PDF，再走对应链路。

**Q：原创题/改编题怎么存？** 不新建目录，靠「来源」多值标签区分（如 `[8上教材第1课, 教材改编]`），改编题「年份」写改编年份。

**Q：同一道题被两套卷收录（如中考真题被期末卷原样收录）怎么办？** 走「一题多卷」：`dedupe_questions.py --check` 预检 → 裁定 → `--merge` 把两卷 id 并入库内主卷（`id` 多值、`来源` 成对平铺），别名题归档到 `.trash/`（库内 Obsidian 回收站，面板可还原）并留卷内 `_重复题清单.md`，全程日志可 `--rollback`。判定与合并规则见 `references/fields.md`「一题多卷与去重」。

**Q：词表怎么维护？** 词表条目就地修订后按 `references/book_index.md` 的「修订联动」清单排查（存量题课字段重映射 + 重跑 gen_index），只改词表不动关联项 = 不完整修订。
