# -*- coding: utf-8 -*-
"""paperkit.docx_kit — Word 通用原语与图片装箱计算。

## 为什么有这个小模块

原 `render_docx`（499 行）把 11 个辅助函数定义成**嵌套闭包**，其中
`setrun/run/para` 与模块级 `card_setrun/card_run/card_para` 是同一套逻辑的两份复制品，
而 `_ar/_fit/_auto_single_w/_auto_right_w` 这 4 个**纯计算**函数因位于闭包内无法单测。

本模块把这两类下沉到模块级：
  · 图片装箱计算的参数显式化（传入 `img_width_mm` 与本模块常量），可直接单测
  · Word 段落/字体原语提供 **同一套签名**，正式卷与答题卡共用（原先各写一份）

⚠ 渲染行为不变：所有宽度常量与判定阈值原值保留（2026-09-26 用户裁定的
「宽幅图铺满版心、普通图回落基准宽」规则一字未改）。
"""

# Word 布局常量（原为 render_docx 内的局部常量，原值搬移）
VW_MM = 168.0            # 版心宽（A4 210 − 左右各 21mm）
IMG_GAP = 6.0            # 并排图之间的水平间距（规范 §10.2：5~8mm）
MAX_W = 155.0            # 单图可铺的最大宽度（版心留 13mm 余量）
BASE_W = 78.0            # 单图基准宽（非宽幅图，沿旧默认值）
RIGHT_W = 55.0           # §10.1 靠右环绕的单图宽（约版心 1/3）
RIGHT_MAX_W = 80.0       # 靠右环绕时宽幅图的上限（再宽就没有"绕左"空间了）
PT_MM = 2.8346           # 1mm ≈ 2.8346pt（图文混排的垂直/水平补偿换算）
WIDE_AR = 2.6            # 「宽幅图」判定阈值（年代尺/时间轴类，宽高比 ≥2.6）

# relativeHeight 递增，避免多图 z 序相同（相当于原闭包里的 `_rel_h = [2]`）
_rel_h = [2]


def next_rel_h() -> int:
    """取下一个 relativeHeight（浮动图 z 序）。"""
    _rel_h[0] += 1
    return _rel_h[0]


def aspect_ratio(src) -> float:
    """图片宽高比（宽/高）；读不到时按 1.5 估算。"""
    try:
        from PIL import Image
        with Image.open(src) as im:
            return (im.width / im.height) if im.height else 1.5
    except Exception:
        return 1.5


def fit_width(src, max_w: float, max_h: float) -> float:
    """在 max_w × max_h 的框内等比装箱，返回可用宽度（mm）。"""
    ar = aspect_ratio(src)
    w = float(max_w)
    if ar > 0 and w / ar > max_h:
        w = max_h * ar
    return max(10.0, w)


def auto_single_w(src, max_h: float, img_width_mm: float = 0.0) -> float:
    """单图宽度：**宽幅图铺到 MAX_W**，普通/高窄图回落到基准宽（并按高度上限收缩）。

    依据规范 §10.1（单图约版心 1/3–1/2）与用户 2026-09-26 裁定：
    嵌入型改为上下型环绕后，宽幅图（年代尺、箭头示意、时间轴，宽高比 ≥2.6）
    不再受"嵌入行"限制，可铺满版心；普通方图保持基准宽，避免占版过度。
    """
    if img_width_mm:                        # 命令行显式给了固定宽 → 一律照此
        return fit_width(src, img_width_mm, max_h)
    ar = aspect_ratio(src)
    w = min(MAX_W, max_h * ar)
    if ar < WIDE_AR:
        w = min(w, BASE_W)
    return max(10.0, w)


def auto_right_w(src, max_h: float = 60.0, img_width_mm: float = 0.0) -> float:
    """§10.1 靠右环绕的单图宽：正文绕左，故图不能占满版心。

    普通图取 RIGHT_W（≈版心 1/3）；宽幅图放宽到 RIGHT_MAX_W；高窄图按高度上限反算。
    `--图片宽` 显式给值则一律照此。
    """
    if img_width_mm:
        return fit_width(src, img_width_mm, max_h)
    ar = aspect_ratio(src)
    w = min(RIGHT_MAX_W, max_h * ar) if ar >= WIDE_AR else min(RIGHT_W, max_h * ar)
    return max(20.0, w)


# ---------------------------------------------------------------------------
# 待办：Word 段落/字体原语尚未统一
#
# 现状：`render_docx` 内的嵌套 `setrun/para/run` 与模块级 `card_setrun/card_run/
# card_para` 仍是**两份复制品**，改字号/字体规则要改两处。
#
# 为什么这次没合并：python-docx 写属性的**顺序与取值**（`space_before` 是否写、
# `keep_together` 写 False 还是省略、`w:ind` 是否出现）都会进 `word/document.xml`，
# 实测合并后答题卡与正式卷的 XML 都发生变化——即版面会被无意改动。
# 合并需要逐个属性对齐原实现，并用 `tests/_snapshot.py` 逐部件比对验收。
#
# 合并时的正确做法（已探明）：
#   · `space_before`：正式卷路径**总是写**（含 0）；答题卡路径**不写**
#   · `left_indent`：正式卷路径总是写（含 0）；答题卡路径不写
#   · `keep_together`：正式卷写 True/False；答题卡不写
#   · 属性写入顺序须与各自原实现一致，否则 XML 字节序变化
# ---------------------------------------------------------------------------


