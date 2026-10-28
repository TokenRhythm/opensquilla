# 本机 WebUI 账号预览

这个开发预览复用 Electron 的 TokenRhythm OAuth、加密账号存储及模型代理，并将公开的账号状态接入 WebUI。账号令牌和模型 Key 留在本机账户服务内；浏览器只接收展示状态和授权页地址。网关通过原有短期代理能力使用账号模型额度。

当前入口只在设置 `VITE_LOCAL_ACCOUNT_PREVIEW=1` 的 Vite 开发预览中出现。所有服务都绑定 `127.0.0.1`，请使用隔离的 `OPENSQUILLA_HOME`。需要先安装仓库 Python 虚拟环境、WebUI 和 Electron 开发依赖。

终端一：

```sh
export OPENSQUILLA_HOME=/private/tmp/opensquilla-web-account-preview
export OPENSQUILLA_WEB_ACCOUNT_UI_ORIGIN=http://127.0.0.1:5174
npm --prefix desktop/electron run preview:web-account
```

终端二：

```sh
VITE_LOCAL_ACCOUNT_PREVIEW=1 \
OPENSQUILLA_GATEWAY_URL=http://127.0.0.1:18891 \
OPENSQUILLA_WEB_ACCOUNT_URL=http://127.0.0.1:18892 \
npm --prefix opensquilla-webui run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

打开 `http://127.0.0.1:5174/control/settings/account`。点击“登录 TokenRhythm”会在新标签页启动网站授权；成功后返回 WebUI。模型服务里可以将 TokenRhythm 账号作为凭证来源，聊天、图片和视频沿用桌面版代理逻辑。关闭终端一会同时停止本机账户服务和其网关。

这个预览尚未作为独立 WebUI 的正式部署模式。真实网站账号授权、余额和计费流程需要在你自己的账号下验收。
