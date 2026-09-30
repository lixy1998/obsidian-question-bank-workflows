# 初始化（首次使用，引导式）

> **本文件为独立流程文件**：仅在**首次使用**或 **vault 未配置**时读取（脚本报「未配置题库库」/ `scripts/skill_config.json` 无有效 vault）。配置完成后不再读。
>
> **前置检测**：`TNOTE_VAULT` 环境变量（优先级最高）> `<skill>/scripts/skill_config.json` 的 `vault` 字段 > 引导报错。

**核心流程：首次使用（或 vault 未配置）时，主动询问用户，不预设**——① 问题库库（vault）在哪（有现成的给路径，没有的给想新建的路径）；② 将路径写入 `<skill>/scripts/skill_config.json`（`python setup.py --vault <路径> --init`，`--init` 自动建库）；③ 之后所有脚本自动读取。

1. **AI 引导**（推荐）：用户说「录入到题库」等时，若脚本报「未配置题库库」→ 主动询问 vault 位置 → 用户提供后执行 `python scripts/setup.py --vault "<路径>" --init`（或仅 `--vault`，不建库）→ 提示完成
2. **命令行引导**：`python scripts/setup.py`（交互式，可新建）或 `python scripts/setup.py --vault <路径> --init`（一键写入+建库）
3. **兼容通道（可选）**：环境变量 `TNOTE_VAULT` 优先级最高（Windows `setx TNOTE_VAULT "<你的vault路径>"`；macOS/Linux 写入 shell 配置）
4. **Python 依赖**（2026-09-30 起统一声明在 `<skill>/requirements.txt`）：
   - 核心流程（写 JSON → gen → scan → export → 分析统计）只需 `pyyaml`
   - 链路B 扩展（可选）：`python-docx`（docx 直读 / Word 卷导出）、`pymupdf`（PDF 补图）；图片修复另需 `numpy` / `opencv-python` / `Pillow`。（**PPT/pptx 无内置脚本**：自行转 docx/PDF 或自写 `python-pptx` 提取代码）
   - **一条命令装齐**：`pip install -r <skill>/requirements.txt`（只装核心：`pip install pyyaml`）
   - **先探测解释器，再决定**：脚本会做依赖预检——缺包时打印「请安装依赖后重试」的中文提示并以退出码 3 结束，不再吐裸 traceback。**不要默认假设系统 `python` 已装 pyyaml**（实测常见只有 python-docx），受管环境一般有自带解释器（如 `~/.workbuddy/binaries/python/envs/default/python.exe`），可用它直接跑全部脚本
   - 输出编码：脚本在**管道输出**（AI 读取、重定向到文件）时自动切 UTF-8，避免 Windows 控制台 GBK 把 `--单元/--卷/--口径` 等中文参数显示成乱码；真实控制台保持系统编码不变
5. **建库**：`--init` 自动建六类别目录 + 复制 `templates/` 下全部模板到 `{vault}/0-Template-模板/`（历史试题模板 + 试题研究笔记 + 试题报告笔记）；已建库的跳过
6. **可选：MinerU 服务**（仅扫描版 PDF/图片需要，链路A）：安装 `mineru-open-api` CLI 并配置 token（`~/.mineru/config.yaml`）；文字版文件（docx/md）走链路B 免 MinerU

> 脚本路径用 `<skill>/scripts/` 指代；在 skill 目录内执行时直接 `cd scripts && python …`。

## 铁律（初始化专属）

1. **首次使用不预设**：vault 未配置时主动询问用户（有现成的给路径、没有的给想新建的路径），禁止猜测默认路径静默写入
2. **目录即数据库**：目录按「学段-类别-卷」组织（保留原卷可追溯），跨册/单元/课筛选靠 YAML + Obsidian Bases/Dataview；原创/改编题不建目录，靠来源标签
3. **配置不入库**：vault 路径写入 `<skill>/scripts/skill_config.json`（已入 .gitignore，不随仓库发布）；环境变量 `TNOTE_VAULT` 优先级最高

## 完成标志

- `skill_config.json` 含有效 vault（或 `TNOTE_VAULT` 已设）
- `--init` 已建六类别目录（初中：`1中考真题 / 2中考模拟题 / 3阶段测试`；高中：`1高考真题 / 2高考模拟题 / 3阶段测试`）
- 模板已复制到 `{vault}/0-Template-模板/`：`历史试题模板.md`（单题）、`试题研究笔记.md`（A 档研究）、`试题报告笔记.md`（B 档报告）

完成后回到主 SKILL.md，按触发场景读取对应流程文件。
