# -*- coding: utf-8 -*-
"""paperkit.manifest — 组卷清单加载、条目归一与题号定位。

从 export_student_paper.py 拆出（纯搬运，逻辑未改）。
"""
import json
from pathlib import Path

from qb_schema import split_frontmatter, stage_roots, strip_comments

from paperkit.paths import norm_no, vol_name_of

try:
    import yaml
except ImportError:
    yaml = None


def load_manifest(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"[失败] 清单文件不存在：{path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("[失败] 读 YAML 清单需要 pyyaml（venv 已内置）")
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:  # 纯文本：一行一个 id，`#` 起注释
        items = [l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        data = {"卷名": path.stem, "题目": items}
    if data is None:
        raise SystemExit(f"[失败] 清单为空：{path}")
    if isinstance(data, list):                      # 兼容旧 selected.json
        data = {"卷名": path.stem, "题目": data}
    if not isinstance(data, dict):
        raise SystemExit(f"[失败] 清单顶层应为映射或数组，实为 {type(data).__name__}")
    return data


def norm_items(man: dict) -> list:
    """清单条目统一为 dict（str → {'id': …}）。"""
    raw = man.get("题目") or man.get("questions") or []
    if not isinstance(raw, list):
        raise SystemExit("[失败] 清单的「题目」应为列表")
    out = []
    for x in raw:
        if isinstance(x, str):
            out.append({"id": x.strip()})
        elif isinstance(x, dict):
            out.append(dict(x))
        else:
            raise SystemExit(f"[失败] 清单条目格式不支持：{x!r}")
    return out


def resolve_items(items: list, index: dict, strict: bool = True) -> tuple:
    """逐条定位题目文件并读出元数据 → (记录列表, 缺失列表)。

    定位优先级：条目自带「文件」→ id 索引 → 「卷名-题号」拆分兜底。
    """
    ok, miss = [], []
    for i, it in enumerate(items, 1):
        path, why = None, ""
        f = it.get("文件") or it.get("file")
        if f and Path(f).exists():
            path = Path(f)
        else:
            qid = str(it.get("id") or "").strip()
            if not qid:
                miss.append((i, "(缺 id)", "条目既无「id」也无有效「文件」"))
                continue
            path = index.get(qid)
            if path is None and "-" in qid:          # 兜底：卷名-题号
                vol, no = qid.rsplit("-", 1)
                for root in stage_roots():
                    for cand in root.glob(f"{vol}/*/*.md"):
                        if norm_no(cand.stem) == norm_no(no):
                            path = cand
                            break
                    if path:
                        break
                why = "（按 卷名-题号 兜底命中）" if path else ""
        if path is None or not path.exists():
            miss.append((i, it.get("id") or it.get("文件"), "题库中未找到该 id"))
            continue
        fm, body, err = split_frontmatter(path.read_text(encoding="utf-8"))
        ok.append({
            "序号": i, "id": it.get("id") or fm.get("id"),
            "path": path, "vol": vol_name_of(path),
            "fm": fm, "body": strip_comments(body),
            "分值": it.get("分值", it.get("score", fm.get("分值"))),
            "来源括注": it.get("来源", ""), "角度": it.get("角度", ""),
            "选项序": it.get("选项序") or it.get("order"),
            "括号": it.get("括号"),
            "备注": it.get("备注", ""), "yaml_err": err, "定位提示": why,
        })
    if miss and strict:
        print(f"[警告] {len(miss)} 条未定位：")
        for i, qid, why in miss:
            print(f"   第{i}条 {qid} —— {why}")
    return ok, miss
