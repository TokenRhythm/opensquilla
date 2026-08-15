# LLMPET 表情包资源规范

每个表情包必须是一个独立、可热更新的资源包：

```text
assets/memes/
  catalog.json
  <meme-id>/
    visual.gif
    voice.mp3
```

运行中的 LLMPET 会轮询目录元数据；替换 GIF、MP3 或修改 `catalog.json`
后，下一次打开表情包页即可看到新版本，无需重启。媒体缓存版本使用文件内容
SHA-256 生成，避免“文件名没变、客户端仍显示旧图”。

## 新增检查单

1. `id` 使用小写英文、数字和连字符，并与目录名完全一致。
2. GIF 不超过 30 MB，MP3 不超过 10 MB；文件扩展名和真实格式必须一致。
3. 中、英、日三种 `label`、`description`、`reactionLabel` 和 `promptText`
   都要填写。Prompt 只保留在主进程，不会下发到渲染进程。
4. `prompt.version` 每次改变行为时递增。
5. `reaction` 只描述桌宠如何回应；持续工作反应放在 `reaction.work`。
6. 必须填写 `provenance`。如果还没有核清版权，明确写
   `"license": "unverified"` 和 `"commercialUse": false`，不要猜测作者或授权。
7. 运行 `node test/meme-actions.js` 和 `node test/i18n.js`。

## provenance 字段

```json
{
  "provenance": {
    "origin": "user-supplied",
    "creator": "unknown",
    "sourceUrl": null,
    "license": "unverified",
    "commercialUse": false,
    "notes": "说明素材如何取得，以及还缺什么授权证据"
  }
}
```

`license` 只接受：

- `cleared`：已有明确许可；
- `public-domain`：已有可靠依据确认属于公有领域；
- `unverified`：尚未核清。

这里记录的是事实状态，不是法律判断。没有证据时一律使用 `unverified`。
