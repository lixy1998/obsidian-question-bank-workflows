# -*- coding: utf-8 -*-
"""拆分基线快照：用固定工具题库出全部产物，记录内容哈希（拆分前后比对用）。

用法：
    python tests/_snapshot.py save   <前缀>   # 生成并记录
    python tests/_snapshot.py check  <前缀>   # 重新生成并与记录比对

比对方式：
  - md / yaml 文件：整文件 sha256
  - docx：解开 zip，逐个部件算 sha256，**跳过时间戳部件**（core.xml / app.xml 中的
    dcterms:created 等会随生成时间变化），因此比对的是排版内容本身
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
PY = sys.executable
WORK = SKILL / "tests" / "_snap"          # 临时仓库目录（每次运行都重建）
BASE = SKILL / "tests" / "_baseline.json"  # 基线记录（放在 _snap 之外，不被重建清掉）


def rec(序号, 答案, 题干):
    return {
        "学段": "初中", "类别": "2中考模拟题", "卷名": "2026快照卷", "序号": 序号,
        "册别": ["7上"], "单元": ["第2单元 夏商周时期"], "课": ["第4课 夏商西周王朝的更替"],
        "小节": ["商朝的统治"], "知识点": ["青铜器"], "核心素养": ["史料实证-实物史料的价值"],
        "素养层次水平": ["史料实证-层次1-辨识可信史料"], "关键能力": ["A2-说明"],
        "题型": "选择题", "难度": "中", "分值": 2,
        "来源": ["2026快照卷", "模拟卷"], "年份": 2026, "考区": "河南",
        "答案": 答案, "解析": "快照用解析，用于比对渲染是否稳定。",
        "题干": 题干,
        "选项": ["精益求精的态度", "拼搏奉献的风貌", "集体协作的精神", "国家权力的强大"],
    }


def env(v):
    return {**os.environ, "TNOTE_VAULT": str(v), "PYTHONIOENCODING": "utf-8"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def docx_parts(path: Path) -> dict:
    """docx 各部件哈希；跳过时间戳相关部件。"""
    out = {}
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if name in ("docProps/core.xml", "docProps/app.xml"):
                continue
            data = z.read(name)
            # 压缩产生的顺序/时间戳不进内容哈希（只哈希解压后的字节）
            out[name] = hashlib.sha256(data).hexdigest()
    return out


def build_all(v: Path):
    """在临时库里产出全部产物，返回产物路径列表。"""
    for r in (rec("01", "C", "司母戊鼎是迄今世界上出土的最重的青铜器，它主要反映了(　　)"),
              rec("02", "B", "甲骨文主要出土于河南安阳，它主要反映了(　　)")):
        j = v / "_in.json"
        j.write_text(json.dumps([r], ensure_ascii=False), encoding="utf-8")
        subprocess.run([PY, str(SCRIPTS / "gen_problem.py"), str(j)],
                       capture_output=True, text=True, encoding="utf-8", env=env(v))
        j.unlink(missing_ok=True)

    mf = v / "清单.yaml"
    mf.write_text("卷名: 测试快照卷\n标题: 2026—2027学年 测试快照卷\n"
                  "说明:\n  - 满分：100分　　建议用时：45分钟\n"
                  "题目:\n  - 2026快照卷-01\n  - 2026快照卷-02\n", encoding="utf-8")

    jobs = [
        ("学生.md", ["--模式", "学生", "--输出"]),
        ("命题说明.md", ["--模式", "命题说明", "--输出"]),
        ("学生.docx", ["--模式", "学生", "--docx"]),
        ("命题说明.docx", ["--模式", "命题说明", "--docx"]),
        ("卷末答题卡.docx", ["--模式", "学生", "--答题卡位置", "卷末", "--docx"]),
    ]
    made = []
    for name, args in jobs:
        out = v / name
        cmd = [PY, str(SCRIPTS / "export_student_paper.py"), "--清单", str(mf)] + args + [str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env(v))
        if r.returncode != 0:
            print("  [失败] %s：%s" % (name, (r.stdout + r.stderr)[-300:]))
            continue
        if out.exists():
            made.append(out)
        else:
            print("  [警告] %s 未生成" % name)
    # 默认独立答题卡
    for p in v.glob("*_答题卡.docx"):
        if p not in made:
            made.append(p)
    return made


def snapshot(v: Path) -> dict:
    snap = {}
    for p in sorted(build_all(v)):
        if p.suffix.lower() == ".docx":
            snap[p.name] = {"kind": "docx", "parts": docx_parts(p)}
        else:
            snap[p.name] = {"kind": "file", "sha256": sha(p)}
    return snap


def fresh_vault() -> Path:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    v = WORK / "vault"
    for d in ("初中-2中考模拟题", "0-Template-模板", "初中-1中考真题", "初中-3阶段测试"):
        (v / d).mkdir(parents=True, exist_ok=True)
    return v


def main():
    import sys as _sys
    sys.path.insert(0, str(SCRIPTS))
    from qb_bootstrap import setup_output_encoding
    setup_output_encoding()          # 管道输出强制 UTF-8，避免诊断输出乱码
    action = _sys.argv[1] if len(_sys.argv) > 1 else "check"
    prefix = _sys.argv[2] if len(_sys.argv) > 2 else "base"
    ref_file = BASE

    # ⚠ 必须在 fresh_vault()（会清空 _snap）之前把基线读进内存
    ref = None
    if action != "save":
        if not ref_file.exists():
            print("✗ 基线不存在：%s（先跑 save）" % ref_file)
            return 2
        ref = json.loads(ref_file.read_text(encoding="utf-8"))

    v = fresh_vault()
    got = snapshot(v)
    print("产物数：%d" % len(got))
    for k in sorted(got):
        print("   ", k)

    if action == "save":
        ref_file.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
        print("已记录基线：%s" % ref_file)
        return 0

    problems = []
    for name in sorted(set(ref) | set(got)):
        if name not in got:
            problems.append("产物缺失：%s" % name)
            continue
        if name not in ref:
            problems.append("新增产物：%s" % name)
            continue
        a, b = ref[name], got[name]
        if a["kind"] != b["kind"]:
            problems.append("类型变化：%s" % name)
            continue
        if a["kind"] == "file":
            if a["sha256"] != b["sha256"]:
                problems.append("内容变化：%s" % name)
        else:
            pa, pb = a["parts"], b["parts"]
            for part in sorted(set(pa) | set(pb)):
                if part not in pb:
                    problems.append("%s 缺部件 %s" % (name, part))
                elif part not in pa:
                    problems.append("%s 多部件 %s" % (name, part))
                elif pa[part] != pb[part]:
                    problems.append("%s 部件内容变化 %s" % (name, part))
    if problems:
        print("\n✗ 与基线不一致（%d 项）：" % len(problems))
        for p in problems[:40]:
            print("   ", p)
        return 1
    print("\n✓ 与基线完全一致（%d 个产物）" % len(got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
