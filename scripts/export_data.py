#!/usr/bin/env python3
"""从飞书打分表导出的 JSON 快照生成 data/*.csv 与 data/categories.yaml。

用法: python3 scripts/export_data.py /path/to/sheet_export.json [data目录]
- 输入: `lark-cli sheets +table-get --url <打分表URL> --output-path <json>` 的产物
- 输出: data/models.csv / data/scores.csv / data/weights.csv / data/categories.yaml / data/VERSION
- 可编程复用: parse_sheets(doc) / write_data(data_dir, parsed)（同步脚本 sync_from_lark.py 复用本模块）
本脚本是 v1 的一次性导出工具 + 同步链路的解析/落盘层（ADR-0004：data/*.csv 为唯一真相源）。
"""
import csv
import json
import os
import sys

AS_OF = "2026-09-23"
VERSION = "v1-20260923"

CATEGORY_DEFS = [
    # id, name, 判类说明, examples
    ("programming", "程序编码", "编写、修改或生成程序代码：写函数/算法/脚本、实现功能、生成代码片段、补全代码。", ["帮我写一个Python函数解析JSON", "用Go实现一个LRU缓存", "生成一段批量重命名文件的shell脚本"]),
    ("frontend_ux", "前端UX设计", "网页/应用的界面设计与交互体验：HTML/CSS布局、组件样式、页面动效、UI设计、用户体验优化。", ["做一个登录页的HTML+CSS", "这个按钮的hover效果怎么写", "帮我把这个页面改成深色主题"]),
    ("document_writing", "文书撰写", "撰写正式文书与公文：通知、报告、方案、合同、纪要、公文格式的写作与排版。", ["写一份项目周报", "起草会议纪要模板", "写一份活动策划方案"]),
    ("data_analysis", "数据分析", "数据处理与统计分析：表格处理、聚合透视、指标计算、可视化图表、数据解读。", ["分析这份销售数据的趋势", "用SQL统计每日订单量", "给这些数字做个透视表"]),
    ("math_reasoning", "数学推理", "数学问题求解与严谨推理：代数、微积分、概率统计、逻辑证明、推导计算。", ["求解这个积分", "证明费马小定理", "计算这个概率题"]),
    ("agent_orchestration", "Agent编排", "多智能体/工具调用编排：规划子任务、拆解流程、调用工具、串联模型协作。", ["把这个任务拆成三个子agent执行", "帮我规划一个多步骤的数据抓取流程", "写一个工具调用链"]),
    ("long_doc", "长文档处理", "长文本的阅读、摘要、结构化提取：论文、报告、合同、书籍的理解与归纳。", ["总结这篇论文的核心观点", "提取这份合同的条款清单", "给这50页报告写个摘要"]),
    ("translation", "翻译润色", "跨语言翻译与文本润色：中英互译、语气调整、表述优化、术语统一。", ["把这段中文翻译成英文", "帮我润色这段英文邮件", "翻译并优化这条产品描述"]),
    ("multimodal", "多模态理解", "图像/音频/视频等多模态内容的识别、理解与描述：看图说话、OCR、图片问答。", ["识别这张图片里的文字", "描述这张图的内容", "这个产品图里的瑕疵在哪里"]),
    ("qa_retrieval", "知识问答/检索", "知识问答与信息检索：百科问题、概念解释、资料查找、基于知识的回答。", ["解释什么是贝叶斯定理", "查一下2026年诺贝尔奖得主", "量子计算和经典计算的区别"]),
    ("creative", "创意生成", "创意内容创作：文案、故事、点子、命名、营销创意、头脑风暴。", ["给新奶茶店起10个名字", "写一个悬疑短篇的开头", "为新品想一句广告语"]),
    ("code_review", "代码审查与调试", "代码审查与调试：找bug、解释报错、审查代码质量、优化性能、补测试。", ["这段代码为什么报错", "审查这个PR的代码质量", "帮我的函数做性能优化"]),
]

COMPLEXITY_LEVELS = ["低", "中", "高"]

DIMENSIONS = ["数学推理", "幻觉控制", "科学推理", "精确指令遵循", "Agentic编程", "Agent任务规划", "Terminal编程", "SuperCLUE总分"]

OUTPUT_FILES = ["models.csv", "scores.csv", "weights.csv", "categories.yaml", "VERSION"]


def cell_num(v):
    """把单元格值转 float；非数值（待补充/N/A/空/—）返回 None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "待补充", "N/A", "N/A ", "—", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ffill(rows):
    """能力评分矩阵的任务类别列为合并单元格：向下填充。"""
    out = []
    last = None
    for r in rows:
        if r[0] and str(r[0]).strip():
            last = str(r[0]).strip()
        out.append([last] + r[1:])
    return out


def parse_sheets(doc: dict) -> dict:
    """解析 +table-get 快照（{complete, sheets:[{name, columns, data, dtypes, range}]}）为结构化数据。

    返回 {models, scores, weights, categories}；任何结构断言失败直接抛异常（校验即失败）。
    """
    sheets = {s["name"]: s for s in doc["sheets"]}

    # ---------- models ----------
    reg = sheets["模型注册表"]
    rows = reg["data"]
    header = [str(c).strip() for c in rows[0]]
    assert header[2] == "型号" and "输入单价" in header[7] and "输出单价" in header[8], header
    models = []
    for r in rows[1:]:
        if not r or not str(r[0]).strip():
            continue
        logical = str(r[2]).strip()
        models.append({
            "logical_name": logical,
            "vendor": str(r[1]).strip(),
            "version": str(r[3]).strip(),
            "open_source": str(r[4]).strip(),
            "license": str(r[5]).strip(),
            "context_window": str(r[6]).strip(),
            "price_in": cell_num(r[7]),
            "price_out": cell_num(r[8]),
            "cache_price": cell_num(r[9]),
            "platform": str(r[10]).strip(),
            "architecture": str(r[12]).strip(),
            "source_url": str(r[13]).strip(),
            "as_of": AS_OF,
        })
    assert len(models) >= 10, f"模型数异常: {len(models)}"

    # ---------- scores（长表） ----------
    mtx = sheets["能力评分矩阵"]
    mrows = mtx["data"]
    mcols = [str(c).strip() for c in mrows[0]]
    assert mcols[0] == "任务类别" and mcols[1] == "复杂度", mcols
    model_cols = mcols[2:-1]
    rows = ffill(mrows[1:])
    scores = []
    seen = set()
    for r in rows:
        if not r or not str(r[0]).strip():
            continue
        cat = str(r[0]).strip()
        cx = str(r[1]).strip()
        basis = str(r[-1]).strip() if r[-1] else ""
        for i, m in enumerate(model_cols, start=2):
            key = (cat, cx, m)
            if key in seen:
                continue
            seen.add(key)
            sc = cell_num(r[i])
            scores.append({
                "category": cat,
                "complexity": cx,
                "model": m,
                "score": "" if sc is None else sc,
                "basis": basis,
                "as_of": AS_OF,
            })
    n_cat_cx = len({(s["category"], s["complexity"]) for s in scores})
    assert n_cat_cx == 36, f"类别×复杂度格数异常: {n_cat_cx}"

    # ---------- weights（区块A：数据第1~12行） ----------
    syn = sheets["综合与性价比"]
    srows = syn["data"]
    scols = [str(c).strip() for c in srows[0]]
    assert scols[0] == "任务类别", scols
    weights = []
    for r in srows[1:13]:
        cat = str(r[0]).strip()
        for j, dim in enumerate(DIMENSIONS, start=1):
            raw = str(r[j]).strip() if j < len(r) and r[j] is not None else ""
            if raw == "直接用VLM分":
                w, note = 0.0, "直接用VLM分（不参与维度加权）"
            else:
                v = cell_num(raw)
                w, note = (0.0 if v is None else v), ""
            weights.append({"category": cat, "dimension": dim, "weight": w, "note": note, "as_of": AS_OF})
    n_cats = len({x["category"] for x in weights})
    assert n_cats == 12, f"权重类别数异常: {n_cats}"
    assert len(weights) == 96, f"权重行数异常: {len(weights)}"

    # ---------- categories ----------
    categories = [
        {"id": cid, "name": name, "description": desc, "examples": ex}
        for cid, name, desc, ex in CATEGORY_DEFS
    ]
    return {"models": models, "scores": scores, "weights": weights, "categories": categories}


def write_data(data_dir: str, parsed: dict) -> None:
    """把 parse_sheets 结果落盘为 data/*.csv + categories.yaml + VERSION。"""
    os.makedirs(data_dir, exist_ok=True)

    models = parsed["models"]
    with open(os.path.join(data_dir, "models.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(models[0].keys()))
        w.writeheader()
        w.writerows(models)

    scores = parsed["scores"]
    with open(os.path.join(data_dir, "scores.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category", "complexity", "model", "score", "basis", "as_of"])
        w.writeheader()
        w.writerows(scores)

    weights = parsed["weights"]
    with open(os.path.join(data_dir, "weights.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category", "dimension", "weight", "note", "as_of"])
        w.writeheader()
        w.writerows(weights)

    import yaml
    with open(os.path.join(data_dir, "categories.yaml"), "w", encoding="utf-8") as f:
        f.write(f"# 任务类别唯一事实源（ADR 词汇表：类别定义）\n# as_of: {AS_OF}\n")
        yaml.safe_dump({"categories": parsed["categories"]}, f, allow_unicode=True, sort_keys=False)

    with open(os.path.join(data_dir, "VERSION"), "w", encoding="utf-8") as f:
        f.write(f"{VERSION}\nas_of: {AS_OF}\nsource: 国内大模型选择器打分表 v1-20260923（飞书）\n")


def main(json_path, data_dir):
    with open(json_path, encoding="utf-8") as f:
        doc = json.load(f)
    parsed = parse_sheets(doc)
    write_data(data_dir, parsed)
    n_score = sum(1 for s in parsed["scores"] if s["score"] != "")
    print(f"models.csv: {len(parsed['models'])} 行")
    print(f"scores.csv: {len(parsed['scores'])} 行（其中有效分值 {n_score}）")
    print(f"weights.csv: {len(parsed['weights'])} 行（12 类别 × 8 维）")
    print(f"categories.yaml: {len(parsed['categories'])} 类")
    print("done ->", data_dir)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python3 scripts/export_data.py <sheet_export.json> [data目录]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "data"))
