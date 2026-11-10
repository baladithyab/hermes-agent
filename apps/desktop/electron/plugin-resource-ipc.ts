import { BrowserWindow, ipcMain } from 'electron'

import { parseResourceBridgeRequest } from './plugin-resource-presentation'
import type { PluginResourcePresentations, ResourceInvoker } from './plugin-resource-presentation'
import type { PluginResourceBridgeRequest } from './plugin-resource-contract'

export function registerPluginResourceIpc(options: {
  presentations: PluginResourcePresentations
  isTrustedWindow: (window: BrowserWindow) => boolean
  rendererUrl: () => string
  capture: (request: PluginResourceBridgeRequest, senderId: number) => Promise<ResourceInvoker>
}): void {
  const observed = new WeakSet<Electron.WebContents>()
  const documents = new WeakMap<Electron.WebContents, number>()
  const authorize = (event: Electron.IpcMainInvokeEvent): (() => boolean) => {
    const owner = BrowserWindow.fromWebContents(event.sender)
    const document = documents.get(event.sender) ?? 0
    const current = () => {
      if (document !== (documents.get(event.sender) ?? 0)) return false
      if (!owner || owner.isDestroyed() || event.sender.isDestroyed() || owner.webContents !== event.sender ||
          !options.isTrustedWindow(owner) || !event.senderFrame || event.senderFrame !== event.sender.mainFrame) return false
      try {
        const actual = new URL(event.senderFrame.url)
        const expected = new URL(options.rendererUrl())
        actual.hash = expected.hash = ''
        actual.search = expected.search = ''
        return actual.href === expected.href
      } catch { return false }
    }
    if (!current()) throw new Error('Resource sender is not authorized')
    if (!observed.has(event.sender)) {
      observed.add(event.sender)
      const retire = () => {
        documents.set(event.sender, (documents.get(event.sender) ?? 0) + 1)
        options.presentations.retireSender(event.sender.id)
      }
      event.sender.once('destroyed', retire)
      event.sender.on('render-process-gone', retire)
      event.sender.on('did-start-navigation', (_event, _url, sameDocument, mainFrame) => {
        if (mainFrame && !sameDocument) retire()
      })
    }
    return current
  }
  ipcMain.handle('hermes:plugin:resource:open', async (event, raw: unknown) => {
    const current = authorize(event)
    const request = parseResourceBridgeRequest(raw)
    try {
      const invoke = await options.capture(request, event.sender.id)
      if (!current()) throw new Error('RESOURCE_REVOKED')
      return options.presentations.open(request, event.sender.id, invoke, current)
    } catch {
      // Do not serialize captured credentials, provider URLs or query-bearing
      // backend exceptions through Electron's IPC exception formatter.
      throw new Error('RESOURCE_UNAVAILABLE')
    }
  })
  ipcMain.handle('hermes:plugin:resource:close', async (event, handleId: unknown) => {
    authorize(event)
    if (typeof handleId !== 'string' || !/^p-[a-f0-9]{48}$/.test(handleId)) throw new Error('RESOURCE_REQUEST_INVALID')
    try { await options.presentations.close(handleId, event.sender.id) }
    catch { throw new Error('RESOURCE_UNAVAILABLE') }
  })
}
