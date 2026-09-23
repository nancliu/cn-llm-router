# Domain Docs

工程技能在探索代码库时如何消费本仓库的领域文档。

## 探索前先读

- 仓库根 **`CONTEXT.md`**，或
- 仓库根 **`CONTEXT-MAP.md`**（若存在）：指向各上下文自己的 `CONTEXT.md`，按主题读相关的每一份；
- **`docs/adr/`**：读与你即将工作的区域相关的 ADR。多上下文仓库还检查 `src/<context>/docs/adr/`。

以上文件不存在时**静默继续**，不标记缺失、不主动建议创建。`/domain-modeling` 技能（经 `/grill-with-docs` 与 `/improve-codebase-architecture` 触达）会在术语或决策真正落地时惰性创建。

## 文件结构

单上下文仓库（大多数仓库）：

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-xxx.md
│   └── 0002-yyy.md
└── src/
```

多上下文仓库（存在根 `CONTEXT-MAP.md` 时）：

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← 上下文内决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 使用词汇表用语

输出命名领域概念（issue 标题、重构提案、假设、测试名）时，使用 `CONTEXT.md` 中定义的说法，不要漂移到词汇表明确回避的同义词。

需要的概念不在词汇表里，是一个信号：要么你在发明项目不用的语言（重新考虑），要么存在真实缺口（记给 `/domain-modeling`）。

## 标注 ADR 冲突

输出与已有 ADR 矛盾时，明确指出来而不是静默覆盖：

> _Contradicts ADR-0001（评分口径与选择策略），但值得重开，因为……_
