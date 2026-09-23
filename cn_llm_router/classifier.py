"""任务分类器（ADR-0002）。

LLM 结构化判类 → 规则关键词兜底 → 默认值兜底；三层均不静默（low_confidence + fallback_reason）。
分类模型 = 候选链按供应商可用性取第一个可用者。
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from .config import RouterConfig, key_available
from .data_loader import RouterData
from .types import COMPLEXITIES, DEFAULT_CATEGORY, DEFAULT_COMPLEXITY, Classification

logger = logging.getLogger("cn_llm_router.classifier")

# 规则关键词兜底表（category 名称 → 关键词；命中任一即定类，复杂度取默认"中"）
RULE_KEYWORDS: dict[str, list[str]] = {
    "程序编码": ["写代码", "写个函数", "写函数", "实现", "python", "go语言", "shell", "脚本", "算法", "代码片段", "补全代码", "生成代码", "重构"],
    "前端UX设计": ["前端", "html", "css", "布局", "页面", "网页", "ui", "ux", "样式", "组件", "hover", "深色主题", "登录页"],
    "文书撰写": ["周报", "月报", "纪要", "公文", "通知", "方案", "报告", "合同", "文书", "策划", "总结", "邮件正文"],
    "数据分析": ["数据分析", "分析", "统计", "透视", "sql", "图表", "可视化", "趋势", "指标", "销售数据", "订单量", "分析一下"],
    "数学推理": ["证明", "积分", "微分", "代数", "概率", "方程", "定理", "计算题", "推导", "求解", "计算"],
    "Agent编排": ["agent", "子任务", "拆解", "编排", "流程", "工具调用", "多步", "串联", "规划", "multi-agent"],
    "长文档处理": ["长文", "论文", "报告总结", "摘要", "提取条款", "归纳", "50页", "合同条款", "书籍"],
    "翻译润色": ["翻译", "润色", "英文", "中文", "英译", "译成", "措辞", "术语", "语法修改"],
    "多模态理解": ["图片", "图像", "识别图中", "ocr", "看图", "这张", "这张图", "产品图", "照片", "视频里的", "多模态"],
    "知识问答/检索": ["什么是", "是什么", "解释", "区别", "查一下", "介绍", "历史", "背景", "原因", "原理", "概念"],
    "创意生成": ["起名", "起名字", "名字", "想一个", "创意", "广告语", "文案", "头脑风暴", "故事开头", "开头", "点子", "取名"],
    "代码审查与调试": ["报错", "bug", "审查", "调试", "性能优化", "性能瓶颈", "语法错误", "优化一下", "为什么错", "error", "异常", "单元测试"],
}

DEFAULT_CATEGORY_KEYWORDS = ["知识问答/检索", "请问", "回答", "是什么"]


class Classifier:
    def __init__(
        self,
        data: RouterData,
        cfg: RouterConfig,
        debug: bool = False,
        llm_json: Optional[Callable[[str, str], dict]] = None,
    ):
        self.data = data
        self.cfg = cfg
        self.debug = debug
        # llm_json(prompt, model) -> dict；测试可注入 mock，默认走 openai 网关
        self._llm_json = llm_json or self._default_llm_json

    # ---------- 主入口 ----------
    def classify(self, prompt: str) -> Classification:
        if not prompt or not str(prompt).strip():
            return self._default_fallback("输入为空")
        text = str(prompt).strip()

        llm_res = self._try_llm(text)
        if llm_res is not None:
            return llm_res

        rule_res = self._rule_fallback(text)
        if rule_res is not None:
            return rule_res

        return self._default_fallback("LLM 与规则兜底均未命中")

    # ---------- LLM 判类 ----------
    def _try_llm(self, text: str) -> Optional[Classification]:
        for model in self.cfg.classifier_models:
            prov = self.cfg.providers.get(model)
            if prov is None or not key_available(prov):
                continue
            try:
                raw = self._llm_json(self._build_prompt(text), model)
                return self._parse(raw, model)
            except Exception as e:  # noqa: BLE001 —— 任何失败都进入降级链
                logger.debug("分类 LLM %s 失败: %s", model, e)
        return None

    def _build_prompt(self, text: str) -> str:
        lines = []
        for cat in self.data.categories.values():
            examples = "；".join(cat.examples[:2]) if cat.examples else ""
            lines.append(f"- {cat.name}：{cat.description}{('（示例：' + examples + '）') if examples else ''}")
        return (
            "你是任务分类器。把用户的请求归入下列 12 个类别之一，并判断复杂度。\n\n"
            "复杂度（低/中/高）判断锚点：\n"
            "- 低：单一步骤、模板化、无设计权衡（写简单函数、润色一段文字、查一个事实、单张图识别）\n"
            "- 中：多步骤但路径清晰、需一定设计或归纳（响应式适配、长文总结、概念解释、经典套路证明）\n"
            "- 高：系统性设计、多轮迭代、边界条件/正确性/归因（分布式系统、完整立项、留存归因、安全审查）\n"
            "- 边界判断：按“步骤数 + 设计深度 + 迭代轮次”中最强的信号定档，不要默认取中。\n\n"
            "类别定义：\n"
            + "\n".join(lines)
            + f'\n\n请求："{text}"\n\n只输出 JSON：{{"category": "类别名", "complexity": "低|中|高", '
            + '"confidence": 0-1, "second_guess": "次选类别名或空字符串"}}'
        )

    def _parse(self, raw: dict, model: str) -> Optional[Classification]:
        category = str(raw.get("category", "")).strip()
        complexity = str(raw.get("complexity", "")).strip()
        if category not in self.data.categories or complexity not in COMPLEXITIES:
            return None
        try:
            confidence = float(raw.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.5
        second = raw.get("second_guess") or None
        if second and second not in self.data.categories:
            second = None
        res = Classification(
            category=category,
            complexity=complexity,
            confidence=confidence,
            second_guess=second,
            low_confidence=confidence < 0.6,
        )
        if self.debug:
            res.raw = raw
        return res

    def _default_llm_json(self, prompt: str, model: str) -> dict:
        from .gateway import build_client

        prov = self.cfg.providers[model]
        client = build_client(prov)
        resp = client.chat.completions.create(
            model=prov.api_model,
            messages=[
                {"role": "system", "content": "你是严格按格式输出的 JSON 分类器。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=1024,  # 推理型模型（如 deepseek CED）思考链长，需保障输出空间
            timeout=self.cfg.classifier_timeout,
        )
        content = resp.choices[0].message.content
        return json.loads(content)

    # ---------- 规则兜底 ----------
    def _rule_fallback(self, text: str) -> Optional[Classification]:
        lowered = text.lower()
        hits: list[tuple[int, int, str]] = []  # (关键词长度, 出现位置, 类别)
        for cat, kws in RULE_KEYWORDS.items():
            for kw in kws:
                pos = lowered.find(kw)
                if pos >= 0:
                    hits.append((len(kw), pos, cat))
        if not hits:
            return None
        # 启发式：最长关键词（更具体）优先，其次最早出现（避免泛词误伤）
        _, _, best = min(hits, key=lambda h: (-h[0], h[1]))
        return Classification(
            category=best,
            complexity=DEFAULT_COMPLEXITY,
            confidence=0.4,
            low_confidence=True,
            fallback_reason="规则关键词兜底",
        )

    # ---------- 默认兜底 ----------
    def _default_fallback(self, reason: str) -> Classification:
        return Classification(
            category=DEFAULT_CATEGORY,
            complexity=DEFAULT_COMPLEXITY,
            confidence=0.2,
            low_confidence=True,
            fallback_reason=reason,
        )
