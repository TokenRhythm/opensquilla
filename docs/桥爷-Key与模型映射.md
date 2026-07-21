# 桥爷 · Key 与模型映射

> 最终方案（2026-07-21）。Key 只保存在 `~/.opensquilla/relay-keys.env`，不得写进项目配置、Git、聊天或日志。

## 当前实际启用

| Key | 通道 | 地址 | 当前用途 | 模型 |
|---|---|---|---|---|
| Key2 | a.99cy 中转 | `https://a.99cy.edu.kg/v1` | 图片、截图、图表问答 | `grok-4.5` |
| Key3 | a.99cy 中转 | `https://a.99cy.edu.kg/v1` | 最高难度文字任务 | `glm-5.2` |
| Key7 | TokenRhythm | `https://tokenrhythm.studio/v1` | 日常三档与四模型会诊 | 见下表 |

Key1、Key4、Key5、Key6 仍安全保存在本机，但不进入当前默认路由。

## 单模型智能路由（默认）

每个问题只调用一个模型；系统根据整次请求的类型和难度自动选择，并非只看关键词。

| 档位 | 适合任务 | 通道与模型 |
|---|---|---|
| c0 | 简单提取、短改写、低风险问答 | Key7 · `deepseek-v4-flash` |
| c1 | 日常工作、普通分析、常规写作 | Key7 · `deepseek-v4-pro` |
| c2 | 多步骤执行、代码工作、长内容综合 | Key7 · `kimi-k2.7-code` |
| c3 | 深度规划、严格审查、复杂推理 | Key3 · `glm-5.2` |
| image | 图片、截图、图表问答 | Key2 · `grok-4.5` |

## 多模型会诊（默认关闭）

重要问题时手动开启。一次会诊会产生 5 次模型调用：4 个模型分别作答，再由 1 个模型汇总，因此比日常模式更慢、更贵。

全部走 Key7 TokenRhythm：

1. `deepseek-v4-pro`
2. `glm-5.2`
3. `kimi-k2.7-code`
4. `qwen3.7-max`
5. `glm-5.2` 负责最终汇总

## Key7 可用模型清单

1. `deepseek-v4-flash`
2. `deepseek-v4-pro`
3. `glm-5`
4. `glm-5.1`
5. `minimax-m2.7`
6. `kimi-k2.5`
7. `kimi-k2.6`
8. `minimax-m2.5`
9. `mimo-v2.5-pro`
10. `qwen3.7-max`
11. `kimi-k2.7-code`
12. `glm-5.2`

## Key4 · Kimi 官方（当前备用）

- 地址：`https://api.kimi.com/coding/v1`
- 模型：`kimi-for-coding`、`kimi-for-coding-highspeed`、`k3`
- Coding Plan 要求 `temperature=1`
- 它不是 a.99cy 中转，不能使用中转地址。

## 已完成的真实验证

- c0：`deepseek-v4-flash`，成功
- c1：`deepseek-v4-pro`，成功
- c3：Key3 `glm-5.2`，成功
- 图片：Key2 `grok-4.5`，成功识别真实图片
- 会诊：4 个提案全部成功，`glm-5.2` 汇总成功
- c2 模型已在会诊中真实调用成功；单模型路由尚未专门构造请求强制命中 c2
