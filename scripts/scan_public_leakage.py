#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获客智能分析 · 竞品测试模拟环境 —— 公开材料泄露扫描
=====================================================
用途：扫描"会作为竞品输入"的公开目录，检出**把本模拟环境的具体答案
     提前告诉竞品**的内容。

⚠️ 边界（刻意区分）：
  - **允许**：通用的业务概念、通用分析方法、本环境的机制设定
    （如"素材 CTR 随上线天数衰减""DID 需先验对照组干净度"这类方法论）
  - **禁止**：把上述方法与**本环境的具体结论**绑定
    （如"本环境某策略份额翻倍导致 CPS 上涨""本题答案是素材疲劳"）

公开目录 = README.md + 01_知识库/ + 02_表元数据/ + 03_模拟数据/ + 04_Benchmark测试材料/
非公开   = 98_独立审计/ + 99_GroundTruth_仅供验证/ + scripts/

运行：python3 scripts/scan_public_leakage.py
退出码：0 = 无泄露；1 = 发现泄露
"""

import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PUBLIC_DIRS = ["01_知识库", "02_表元数据", "03_模拟数据", "04_Benchmark测试材料"]
PUBLIC_FILES = ["README.md"]
SKIP_SUFFIX = (".csv",)          # 数据文件按字段名检查，不做全文关键词检查

# ------------------------------------------------------------
# 泄露规则：(编号, 说明, 正则, 严重度)
# ------------------------------------------------------------
RULES = [
    # ---- 结论性数值：直接给出 GroundTruth 的答案数字 ----
    ("L-01", "Showcase A 答案数值（CPS 终值/涨幅/贡献度）",
     r"0\.5275|0\.4663\s*[→\-]>|13\.14%|\+13\.1%|18\.8%|81\.2%|60\.9%|\+\s*0\.0531", "P0"),
    ("L-02", "Showcase B 答案数值",
     r"1\.2638%|−26\.0%|60\.76%|16\s*天空窗|0\.6659%|0\.9262%|2\.2331%", "P0"),
    ("L-03", "Showcase C 答案数值（标的指标/反事实结果）",
     r"0\.7163|0\.4647|31\.69%|31\.7%|7,241|43,248|86\.0%|8\.4%|−3\.16%|-3\.16%", "P0"),
    ("L-04", "Showcase D 答案数值（DID 结果）",
     r"\+45\.0%|38\.1%|29\.8%|29\.0%|\+36\.2%|\+35\.0%|DID\s*(规模|CPS|授信单产|安全过件率)", "P0"),

    # ---- 结论性措辞：把方法与本环境结论绑定 ----
    ("L-05", "点名 Showcase A 的主因",
     r"(消耗)?结构(变化|倾斜)(是|为|即)?.*(CPS|成本).*(主因|主要|上涨|第一原因)|腾讯RTA策略_A.{0,20}占比.{0,10}(15%|翻倍|30%)", "P0"),
    ("L-06", "点名 Showcase B 的主因",
     r"素材疲劳(是|为|即|识别)|\*\*素材疲劳识别\*\*|本题.*素材疲劳", "P0"),
    ("L-07", "点名 Showcase C 的结论",
     r"不要只看\s*CPS\s*高就|不应(直接)?关闭|该流量.*(不该|不应)关|剔除后.{0,20}(质量|规模).{0,10}(受损|下降)|该不该关.{0,20}(答案|结论)", "P0"),
    ("L-08", "点名 Showcase D 的结论 / 对照污染",
     r"对照组.{0,6}污染|对照组.{0,10}(不干净|被污染)|保持\+?小幅扩大", "P1"),

    # ---- 校验结论外泄 ----
    ("L-09", "把内部校验的因果结论写进公开材料",
     r"C5-[a-e].*(结构变化|素材\s*CTR\s*衰退|系数调整|规模跃升|回传比例短时波动)", "P0"),
    ("L-10", "把 Showcase 的答案写成考察点",
     r"考察点.*(素材疲劳|结构效应|对照组污染|反事实测算结果)", "P1"),

    # ---- 假字段歧义 ----
    ("L-11", "delivery 表仍把裸 `日预算` 作为本表字段描述",
     r"\|\s*\*\*`日预算`\*\*\s*\|", "P1"),

    # ---- 具体构造机制外泄（一旦公开，等于提示竞品"某题的前提不可信"） ----
    ("L-12", "公开材料泄露实验组/对照组的构造机制",
     r"固定份额|同源派生|由同一大盘|同一大盘按|构造性(的)?平行趋势|两组的?比率(天然)?接近|比值恒(为|等)?\s*1\.0",
     "P0"),
]

# ------------------------------------------------------------
# 按文件范围生效的规则：(编号, 说明, 文件正则, 行正则, 严重度)
# ------------------------------------------------------------
SCOPED_RULES = [
    ("L-13", "README 中讨论「平行趋势」——该词只应出现在通用方法论文档（07）与表元数据（strategy_experiment）中，不应在公开 README 里评价本环境的实验设计",
     r"^README\.md$", r"平行趋势", "P0"),
    ("L-14", "公开材料点名某题的构造缺陷或前提不成立",
     r".*", r"构造(产生|属性).{0,12}不是实证|前提(需要|需)自行检验|两支?组.{0,6}天然", "P0"),
]

# ------------------------------------------------------------
# 数据文件的字段名检查（delivery 表不得再出现裸 `日预算`）
# ------------------------------------------------------------
FIELD_RULES = [
    ("L-20", "ads_rta_delivery_daily.csv 含裸字段 `日预算`（应为 `均分预算参考值`）",
     r"ads_rta_delivery_daily\.csv", r"(^|,)日预算(,|$)"),
]


def iter_public_files():
    for d in PUBLIC_DIRS:
        full = os.path.join(BASE_DIR, d)
        if not os.path.isdir(full):
            continue
        for root, _, files in os.walk(full):
            for fn in sorted(files):
                if fn.endswith(SKIP_SUFFIX):
                    continue
                if fn.startswith("."):
                    continue
                yield os.path.join(root, fn)
    for fn in PUBLIC_FILES:
        p = os.path.join(BASE_DIR, fn)
        if os.path.isfile(p):
            yield p


def main():
    findings = []
    files = list(iter_public_files())
    for path in files:
        rel = os.path.relpath(path, BASE_DIR)
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        for cid, desc, pat, sev in RULES:
            rx = re.compile(pat)
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    findings.append((sev, cid, rel, i, desc, line.strip()[:110]))
        # 数据文件字段名检查
        for cid, desc, filepat, fldpat in FIELD_RULES:
            if re.search(filepat, rel):
                head = lines[0] if lines else ""
                if re.search(fldpat, head):
                    findings.append(("P1", cid, rel, 1, desc, head[:110]))
        # 按文件范围生效的规则
        for cid, desc, filepat, linepat, sev in SCOPED_RULES:
            if not re.search(filepat, rel):
                continue
            rx = re.compile(linepat)
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    findings.append((sev, cid, rel, i, desc, line.strip()[:110]))

    print("=" * 96)
    print("公开材料泄露扫描")
    print("=" * 96)
    print(f"扫描范围：{len(files)} 个公开文件（README + 01_知识库 + 02_表元数据 + 03_模拟数据 + 04_Benchmark测试材料）")
    print(f"规则数：{len(RULES)} 条关键词规则 + {len(FIELD_RULES)} 条字段名规则 + {len(SCOPED_RULES)} 条范围规则")
    print()
    if not findings:
        print("✅ 未发现泄露。")
        return 0
    findings.sort(key=lambda x: (x[0], x[2], x[3]))
    print(f"❌ 发现 {len(findings)} 处疑似泄露：\n")
    for sev, cid, rel, ln, desc, snippet in findings:
        print(f"  [{sev}] {cid} {rel}:{ln}")
        print(f"        规则：{desc}")
        print(f"        命中：{snippet}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
