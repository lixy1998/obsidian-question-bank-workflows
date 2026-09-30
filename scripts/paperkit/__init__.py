# -*- coding: utf-8 -*-
"""paperkit — 组卷导出（export_student_paper.py）的内部模块包。

拆分目的：把 2200 余行的单文件按职责拆开，使
  · Word 原语与图片装箱计算可单独测试（原先藏在 render_docx 的嵌套闭包里）
  · 答题卡与正式卷共用同一套 Word 原语（原先 card_setrun/card_run/card_para 是复制品）
  · 各渲染器可独立阅读与修改，不再出现「同一处规则改两遍」

依赖方向单向：paths → manifest → {images, markup, slots} →
{sections, balance, checkup} → {md_render, spec, docx_kit} → docx_render → CLI。

对外的 CLI 仍是 scripts/export_student_paper.py（保留全部参数与默认值）。
"""
