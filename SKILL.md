---
name: Obsidian历史题库搭建
description: Obsidian 历史题库建设——「每道题一个文件」：正文只放题干/选项/材料，答案与解析藏 YAML frontmatter。
触发词：历史题库、题库搭建、录入题库、录入题目/试题、录入到题库、出题、组卷、出一套卷子、BOOK_INDEX、课名词表、题库质量检查。
version: 1.1.0
license: CC BY-NC-SA 4.0
author: lixy1998
---

# Obsidian历史题库搭建

管理Obsidian 历史题库：**存题**（录入）、**校题**（BOOK_INDEX 词表校验）、**组卷**（按条件检索→用户决定→出学生/教师卷）。

## 按需加载（必读）

**本 skill 按场景分文件加载，只读当前任务所需文件，不全文通读**（流程文件在 `workflow/`，参考文档在 `references/`）：

| 触发场景 | 读取文件 |
|---|---|
| 首次使用 / 脚本报「未配置题库库」 | `workflow/00-init.md` |
| 录入题库（录入到题库、录入题目/试题） | `workflow/01-input.md` |
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
| 脚本（本 skill 内） | `<skill>/scripts/`：qb_schema.py / gen_problem.py / scan_questions.py / export_student_paper.py / gen_index.py（从 BOOK_INDEX 生成 1-MoC-索引 六册试题索引，词表更新后重跑重建）/ **read_docx.py（文字版 docx 结构化提取，免 MinerU）** / **pull_pdf_images.py（从官方 PDF 裁剪补图）** / fix_arabic_numerals.py（中文数字→阿拉伯数字批量转换，覆盖题库 YAML / 词表两处）/ **setup.py（首次配置引导：写入 vault + 建库）** |
| 课名词表（本 skill 内） | `<skill>/scripts/BOOK_INDEX.md`（唯一权威，随 skill 分发） |
| 单题模板（本 skill 内） | `<skill>/templates/历史试题模板.md`（初始化时复制到 vault 模板目录） |
| 本机配置（本 skill 内） | `<skill>/scripts/skill_config.json`（`{"vault": "..."}`；**已入 .gitignore，不随仓库发布**） |
| Python | 运行环境矩阵：**venv**（pyyaml）跑 qb_schema / scan / gen_problem / export / gen_index / fix_arabic_numerals / setup；**主环境**跑 `read_docx.py`（python-docx）、`pull_pdf_images.py`（pymupdf）及 PPT/md 链路B 提取（python-pptx）——venv 未装这些依赖，勿用 venv 跑；依赖安装见「初始化」 |

**类别枚举**（学段-序号类别，原创/改编不占目录）：初中：`1中考真题 / 2中考模拟题 / 3阶段测试`；高中：`1高考真题 / 2高考模拟题 / 3阶段测试`。

**来源 = 多值标签**：出处标签自由（卷名/教材课名，如 `2026河南中考`、`8上教材第1课`）+ 类型标签建议受控（`真题 / 教材改编 / 中考改编 / 原创题 / 期末卷 / 模拟卷`）。**原创题/改编题不新建文件夹**，靠来源标签区分；改编题「年份」写改编年份（非原卷年份）。

## 字段速查

YAML 字段：id/册别/单元/课/小节/知识点/核心素养/素养层次水平/关键能力/题型/难度/分值/来源/年份/考区/答案/解析
- 册别 11：7上~9下 + 纲要上/下 + 选必1-3；题型 6：选择题/材料解析题/论述题/综合探究题/小论文/填空题；难度 3：易/中/难
- **多值字段**：`册别/单元/课/小节/知识点/核心素养/素养层次水平/关键能力` 均为 list，可多值；跨册别/跨单元/跨课综合题各字段填完整多值 list（主考点在前），单值 str 旧数据自动兼容
- 课名必须与 BOOK_INDEX 对应册别词表一致（多值时课名须命中任一已列册别词表；含引号课名如「第3课 “开元盛世”」须用中文弯引号）
- **核心素养/素养层次水平/关键能力**：受控词表见技能内 `references/素养能力词表.md`（唯一权威词表，不依赖 vault 路径；条目 `⟦…⟧` 内为来源/示例注记，入库时只取 `⟦` 前内容、注记不写入 YAML）——核心素养=`素养-细目`（如 `[史料实证-史料信息的提取, 历史解释-史事的因果性阐释]`）；素养层次水平=初高中分轨（初中 `素养名称-层次N-体现`、高中 `素养名称-水平N-体现`，**不跨学段换算**）；关键能力=郑林 9 要素编号（如 `[A3-概括, B2-分析]`）
- **来源为多值标签 list**：`[2026河南中考, 真题]`、`[8上教材第1课, 教材改编]`、`[2026原创, 原创题]`；改编题年份=改编年份

## 全局陷阱（跨流程，血泪）

1. **Obsidian 锁文件**：库打开时图片复制可能 WinError 32，脚本幂等跳过；重试即可
2. **Dataview DQL 语法**（2026-08-22 三次排查定案）：无 `CONTAINS` 操作符；检索一律 `contains(字段, "值")` 函数语法；字符串字面量只认双引号——操作符写法或单引号参数必 PARSING FAILED；词表课名里的中文引号（“”）无害，勿误删

> 各流程专属陷阱见对应流程文件（录入：YAML 特殊字符/材料题图片/第三方整理冲突；组卷：导出文件勿入库；校验：索引列由 gen_index.py 决定/录入组卷勿跑 gen_index.py）。

## References

- `workflow/00-init.md` — 首次使用引导（vault 配置 + 建库 + 依赖），含初始化铁律
- `workflow/01-input.md` — 流程一 录入（链路A/B、写 JSON、生成+校验、模板使用），含录入铁律与陷阱
- `workflow/02-compose.md` — 流程二 组卷/出题（检索、列候选、export 出卷、册别索引），含组卷铁律与陷阱
- `workflow/03-check.md` — 流程三 校验与词表维护（scan、词表校准、索引维护），含校验铁律与陷阱
- `references/fields.md` — 字段详解 + JSON 模板（选择题/材料题/小论文）+ 目录规则 + 卷名命名约定
- `references/book_index.md` — BOOK_INDEX 结构与维护、9上9下核实状态
- `references/素养能力词表.md` — 核心素养/素养层次水平/关键能力受控词表（唯一权威源；含 `⟦…⟧` 注记标记与注记剔除规则、初高中分轨（初中层次1-3、高中水平1-4）+标注规则、郑林 9 要素主词表+徐奉先对照迁移映射、字段检索示例）
