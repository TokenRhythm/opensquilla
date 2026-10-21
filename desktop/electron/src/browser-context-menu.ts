import { app, clipboard, type ContextMenuParams, type MenuItemConstructorOptions, type WebContents } from 'electron'
import { popupTextContextMenu } from './text-context-menu.js'

export interface BrowserContextMenuActions {
  canOpen(url: string): boolean
  openRelated(url: string): void
  navigate(action: 'back' | 'forward' | 'reload'): void
  find(): void
}

/** Keep actions in the owning browser page and validate links before opening. */
export function browserContextMenuTemplate(
  contents: WebContents, params: ContextMenuParams, actions: BrowserContextMenuActions,
  chinese = false,
): MenuItemConstructorOptions[] {
  const label = (en: string, zh: string) => chinese ? zh : en
  const items: MenuItemConstructorOptions[] = []
  if (params.linkURL && actions.canOpen(params.linkURL)) {
    items.push({ label: label('Open link in new tab', '在新标签页打开链接'), click: () => actions.openRelated(params.linkURL) },
      { label: label('Copy link address', '复制链接地址'), click: () => clipboard.writeText(params.linkURL) })
  }
  if (params.mediaType === 'image') {
    items.push({ label: label('Copy image', '复制图片'), click: () => contents.copyImageAt(params.x, params.y) })
  }
  if (items.length) items.push({ type: 'separator' })
  if (params.isEditable) {
    items.push({ role: 'undo', enabled: params.editFlags.canUndo },
      { role: 'redo', enabled: params.editFlags.canRedo },
      { type: 'separator' },
      { role: 'cut', enabled: params.editFlags.canCut },
      { role: 'copy', enabled: params.editFlags.canCopy },
      { role: 'paste', enabled: params.editFlags.canPaste })
  } else if (params.selectionText) {
    items.push({ role: 'copy', enabled: params.editFlags.canCopy })
  }
  items.push({ role: 'selectAll' }, { type: 'separator' },
    { label: label('Back', '后退'), enabled: contents.navigationHistory.canGoBack(), click: () => actions.navigate('back') },
    { label: label('Forward', '前进'), enabled: contents.navigationHistory.canGoForward(), click: () => actions.navigate('forward') },
    { label: label('Reload', '刷新'), click: () => actions.navigate('reload') },
    { label: label('Find in page', '在页面中查找'), click: () => actions.find() })
  return items
}

export function showBrowserContextMenu(
  contents: WebContents, params: ContextMenuParams, actions: BrowserContextMenuActions,
): void {
  popupTextContextMenu(contents, params, browserContextMenuTemplate(contents, params, actions,
    app.getLocale().toLowerCase().startsWith('zh')))
}
