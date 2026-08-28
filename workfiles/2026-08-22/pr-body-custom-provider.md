## 问题

自定义 OpenAI 兼容服务商的接入流程目前"麻烦繁琐"，普通用户难以完成：

1. **前端缺创建入口**：添加服务商对话框只有官方推荐目录，没有自定义表单。想接入任意端点（自建 vLLM、硅基流动、各类中转）必须手改配置文件——对普通用户门槛过高。
2. **配置不生效需重启**：`[llm_profiles.<name>]` 只在启动时加载，新增或修改服务商必须重启网关，运维体验差。
3. **保存前拿不到模型列表**：现有模型获取接口要求服务商已存在于配置中，而用户在"创建前"点获取就直接报错；改为浏览器直连 `GET /models` 又会被 CORS 拦截（多数 OpenAI 兼容端点不返回 `Access-Control-Allow-Origin`），体验断裂。

上游已有关联上下文：#31（需求，needs-design 关闭）、#886（已合并，恢复自定义端点 Base URL 字段）、#912（open，自定义端点探测失败）。方向被官方认可，但接入链始终没补齐。

## 解决

**动态注册 + 前端表单双管齐下**：

- **前端**：新增自定义服务商创建表单（服务商 ID、显示名、API 地址、协议、密钥、模型列表），作为添加对话框的默认视图；官方推荐目录保留在下一层，默认路径不动。
- **动态注册**：`register_profile_providers` 让任何带 `base_url` 的 profile 在首次解析时自动注册，配置修改即时生效，**无需重启网关**；`boot.py` 启动时预注册全部 profile，`mutations.py` 保存服务商时即时注册。
- **服务端模型探测**：新增 `onboarding.customProvider.models.discover` RPC，由网关服务端发起 `GET {base_url}/models`（天然绕过浏览器 CORS），且不依赖服务商已注册——创建前即可获取模型列表。
- **Provider 级默认上下文窗口**：`context_window_tokens` 字段作为服务商层默认窗口注入模型目录。

## 体验优化

- **补全既有底座**：上游 #886 恢复了自定义端点的 Base URL 字段，本 PR 在其之上补完"创建表单 → 动态注册 → 保存前探测"整条链路，让自定义服务商从"半成品"变成开箱可用的完整功能。
- **开箱即用不被破坏**：懒用户仍走推荐服务商一键接入（TokenRhythm），极客用户多一条自定义路径——两条路互不干扰。
- **错误可诊断**：探测失败时返回可读错误（HTTP 状态、API key 提示、超时），不再静默失败。

## 验证

- `vue-tsc --noEmit` 通过
- WebUI setup 相关 708 个测试通过
- 后端 provider/onboarding 测试套件通过（既有 golden drift 与 Windows symlink 失败与本次改动无关）
- 手动验证：网关运行中通过真实 token 调用 `onboarding.customProvider.models.discover` 返回正常（HTTP 401 错误正确回传，200 返回模型列表）
