# -*- coding: utf-8 -*-
"""qb_bootstrap.py — 所有脚本共用的启动引导（依赖预检 / 输出编码 / vault 定位）。

设计目标（2026-09-30 立，修 P0 三项）：
1. **依赖预检**：缺 pyyaml 等依赖时给出「怎么装」的中文提示，不吐裸 traceback。
2. **输出编码**：管道输出（AI/重定向读取）强制 UTF-8，避免 Windows 控制台 GBK
   把中文参数名（`--单元`/`--卷`/`--口径`）显示成 `--��Ԫ` 导致误读；
   真实控制台（isatty）不动，保证用户在 cmd/PowerShell 里看到的中文正常。
3. **vault 定位**：统一入口 `resolve_vault()`（--vault > TNOTE_VAULT > skill_config.json），
   取代此前散在 fix_image_formats / verify_against_source 里的两套重复实现。

用法（各脚本在进入 main 后调用）：

    from qb_bootstrap import bootstrap, require_vault
    bootstrap()                        # ① 编码 + 依赖预检（唯一必做项）
    bootstrap(need=["docx"])           # 额外要求 python-docx
    vault = require_vault(args.vault)  # ② 确实要读写题库时取库路径（保证目录存在）

**取库路径一律用 `require_vault()`，不要写 `bootstrap(vault=…)` 取路径**——`bootstrap()`
默认 `want_vault=False`，传了 `vault` 也返回 `None`（再 `Path(None)` 直接 TypeError）。
只有「仅解析路径、不要求目录存在」的少数场景才用 `bootstrap(vault=…, want_vault=True)`。

**推荐骨架**（`require_vault` 必须排在参数校验**之后**，理由见其 docstring）：

    args = ap.parse_args()
    bootstrap()                        # 依赖预检 + 管道 UTF-8
    ...自行校验参数（白名单 / 互斥 / 必填）...
    vault = require_vault(args.vault)  # 最后才取库路径

未配置 vault 时以退出码 2 结束并打印中文引导，不再抛 RuntimeError。
"""
from __future__ import annotations

import sys

# 依赖包名 → （import 名，pip 名，用途）
# key 一律用**脚本里实际 import 的名字**（PyMuPDF 新版 import 名是 `pymupdf`、旧版是 `fitz`，两者都列）
DEPS = {
    "yaml": ("yaml", "pyyaml", "核心流程必需（YAML frontmatter 解析）"),
    "docx": ("docx", "python-docx", "链路B：docx 直读（read_docx.py）"),
    "fitz": ("fitz", "pymupdf", "PDF 裁剪补图（pull_pdf_images.py；PyMuPDF 旧导入名）"),
    "pymupdf": ("pymupdf", "pymupdf", "PDF 裁剪补图（pull_pdf_images.py；PyMuPDF 新导入名）"),
    "pptx": ("pptx", "python-pptx", "PPT 提取（可选）"),
    "cv2": ("cv2", "opencv-python", "图片格式转换读图（fix_image_formats.py）"),
    "numpy": ("numpy", "numpy", "图片像素处理（fix_image_formats.py）"),
    "PIL": ("PIL", "Pillow", "图片保存与 dpi（fix_image_formats.py）"),
}

_INSTALL_HINT = (
    "请安装依赖后重试：\n"
    "  pip install -r <skill>/requirements.txt        # 一次装齐\n"
    "  pip install pyyaml                             # 只装核心\n"
    "受管环境（WorkBuddy/DSH）可用自带解释器：\n"
    "  <受管 venv>/python.exe <脚本>   # 通常已含 pyyaml/python-docx/pymupdf/numpy/cv2/Pillow"
)


def setup_output_encoding() -> None:
    """管道输出强制 UTF-8；真实控制台保持原编码（不制造新的乱码）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def check_deps(need=("yaml",)) -> None:
    """预检依赖；缺什么就报什么，退出码 3。"""
    import importlib

    missing = []
    for key in need:
        mod, pip_name, why = DEPS.get(key, (key, key, ""))
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append((pip_name, why))
    if not missing:
        return
    print("[失败] 缺少 Python 依赖：", file=sys.stderr)
    for pip_name, why in missing:
        print(f"  · {pip_name:<16} {why}", file=sys.stderr)
    print(_INSTALL_HINT, file=sys.stderr)
    raise SystemExit(3)


def lazy_import(key, hint=""):
    """模块级可选依赖的安全导入：缺失时给中文提示 + 退出码 3。

    为什么需要它：若脚本在**模块顶层** `import docx` / `import numpy` / `import pymupdf`，
    缺包时会在 `main()` 执行之前就抛裸 traceback，`bootstrap()` 的预检根本来不及生效。
    把顶层导入改成本函数即可（返回模块对象）：

        docx = lazy_import("docx")
        np = lazy_import("numpy")
    """
    import importlib

    mod, pip_name, why = DEPS.get(key, (key, key, ""))
    try:
        return importlib.import_module(mod)
    except ImportError:
        print(f"[失败] 缺少 Python 依赖：{pip_name}"
              + (f"（{hint or why}）" if (hint or why) else ""), file=sys.stderr)
        print(_INSTALL_HINT, file=sys.stderr)
        raise SystemExit(3)


def try_import(key):
    """静默尝试导入（不打印、不退出），失败返回 None。用于「同一库有多个候选导入名」场景。"""
    import importlib

    mod, _pip, _why = DEPS.get(key, (key, key, ""))
    try:
        return importlib.import_module(mod)
    except ImportError:
        return None


def resolve_vault(cli_vault=None):
    """vault 定位统一入口：--vault > 环境变量 TNOTE_VAULT > scripts/skill_config.json。

    返回值保证为绝对路径字符串；未配置时打印中文引导并 raise SystemExit(2)。
    """
    from pathlib import Path

    if cli_vault:
        return str(Path(cli_vault).expanduser())
    import qb_schema  # 延迟导入：避免与 qb_schema 的模块级依赖形成环

    try:
        return qb_schema.get_vault()
    except RuntimeError as e:
        print(f"[失败] {e}", file=sys.stderr)
        print("提示：可用 --vault <路径> 临时指定，或跑 python scripts/setup.py --vault <路径> --init",
              file=sys.stderr)
        raise SystemExit(2)


def require_vault(cli_vault=None) -> "Path":
    """取 vault 目录（Path）。未配置 / 目录不存在时给中文引导 + 退出码 2。

    与 `resolve_vault` 的区别：本函数**保证目录存在**，供"确实要读写题库"的脚本在
    动作前调用（巡检、统计、导出、合并…）。绝不能放在模块顶层——那会让未配置时的
    `--help` 也失败（2026-09-30 前 `qb_schema` 的 `VAULT` 顶层求值就是这个毛病）。

    **调用位置：`ap.parse_args()` 之后，但在自己的参数校验之后**（2026-09-30 实测定案）。
    顺序反了会掩盖真正的报错：未配置 vault 的环境下，`--去重 XX` 这类「字段不在白名单」
    的参数报错会先被本函数的 exit=2 盖掉，用户看到的提示与真实病因无关（`edit_fields.py`
    的白名单测试正是踩在这个坑上暴露的）。
    """
    from pathlib import Path

    p = Path(resolve_vault(cli_vault))
    if not p.is_dir():
        print(f"[失败] 题库库目录不存在：{p}", file=sys.stderr)
        print("提示：确认路径，或跑 python scripts/setup.py --vault <路径> --init 重新配置/建库",
              file=sys.stderr)
        raise SystemExit(2)
    return p


def bootstrap(need=("yaml",), vault=None, want_vault=False):
    """脚本入口引导。返回解析出的 vault 字符串（**仅** `want_vault=True` 时），否则 `None`。

    ⚠ 要取库路径请用 `require_vault()`（保证目录存在并排在参数校验之后）；本函数只在
    「仅解析路径、不要求目录存在」的场景才加 `want_vault=True`，见模块 docstring 的推荐骨架。
    """
    setup_output_encoding()
    check_deps(need)
    if want_vault:
        return resolve_vault(vault)
    return None
