# -*- coding: utf-8 -*-
"""setup.py — Obsidian历史题库搭建与组卷 首次配置引导（写入 skill 内 skill_config.json）。

用法：
  python setup.py --vault <路径> [--init]   # 写入配置；--init 顺便建库（六类别目录+模板）
  python setup.py --init                    # 用当前已有配置建库（未配置则报错）
  python setup.py --show                    # 显示当前配置
  python setup.py                           # 交互式：询问 vault 路径（可新建）

说明：
- 配置文件 scripts/skill_config.json 已加入 .gitignore，不随仓库发布，个人路径不会泄漏
- 优先级：环境变量 TNOTE_VAULT > 本配置文件（qb_schema.get_vault()）
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from qb_bootstrap import bootstrap

SCRIPTS = Path(__file__).parent
SKILL_ROOT = SCRIPTS.parent
CONFIG_FILE = SCRIPTS / "skill_config.json"
TEMPLATES_DIR = SKILL_ROOT / "templates"
CATEGORIES = ["初中-1中考真题", "初中-2中考模拟题", "初中-3阶段测试",
              "高中-1高考真题", "高中-2高考模拟题", "高中-3阶段测试"]


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_config(vault: str) -> None:
    CONFIG_FILE.write_text(
        json.dumps({"vault": str(vault).rstrip("/\\")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[配置] 已写入 {CONFIG_FILE}")


def init_vault(vault: str) -> None:
    """建库：六类别目录 + 功能目录（Inbox/1-MoC-索引/2-Evidence-依据/3-Research-研究/0-Template）+ 复制模板。"""
    root = Path(vault)
    root.mkdir(parents=True, exist_ok=True)
    for c in CATEGORIES:
        (root / c).mkdir(parents=True, exist_ok=True)
    # 功能目录（空目录也建，保证链路有落点；题根中的 案例学习 仍按需手工建）
    for d in ("Inbox-收集", "1-MoC-索引", "2-Evidence-依据", "3-Research-研究", "0-Template-模板"):
        (root / d).mkdir(parents=True, exist_ok=True)
    tpl_dir = root / "0-Template-模板"   # 与 qb_schema.TEMPLATE_DIR 同名
    tpl_dir.mkdir(parents=True, exist_ok=True)
    tpls = sorted(TEMPLATES_DIR.glob("*.md")) if TEMPLATES_DIR.exists() else []
    if not tpls:
        print(f"[建库] ⚠ 未找到技能内模板目录 {TEMPLATES_DIR}，跳过模板复制")
    for src in tpls:
        dst = tpl_dir / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            print(f"[建库] 模板已复制到 {dst}")
        else:
            print(f"[建库] 模板已存在，跳过：{dst}")
    print(f"[建库] 完成：{root}（六类别目录 + 功能目录 + 模板）")


def interactive() -> None:
    print("=== Obsidian历史题库搭建与组卷 首次配置 ===")
    print("请提供题库库（Obsidian vault）根目录路径；")
    print("没有现成题库库的话，可以直接给一个想新建的路径，我会自动建库。")
    vault = input("vault 路径（回车取消）: ").strip().strip('"').strip("'")
    if not vault:
        print("已取消。")
        return
    write_config(vault)
    ans = input(f"现在建库（创建六类别目录+模板）？[Y/n] ").strip().lower()
    if ans != "n":
        init_vault(vault)
    print("配置完成。重新执行录入/组卷命令即可开始使用。")


def main():
    bootstrap(need=())
    ap = argparse.ArgumentParser(description="Obsidian历史题库搭建与组卷 首次配置")
    ap.add_argument("--vault", help="题库库根目录路径（写入配置）")
    ap.add_argument("--init", action="store_true", help="建库（六类别目录+模板）")
    ap.add_argument("--show", action="store_true", help="显示当前配置")
    args = ap.parse_args()

    if args.show:
        cfg = load_config()
        if cfg.get("vault"):
            print(f"当前 vault：{cfg['vault']}")
        else:
            print("尚未配置（可用 --vault <路径> 配置）")
        return

    if args.vault:
        write_config(args.vault)
        if args.init:
            init_vault(args.vault)
        return

    if args.init:
        cfg = load_config()
        if not cfg.get("vault"):
            print("未配置 vault，请先：python setup.py --vault <路径> 或直接运行 setup.py 交互配置")
            sys.exit(1)
        init_vault(cfg["vault"])
        return

    interactive()


if __name__ == "__main__":
    main()
