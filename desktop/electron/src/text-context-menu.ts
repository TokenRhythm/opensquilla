import { BrowserWindow, Menu, type ContextMenuParams, type MenuItemConstructorOptions, type WebContents } from 'electron'

function textContextMenuTemplate(params: ContextMenuParams): MenuItemConstructorOptions[] {
  if (params.isEditable) {
    return [
      { role: 'cut', enabled: params.editFlags.canCut },
      { role: 'copy', enabled: params.editFlags.canCopy },
      { role: 'paste', enabled: params.editFlags.canPaste },
      { type: 'separator' },
      { role: 'selectAll', enabled: params.editFlags.canSelectAll },
    ]
  }
  return params.selectionText ? [{ role: 'copy', enabled: params.editFlags.canCopy }] : []
}

export function popupTextContextMenu(
  contents: WebContents, params: ContextMenuParams, template: MenuItemConstructorOptions[],
): void {
  if (!template.length || contents.isDestroyed()) return
  const window = BrowserWindow.fromWebContents(contents)
  if (!window || window.isDestroyed()) return
  // A WebContentsView shares its window with the chat renderer. Keep editing
  // commands in the page that raised the menu, rather than the chat composer.
  if (!contents.isFocused()) contents.focus()
  Menu.buildFromTemplate(template).popup({ window, ...(params.frame ? { frame: params.frame } : {}) })
}

export function installTextContextMenu(contents: WebContents): void {
  contents.on('context-menu', (_event, params) => {
    popupTextContextMenu(contents, params, textContextMenuTemplate(params))
  })
}
