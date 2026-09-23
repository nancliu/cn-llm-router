"""复杂度口径校准（2026-09-23，一次性可复现脚本）。

背景：评测显示 108 个 golden cases 中 28 题处于相邻档边界，LLM 呈向中档收缩倾向。
依据三模型（CED/豆包/Qwen）多数判向 + 人工按新锚点定义终裁，校准 15 题的复杂度标注：
  低 = 单一步骤/模板化/无设计权衡
  中 = 多步骤但路径清晰/需一定设计或归纳
  高 = 系统性设计/多轮迭代/边界条件/正确性/归因
改动保留原值到行尾注释（可回滚、可追溯）；规则关键词表不受影响（复杂度不参与规则判类）。
"""
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden_cases.yaml"

CALIBRATIONS = {
    # 中→低：单步/教科书级
    "math-mid": "低",      # 分部积分一次
    "tr-mid": "低",        # 润色一段英文邮件语气
    "tr-mid-2": "低",      # 润色自我介绍
    "mm-mid-2": "低",      # 对比两张产品图差异
    "qa-mid-2": "低",      # 单步科普解释
    "qa-mid-3": "低",      # 单步检索问答
    # 低→中：多步骤/需归纳或设计
    "fe-low-3": "中",      # 响应式布局（多断点适配）
    "ld-low-3": "中",      # 50 页报告总结（长文归纳）
    "qa-low-3": "中",      # 区块链原理（多机制概念）
    "agent-low-3": "中",   # LLM 工具调用编排流程（编排设计）
    # 高→中：经典套路/单步描述
    "math-high-2": "中",   # 组合恒等式（经典套路证明）
    "mm-high-3": "中",     # 看图描述图表趋势异常
    # 中→高：系统性设计/归因
    "da-mid-2": "高",      # 留存下降归因（多步分析）
    "agent-mid-2": "高",   # 抓取-清洗-入库多步骤流程
    "agent-mid-3": "高",   # 多模型协作 agent 系统设计
}


def main() -> None:
    lines = GOLDEN.read_text(encoding="utf-8").splitlines(keepends=True)
    cur_id: str | None = None
    changed: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- id:"):
            cur_id = stripped.split(":", 1)[1].strip()
        if cur_id in CALIBRATIONS and stripped.startswith("complexity:"):
            new_val = CALIBRATIONS[cur_id]
            old_val = stripped.split(":", 1)[1].strip().split()[0]
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}complexity: {new_val} # 校准2026-09-23: 原={old_val}\n"
            changed.append(f"{cur_id}: {old_val} → {new_val}")
            cur_id = None  # 每 case 只改一次
    GOLDEN.write_text("".join(lines), encoding="utf-8")
    print(f"已校准 {len(changed)} 题：")
    for c in changed:
        print("  " + c)


if __name__ == "__main__":
    main()
