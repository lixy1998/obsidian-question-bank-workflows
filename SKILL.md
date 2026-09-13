---
name: Obsidian历史题库搭建
description: Obsidian 历史题库建设——「每道题一个文件」：正文只放题干/选项/材料，答案与解析藏 YAML frontmatter。
触发词：历史题库、题库搭建、录入题库、录入题目/试题、录入到题库、出题、组卷、出一套卷子、BOOK_INDEX、课名词表、题库质量检查、查重、题目重复、一题多卷、去重。
version: 1.3.0
license: CC BY-NC-SA 4.0
author: lixy1998
---

# Obsidian历史题库搭建

管理Obsidian 历史题库：**存题**（录入，含查重）、**校题**（BOOK_INDEX 词表校验）、**组卷**（按条件检索→用户决定→出学生/教师卷）、**去重**（一题多卷合并，v1.3.0 起）。

## 按需加载（必读）

**本 skill 按场景分文件加载，只读当前任务所需文件，不全文通读**（流程文件在 `workflow/`，参考文档在 `references/`）：

| 触发场景 | 读取文件 |
|---|---|
| 首次使用 / 脚本报「未配置题库库」 | `workflow/00-init.md` |
| 录入题库（录入到题库、录入题目/试题） | `workflow/01-input.md` |
| 查重 / 发现重复题 / 一题多卷合并 | `workflow/01-input.md`「查重与合并」+ `references/fields.md`「一题多卷与去重」 |
| 组卷/出题（出题、组卷、出一套卷子、某范围检索） | `workflow/02-compose.md` |
| 校验/词表维护（题库质量检查、scan、BOOK_INDEX 维护） | `workflow/03-check.md` |

- **未检测到初始化**（`<skill>/scripts/skill_config.json` 无有效 vault 且无 `TNOTE_VAULT`）→ 先读 `workflow/00-init.md`，再回到对应流程文件
- 各流程文件自带**该环节专属铁律与陷阱**，跟随所在文件执行
- 字段/词表细节：`references/fields.md`（字段详解+JSON 模板）、`references/book_index.md`（词表结构）、`references/素养能力词表.md`（受控词表）

## 目录规范（数据文件不入 skill）

- **过程性数据文件（一次性题库 JSON、临时生成脚本、图片缓存、中间产物等）严禁放入 skill 目录**——`scripts/` 只保留随 skill 分发的可复用脚本与 BOOK_INDEX 词表；`templates/` 只放模板；`references/` 只放参考文档；`workflow/` 只放流程文件
- 题库录入的中间 JSON / 一次性 gen 脚本在题目入库（gen + scan 完成）后，**归档到工作区或删除**，不得留在 skill 内

## 资产位置

> **题库库根目录（vault）配置**：首次使用引导写入 `<skill>/scripts/skill_config.json`（优先级：环境变量 `TNOTE_VAULT` > 配置文件 > 引导报错）。以下路径均以 `{vault}` 表示。

| 资产 | 路径 |
|---|---|
| 题库库（Obsidian vault） | `{vault}/{学段}-{序号类别}/{卷名}/{卷名}_{题型}/{序号}.md`（如 `{vault}/初中-1中考真题/2026河南中考/…`；类别枚举见下） |
| 脚本（本 skill 内） | `<skill>/scripts/`：qb_schema.py / gen_problem.py / scan_questions.py / export_student_paper.py / gen_index.py（从 BOOK_INDEX 生成 1-MoC-索引 六册试题索引，词表更新后重跑重建）/ **read_docx.py（文字版 docx 结构化提取，免 MinerU）** / **pull_pdf_images.py（从官方 PDF 裁剪补图）** / **dedupe_questions.py（查重 + 一题多卷合并：--scan/--check/--merge/--list-merged/--rollback）** / fix_arabic_numerals.py（中文数字→阿拉伯数字批量转换，覆盖题库 YAML / 词表两处）/ **setup.py（首次配置引导：写入 vault + 建库）** |
| 课名词表（本 skill 内） | `<skill>/scripts/BOOK_INDEX.md`（唯一权威，随 skill 分发） |
| 单题模板（本 skill 内） | `<skill>/templates/历史试题模板.md`（初始化时复制到 vault 模板目录） |
| 本机配置（本 skill 内） | `<skill>/scripts/skill_config.json`（`{"vault": "..."}`；**已入 .gitignore，不随仓库发布**） |
| Python | 运行环境矩阵：**venv**（pyyaml）跑 qb_schema / scan / gen_problem / export / gen_index / fix_arabic_numerals / setup；**主环境**跑 `read_docx.py`（python-docx）、`pull_pdf_images.py`（pymupdf）及 PPT/md 链路B 提取（python-pptx）——venv 未装这些依赖，勿用 venv 跑；依赖安装见「初始化」 |

**类别枚举**（学段-序号类别，原创/改编不占目录）：初中：`1中考真题 / 2中考模拟题 / 3阶段测试`；高中：`1高考真题 / 2高考模拟题 / 3阶段测试`。

**来源 = 多值标签**：出处标签自由（卷名/教材课名，如 `2026河南中考`、`8上教材第1课`）+ 类型标签建议受控（`真题 / 教材改编 / 中考改编 / 原创题 / 期末卷 / 模拟卷`）。**原创题/改编题不新建文件夹**，靠来源标签区分；改编题「年份」写改编年份（非原卷年份）。**多卷收录同一题时成对平铺**：`[主卷名, 类型, 别名卷名, 别名类型]`。

## 字段速查

YAML 字段：id/册别/单元/课/小节/知识点/核心素养/素养层次水平/关键能力/题型/难度/分值/来源/年份/考区/答案/解析
- 册别 11：7上~9下 + 纲要上/下 + 选必1-3；题型 6：选择题/材料解析题/论述题/综合探究题/小论文/填空题；难度 3：易/中/难
- **多值字段**：`id/册别/单元/课/小节/知识点/核心素养/素养层次水平/关键能力` 均为 list，可多值；跨册别/跨单元/跨课综合题各字段填完整多值 list（主考点在前），单值 str 旧数据自动兼容
- **id 多值 = 一题多卷**：首项 = 主卷 id（本题文件所在卷），其后 = 收录本题的其他卷 id（如 `[2024河南中考-08, 河南省实验中学2025-2026学年8上期末-03]`）；重复题只在主卷存一份，别名题归档到 `_归档/重复题/`——**查重/合并一律用 `dedupe_questions.py`，禁止手工删题**（规则见 `references/fields.md`「一题多卷与去重」）
- 课名必须与 BOOK_INDEX 对应册别词表一致（多值时课名须命中任一已列册别词表；含引号课名如「第3课 “开元盛世”」须用中文弯引号）；**学史方法**写 `学史方法-<完整方法名>`（六册共 15 个，见 BOOK_INDEX「初中·学史方法（全六册）」表；只校验方法名、不校验册别）
- **核心素养/素养层次水平/关键能力**：受控词表见技能内 `references/素养能力词表.md`（唯一权威词表，条目 `⟦…⟧` 内为来源/示例注记，入库时只取 `⟦` 前内容、注记不写入 YAML）——核心素养=`素养-细目`（如 `[史料实证-史料信息的提取, 历史解释-史事的因果性阐释]`）；素养层次水平=初高中分轨（初中 `素养名称-层次N-体现`、高中 `素养名称-水平N-体现`，**不跨学段换算**）；关键能力=郑林 9 要素编号（如 `[A3-概括, B2-分析]`）
- **来源为多值标签 list**：`[2026河南中考, 真题]`、`[8上教材第1课, 教材改编]`、`[2026原创, 原创题]`；改编题年份=改编年份

## 全局陷阱（跨流程，血泪）

1. **Obsidian 锁文件**：库打开时图片复制可能 WinError 32，脚本幂等跳过；重试即可
2. **Dataview DQL 语法**：无 `CONTAINS` 操作符；检索一律 `contains(字段, "值")` 函数语法；字符串字面量只认双引号——操作符写法或单引号参数必 PARSING FAILED；词表课名里的中文引号（“”）无害，勿误删
3. **YAML list 两种风格并存**：存量题库的 list 字段既有**块状**（`key:` 换行后顶格 `- 值`）也有**流式**（`key: [值]`）写法；脚本改写字段时必须按原风格渲染、并完整吞掉旧值（**含顶格 `- ` 行**，只吞缩进行会残留成游离项直接破坏 YAML）——2026-09-13 dedupe merge 实测踩坑，已修复并加写盘自检

> 各流程专属陷阱见对应流程文件（录入：YAML 特殊字符/材料题图片/第三方整理冲突；组卷：导出文件勿入库；校验：索引列由 gen_index.py 决定/录入组卷勿跑 gen_index.py）。

## References

- `workflow/00-init.md` — 首次使用引导（vault 配置 + 建库 + 依赖），含初始化铁律
- `workflow/01-input.md` — 流程一 录入（链路A/B、写 JSON、生成+校验、模板使用、**查重与合并**），含录入铁律与陷阱
- `workflow/02-compose.md` — 流程二 组卷/出题（检索、列候选、export 出卷、册别索引），含组卷铁律与陷阱
- `workflow/03-check.md` — 流程三 校验与词表维护（scan、词表校准、索引维护），含校验铁律与陷阱
- `references/fields.md` — 字段详解 + JSON 模板（选择题/材料题/小论文）+ 目录规则 + 卷名命名约定
- `references/book_index.md` — BOOK_INDEX 结构与维护、9上9下核实状态
- `references/素养能力词表.md` — 核心素养/素养层次水平/关键能力受控词表（唯一权威源；含 `⟦…⟧` 注记标记与注记剔除规则、初高中分轨（初中层次1-3、高中水平1-4）+标注规则、郑林 9 要素主词表+徐奉先对照映射、字段检索示例）
- `references/试卷排版规范.md` — **成卷版式规范**（卷头/大题结构/小题连续编号/选项断行/材料题版式/图片处理/答案卷/字体/文件命名/组卷适配操作），md 卷与 Word 卷均适用；题面信息标注格式另见「历史原创命题」语言规范第六节
