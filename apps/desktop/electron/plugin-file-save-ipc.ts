import { BrowserWindow, dialog, ipcMain } from 'electron'

import { savePluginFile } from './plugin-file-save'

export function registerPluginFileSaveIpc(options: {
  isTrustedWindow: (window: BrowserWindow) => boolean
  rendererUrl: () => string
}): void {
  ipcMain.handle('hermes:plugin:saveFile', async (event, request: unknown) => {
    const owner = BrowserWindow.fromWebContents(event.sender)
    const isAuthorized = () => {
      if (
        !owner || owner.isDestroyed() || event.sender.isDestroyed() ||
        owner.webContents !== event.sender || !options.isTrustedWindow(owner) ||
        !event.senderFrame || event.senderFrame !== event.sender.mainFrame
      ) {
        return false
      }
      // Compare the renderer document, not just its origin (file: origins are
      // all "null"). Route/query changes are ordinary host navigation.
      try {
        const actual = new URL(event.senderFrame.url)
        const expected = new URL(options.rendererUrl())
        actual.hash = expected.hash = ''
        actual.search = expected.search = ''
        return actual.href === expected.href
      } catch {
        return false
      }
    }
    if (!owner || !isAuthorized()) {
      throw new Error('File save sender is not authorized')
    }
    return savePluginFile(request, {
      isAuthorized,
      showSaveDialog: settings => dialog.showSaveDialog(owner, settings)
    })
  })
}
