# LOCAL-PATCH — 本地改动记录

> 本 skill 为自研项目（GitHub：`lixy1998/obsidian-question-bank-workflows`），改动均有 git 历史可溯。本文件登记**对脚本行为的关键改造**与验证样本，供跨机同步、重装或回溯时快速定位非显而易见的行为变化。
> 每次改动请记录：日期、文件、改动内容、原因、复现步骤。

---

## 2026-09-18 · `scripts/verify_against_source.py` 多卷单文件支持

**背景**：源 markdown 为**单文件含多卷**（如《解析与检测》上册 1 个 md 含 2 套卷、下册 1 个 md 含 8 套卷）时，原 `split_source()` 无法定位目标卷，把整文件（含其余卷与参考答案区）当作单卷正文比对 → 相似度暴跌至 0.05~0.25，且**静默给出错误结论**。

**改动**：

1. 新增 `split_volumes(text) -> [{title, body}, ...]`
   - 按一级标题（`# …`）切分源文本，返回各卷标题与正文
2. 新增 `match_juan(vol_title, juan) -> bool`
   - 用卷名尾部数字与源标题内的数字比对（阿拉伯数字与中文数字均支持）
   - 中文数字用**显式字表映射**（现为 `cn2int()`，支持 一~九十九 与纯数字串），**不可用 `int()` 转换或字符码/索引推导**。⚠ 2026-09-20 补正：本条落地时实际写的是「字符串索引」推导（`'〇一二…'.index(c)`），单字与「十X」可用但**「二十」及以上会解析成 0**；已改回显式字表并补单元测试（见下条）
3. 改写 `split_source(text, want_juan=None)`
   - 识别到多卷时先定位目标卷，**只比对目标卷正文**
   - **定位失败 → `sys.exit(2)` 报错退出**，不再静默回退到整文件
4. `--卷` 参数 `default=None`；新增 `--列出卷` 列出源文件内所有卷名

**复现（升级后重做）**：
```
① 读当前 verify_against_source.py，确认 split_source 是否仍为单卷版
② 按上述 4 点改造；中文数字 MAP 必须显式写全
③ 实测：多卷源文件 + --卷 N，确认相似度恢复 0.90+，卷次不符时退出码 2
```

**验证样本**（2026-09-18 实测）：
- 上册源 md 2 卷：识别正确，卷1 相似度 0.90+
- 下册源 md 8 卷：识别正确，逐卷相似度 0.90+
- 卷次不符：退出码 2，报错信息清晰

---

## 2026-09-20 · 写库脚本安全改造（`fix_arabic_numerals.py` / `gen_index.py`）

**背景**：批量跑 `--help` 做脚本可用性巡检时，两个**没有 `argparse`** 的脚本把 `--help` 当普通参数直接执行了 `main()`：
- `fix_arabic_numerals.py` 的 vault 取值为 `Path(os.environ.get("TNOTE_VAULT") or "").resolve()` —— 未设环境变量时解析成 **cwd**，当日 cwd 恰为技能目录，于是把技能自身的 `SKILL.md` / `workflow/` / `references/` 当题库改写（实测 4 文件 12 处）；
- `gen_index.py` 直接重建了题库六册索引（按配置写入真实 vault，内容幂等，但**若索引里曾手工补过课标题会被静默覆盖**）。

**改动**：

1. `fix_arabic_numerals.py`
   - vault 改用 `qb_schema.get_vault()`（环境变量 > skill_config.json > 报错），**删除 cwd 兜底**
   - 加 `argparse`：`--vault` / `--apply`；**默认 dry-run 只预览并列出逐文件改动处数**，`--apply` 才落盘
   - 单元替换**收窄范围**：`fix_md` → `scan_md(unit_scope=…)`，题库 md 只改 YAML frontmatter 内的 `单元:` 字段、词表只改行首 `- 第X单元` 行；**正文一律不动**
     （原全文 `re.subn(r"第([一二三四五六七八九十])单元", …)` 会误伤正文普通序数表达，实测把「世界近代史的第一单元」改成「第1单元」、归档报告 3 处同样中招）
2. `gen_index.py`：加 `argparse`（`--dry-run` / `--force`）；写入前检查现存文件中**不在词表内的 `##`/`###` 标题**（疑手工新增的课/组），检出即**中止该册**并提示并入词表或加 `--force`
3. `verify_against_source.py`：`match_juan` 的中文数字解析改回**显式字表** `cn2int()`（修上条记录的偏差），补 `二十`~`九十九` 支持

**复现（升级后重做）**：
```
① 确认三脚本均有 argparse（grep -l argparse scripts/*.py）
② fix_arabic_numerals.py 无参数跑 → 只打印预览、文件 mtime 不变；--apply 才有写入
③ gen_index.py --dry-run → 六册"与现状一致"；手工在某册索引加一个词表外的 ### 标题后重跑 → 该册中止
④ cn2int 单元测试：「二十」→20、「二十三」→23、「九十九」→99、「廿」→None
```

**验证样本**（2026-09-20 实测）：
- `fix_arabic_numerals.py`（默认）：仅报 1 个文件 31 处（册别替换），收窄前误报的 2 个文件 4 处已消除
- `gen_index.py --dry-run`：六册全部"与现状一致"，无词表外标题
- `cn2int`：15 个用例全通过；`match_juan('综合检测卷二十','检测卷20')` → True
