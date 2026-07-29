# B6: 中文意图识别覆盖 — 系统性调研报告

> **Status**: 调研 v1.0（待 KunYu 确认后出实施方案）
> **Date**: 2026-07-29
> **Scope**: L2 `constraint_routing.py` 中 `classify_query_intent()` 的多语言覆盖
> **Trigger**: 当前 `_INTENT_PATTERNS` 包含中英双语关键词，但覆盖不系统

---

## 1. 当前实现分析

文件：`src/opensquilla/memory/constraint_routing.py` (lines 34-89)

**4 种 intent + 默认 general，正则匹配，first-match-wins**：

| Intent | 英文关键词 | 中文关键词 | 置信度 |
|--------|-----------|-----------|--------|
| `avoid_failure` | problem, error, wrong, bug, fail, crash, broken, issue | 问题, 错误, 失败, 崩溃, 故障, 异常, 报错 | 0.7 |
| `continue_task` | continue, resume, next step, pick up, where (was/are) we | 继续, 接着, 上次, 接下来, 下一步, 接着做 | 0.7 |
| `retrieve_rationale` | why, reason, rationale, because, explain | 为什么, 原因, 怎么回事, 为何, 理由 | 0.7 |
| `transfer_knowledge` | similar, like before, same as, analogous, experience | 类似, 有没有经验, 以前, 之前做过, 同样 | 0.6 |
| `general` | *(default catch-all)* | — | 0.5 |

### 1.1 已知覆盖缺口

**中文常见表达但当前未覆盖**：

| 用户可能的中文查询 | 当前匹配 | 应匹配 | 缺失关键词 |
|-------------------|---------|--------|-----------|
| "上次做到哪了" | continue_task ✅ | — | — |
| "恢复之前的任务" | general ❌ | continue_task | "恢复" |
| "还有哪里没做完" | general ❌ | continue_task | "没做完" |
| "接着干" | continue_task ✅ | — | — |
| "参考之前的做法" | general ❌ | transfer_knowledge | "参考" |
| "借鉴一下" | general ❌ | transfer_knowledge | "借鉴" |
| "我们当时选了哪个方案" | general ❌ | retrieve_rationale | "选了哪个" |
| "之前的决策依据是什么" | general ❌ | retrieve_rationale | "决策依据" |
| "这个怎么修" | general ❌ | avoid_failure | "怎么修" |
| "搞不定" | general ❌ | avoid_failure | "搞不定" |
| "出错了" | general ❌ | avoid_failure | "出错了" |
| "排查一下" | general ❌ | avoid_failure | "排查" |

### 1.2 否定检测缺口

当前 regex 用 `re.search()` 匹配子串，以下情况会误匹配：
- "没有问题" → 匹配到 `问题` → `avoid_failure`（应为 general）
- "不用继续" → 匹配到 `继续` → `continue_task`（应为 general）
- "没有错误" → 匹配到 `错误` → `avoid_failure`（应为 general）

### 1.3 中文特殊性

- **无空格切分**：中文词语连续，regex 子串匹配可能产生歧义
- **口语化变体**："咋回事"、"啥情况"、"怎么肥四"
- **中英夹杂**："这个 bug 怎么 fix"、"continue 刚才的任务"
- **简繁体**：繼續 / 報錯 / 怎麼辦（港台用户）
- **同义词无穷**："上次" / "上回" / "上一次" / "之前" / "刚才"

---

## 2. 方法论调研

### 2.1 方法谱系

| 方法 | 代表 | 中文效果 | 延迟 | 依赖 | 可控性 | 适用场景 |
|------|------|---------|------|------|--------|---------|
| **正则关键词**（当前） | 本方案 | 受限于词表覆盖 | <1ms | 零 | 高（确定性） | 精确匹配、高优先级意图 |
| **TF-IDF + 分类器** | Rasa DIET (pre-BERT) | 中等 | ~10ms | jieba + sklearn | 中 | 有标注数据 |
| **BERT 微调** | bert-base-chinese + softmax | 优秀 | ~50ms | torch + 模型 | 低（黑箱） | 大量标注数据 + GPU |
| **Cross-lingual embedding** | multilingual-E5 / BGE-M3 | 优秀 | ~30ms | sentence-transformers | 中 | 零样本跨语言 |
| **LLM zero-shot** | GPT/Claude prompt | 优秀 | ~500ms | API | 低（成本高） | 快速原型、低 QPS |
| **混合方案** | 快路径(正则) + 慢路径(embedding/LLM) | 优秀 | <1ms ~ ~30ms | 可选 | 中-高 | 生产环境平衡 |

### 2.2 关键研究发现

**来源 1**：Cureus Journal 综述 (2025-10)
- LLM prompting + instruction tuning 在多意图和多语言基准上超越传统 hard prompts
- 模板工程 + context-aware embeddings 对歧义查询有显著提升

**来源 2**：ACM TMIS — Multilingual Intent Discovery (2024)
- 多语言文本嵌入可分解为语言特定组件 + 语言无关语义组件
- 网络深度（而非词汇重叠）是跨语言迁移的关键因素
- mBERT 在中文 NLU 任务上表现取决于网络深度

**来源 3**：multilingual-E5 技术报告 (arXiv 2402.05672)
- mE5-large (1024D) 在中文检索任务上显著优于 mDPR
- 三个尺寸可选：small (384D) / base (768D) / large (1024D)
- 支持零样本跨语言：英文训练 → 中文推理
- MTEB 中文检索 benchmark 上 mE5 超越所有之前的多语言模型

**来源 4**：Label Your Data 综述 (2025-07)
- 规则系统适合紧 scoped 领域 + 快速原型
- Transformers 需要标注数据 + GPU
- LLM 零样本但运行成本高
- 混合方案（规则 + ML fallback）是生产环境最佳实践

### 2.3 Embedding 模型候选

| 模型 | 大小 | 维度 | 中文支持 | 跨语言 | 许可证 |
|------|------|------|---------|--------|--------|
| `intfloat/multilingual-e5-small` | ~118M params / ~235MB | 384 | ✅ 强 | ✅ | MIT |
| `intfloat/multilingual-e5-base` | ~278M / ~550MB | 768 | ✅ 强 | ✅ | MIT |
| `BAAI/bge-small-zh-v1.5` | ~33M / ~100MB | 512 | ✅ 强 | ❌ 仅中文 | MIT |
| `BAAI/bge-m3` | ~568M / ~2.3GB | 1024 | ✅ 强 | ✅ | MIT |
| `nomic-embed-text-v2-moe` | ~137M | 768 | ⚠️ 中 | ✅ | Apache 2.0 |

**推荐**（如果引入 embedding）：`multilingual-e5-small`（平衡大小和中文质量，MIT 许可证，384D 维度易于存储和计算）

---

## 3. 开源项目多语言策略

### 3.1 Agent Memory 项目

| 项目 | 多语言意图分类 | 中文策略 | 可借鉴点 |
|------|-------------|---------|---------|
| **Mem0** | ❌ 无意图分类 | 纯语义向量检索，依赖 embedding 多语言能力 | 无显式 intent |
| **Letta** | ❌ 无意图分类 | Agent 自主决策，靠 LLM 黑箱 | 无 |
| **Cognee** | ❌ 无意图分类 | LLM 提取实体（LLM 自身多语言） | 无 |
| **Zep** | ❌ 无意图分类 | 1024D 语义嵌入，不显式做 intent | 无 |
| **Rasa** | ✅ DIET + BERT | jieba 分词 → BERT → intent+entity 联合 | pipeline 可配置 |

**核心发现**：没有任何开源 agent memory 项目显式做 query intent classification 的中文适配——它们要么不分类（Mem0/Cognee/Zep），要么靠 LLM 黑箱（Letta）。我们的正则方案是业界唯一的轻量级显式 intent router。

### 3.2 中文 NLU 开源项目

| 项目 | 方法 | 语言 | 许可证 | 借鉴 |
|------|------|------|--------|------|
| `taishan1994/pytorch_bert_intent_classification` | BERT 微调 | 中文 | MIT | 中文 intent 分类参考架构 |
| `lhr0909/rasa-v2-nlu-bert-chinese` | Rasa + BERT Chinese | 中文 | MIT | Rasa pipeline 中文配置 |
| CLUE Benchmark | 多模型评估 | 中文 | 各项目 | 中文 NLU 评估数据集 |
| Rasa DIET | 关键词 + 模型混合 | 多语言 | Apache 2.0 | 双层 fallback 设计 |

### 3.3 Rasa 的混合策略（最接近我们）

Rasa 的中文意图识别 pipeline：
```
JiebaTokenizer → RegexFeaturizer → LexicalSyntacticFeaturizer → DIETClassifier
                                                                    ↓
                                                        KeywordIntentClassifier (fallback)
```

**借鉴**：
- ✅ 关键词 + 模型混合的双层设计
- ✅ `JiebaTokenizer` 的分词预处理（我们已有 `_needs_jieba_segmentation` 和 `_segment_for_fts`）
- ✅ RegexFeaturizer 作为特征输入（不是唯一判据）
- ✅ 置信度阈值 + fallback 链

---

## 4. 方案评估

### 4.1 方案 A：扩展正则词表（最小改动）

**思路**：补充缺失的中文关键词变体到现有正则

**改动量**：~20 行代码，新增 ~40 个中文关键词

**优点**：
- 零依赖、零延迟增加
- 完全可解释、可审计
- 向后兼容

**缺点**：
- 覆盖率有天花板——中文表达变体无穷
- 无法处理语义相似但词表未覆盖的查询
- 维护成本随时间递增

**适合**：v0.7 阶段快速改进，作为 baseline

### 4.2 方案 B：正则快路径 + embedding 慢路径（分层）

**思路**：
```
query
  ↓
正则匹配（<1ms）──→ 命中 ──→ 直接返回 intent
  ↓ 未命中
embedding 相似度匹配（~30ms）──→ 返回 intent
  ↓ 置信度 < 阈值
返回 general
```

**embedding 匹配方式**：
- 预计算 5 种 intent 的原型向量（每种 3-5 个示例查询的 embedding 平均值）
- 实时计算 query embedding → cosine 相似度 → argmax
- 阈值 > 0.75 → 采纳；否则 general

**优点**：
- 语义级覆盖，不依赖词表
- 中英文统一处理
- 快路径保持零延迟

**缺点**：
- 引入 embedding 模型依赖（~235MB 磁盘 + ~500MB 内存）
- 首次加载延迟（~2s 模型加载）
- 增加架构复杂度

**适合**：v0.8+ 如果零依赖约束放松

### 4.3 方案 C：正则 + LLM zero-shot fallback

**思路**：
```
query
  ↓
正则匹配 ──→ 命中 ──→ 直接返回
  ↓ 未命中
LLM zero-shot prompt ──→ 返回 intent
  ↓ LLM 不可用/超时
返回 general
```

**优点**：
- 最高准确率
- 无额外模型依赖（复用已有 LLM provider）
- 中英文统一

**缺点**：
- 延迟 ~500ms（LLM 调用）
- 成本：每次未命中查询消耗 ~100 tokens
- 依赖 LLM 可用性

**适合**：如果查询 QPS 低且 LLM 延迟可接受

### 4.4 方案 D：混合方案（A + C 组合，推荐评估）

**思路**：
```
query
  ↓
① 正则快路径（扩展词表）
  ↓ 未命中
② LLM zero-shot fallback（如果可用）
  ↓ LLM 不可用
③ 返回 general
```

**优点**：
- 快路径覆盖高频场景（零延迟）
- 慢路径覆盖长尾场景（高准确率）
- 无额外模型文件依赖
- LLM 不可用时优雅降级
- 复用 A1 的 `LlmCallFn` 注入模式

**缺点**：
- 仍有 LLM 延迟（但仅对正则未命中的查询）

---

## 5. 建议路径

### 5.1 短期（v0.7 — 本次 PR 后）

**执行方案 A（扩展正则词表）**：

1. 补充中文关键词变体（见 §6 附录）
2. 补充英文变体（如 "how come", "what's the reason", "fix", "debug"）
3. 添加否定检测（`_has_negation()` 函数）
4. 添加测试用例覆盖新增关键词 + 否定表达 + 中英混合
5. 不引入额外依赖

**预估改动量**：~60 行代码 + ~80 行测试。

### 5.2 中期（v0.8 — 后续迭代）

**评估方案 D（混合方案）**：

1. 在 v0.7 正则方案基础上，收集未命中 general intent 的查询日志
2. 评估 LLM fallback 的延迟和成本是否可接受
3. 如果 QPS 低（< 10/s），LLM fallback 成本可忽略
4. 实现时复用 A1 的 `LlmCallFn` 注入模式

### 5.3 长期（v0.9+ — 如果需要）

**评估方案 B（embedding 分层）**：

1. 如果零依赖约束放松（或 `multilingual-e5-small` 可作为可选依赖）
2. 预计算 intent 原型向量 + 实时 cosine 相似度
3. 实现 zero-shot 跨语言 intent 匹配
4. 缓存 query embedding 避免重复计算

---

## 6. 附录：中文关键词扩展草案

### 6.1 avoid_failure 补充

当前：`问题|错误|失败|崩溃|故障|异常|报错`

建议新增：
- `出错了`、`不对`、`不正常`、`没法用`、`不能用`
- `卡住`、`死锁`、`超时`、`挂了`、`挂掉`
- `怎么修`、`如何解决`、`搞不定`、`修不了`
- `排查`、`调试`、`排错`、`修复`
- `warning`（中文场景常用英文）

### 6.2 continue_task 补充

当前：`继续|接着|上次|接下来|下一步|接着做`

建议新增：
- `恢复`、`接着干`、`继续做`、`还没做完`、`没做完`
- `上次做到`、`之前到哪了`、`停在`、`做到哪了`
- `接着上次的`、`从上次停的地方`、`接着来`
- `回到刚才`、`继续弄`、`继续刚才`

### 6.3 retrieve_rationale 补充

当前：`为什么|原因|怎么回事|为何|理由`

建议新增：
- `决策依据`、`选了哪个`、`当时为什么`、`选择的理由`
- `解释一下`、`说明一下`、`怎么理解`
- `为啥`、`怎么会`、`什么道理`、`依据`
- `什么意思`、`什么情况`、`怎么解释`

### 6.4 transfer_knowledge 补充

当前：`类似|有没有经验|以前|之前做过|同样`

建议新增：
- `参考`、`借鉴`、`照着`、`沿用`
- `有没有类似的`、`相同场景`、`一样的`
- `上次也是`、`以前也遇到过`、`有没有先例`
- `同类`、`相仿`、`类似的方案`

### 6.5 否定检测设计

```python
_NEGATION_PREFIX_RE = re.compile(
    r"(不|没|别|非|未|无|not|no|don't|never|nothing|without)\s*$"
)

def _has_negation_before(text: str, match_start: int) -> bool:
    """Check if there's a negation word immediately before the keyword match."""
    prefix = text[max(0, match_start - 5):match_start]
    return bool(_NEGATION_PREFIX_RE.search(prefix))
```

---

## 7. 评估方法（后续）

### 7.1 评估数据集

建议从真实查询中构建：
```jsonl
{"query": "继续上次的工作", "intent": "continue_task"}
{"query": "这个 bug 怎么修", "intent": "avoid_failure"}
{"query": "为什么选了这个方案", "intent": "retrieve_rationale"}
{"query": "有没有类似的经验", "intent": "transfer_knowledge"}
{"query": "没有问题", "intent": "general"}
```

### 7.2 评估指标

| 指标 | 说明 |
|------|------|
| 准确率 (Accuracy) | 正确分类比例 |
| 召回率 (Recall) | 每个 intent 被正确识别的比例 |
| 精确率 (Precision) | 每个 intent 预测正确的比例 |
| 误报率 | 非目标 intent 被错误分类的比例（否定表达） |

### 7.3 影子模式

- 新分类器并行运行，记录但不影响结果
- 对比旧版本和新版本的指标差异
- 逐步放量

---

## 8. 待确认设计决策

| # | 问题 | KunYu 决定 |
|---|------|-----------|
| 1 | v0.7 是否执行方案 A（扩展正则词表 + 否定检测）？ | 待确认 |
| 2 | v0.8 是否评估方案 D（LLM fallback）？ | 待确认 |
| 3 | 零依赖约束是否允许引入 `multilingual-e5-small` 作为可选依赖？ | 待确认 |
| 4 | 是否需要收集 intent 未命中日志用于后续评估？ | 待确认 |
| 5 | 是否需要区分简体中文/繁体中文？ | 待确认 |
| 6 | 当前 5 种 intent 是否满足需求？是否需要新增？ | 待确认 |

---

## 9. 参考资源

| 资源 | 链接 | 说明 |
|------|------|------|
| Multilingual E5 论文 | arxiv.org/abs/2402.05672 | 多语言 embedding SOTA，MIT |
| BGE-M3 | huggingface.co/BAAI/bge-m3 | 多语言 + 多粒度 embedding，MIT |
| Rasa NLU | rasa.com | 开源对话 NLU，关键词 + DIET 混合 |
| CLUE Benchmark | cluebenchmarks.com | 中文 NLU 基准 |
| Mem0 论文 | arxiv.org/abs/2504.19413 | 无启发式门控，全 LLM 驱动 |
| MTEB Benchmark | huggingface.co/mteb | 多语言 embedding 评估 |
| ACM Multilingual Intent | dl.acm.org/doi/10.1145/3688400 | LLM 多语言意图发现 |
| bert-base-chinese | huggingface.co/google-bert/bert-base-chinese | 中文 BERT 基座 |
