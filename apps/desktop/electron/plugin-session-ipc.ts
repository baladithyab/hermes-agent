import { randomBytes } from 'node:crypto'
import { BrowserWindow, ipcMain } from 'electron'
import { pluginSelectionHeaders } from './plugin-selection'
import type { PluginSelectionToken } from './plugin-selection'
import { pluginSessionFailure } from './plugin-session-contract'
import type { PluginSessionOpen, PluginSessionCall, PluginSessionReply } from './plugin-session-contract'

export type CapturedPluginTransport = (request: PluginSessionCall) => Promise<unknown>
interface CapturedSession { senderId: number; pluginId: string; invoke: CapturedPluginTransport; selection?: PluginSelectionToken }

export function registerPluginSessionIpc(options: {
  isTrustedWindow: (window: BrowserWindow) => boolean
  rendererUrl: () => string
  capture: (request: PluginSessionOpen) => Promise<CapturedPluginTransport>
}): { transport: (handle: string, senderId: number, pluginId: string) => CapturedPluginTransport } {
  const sessions = new Map<string, CapturedSession>()
  const observed = new WeakSet<Electron.WebContents>()
  const documents = new WeakMap<Electron.WebContents, number>()
  let opening = 0
  const authorize = (event: Electron.IpcMainInvokeEvent): void => {
    const window = BrowserWindow.fromWebContents(event.sender)
    if (!window || window.isDestroyed() || !options.isTrustedWindow(window) ||
        !event.senderFrame || event.senderFrame !== event.sender.mainFrame) throw new Error('PLUGIN_SESSION_DENIED')
    const actual = new URL(event.senderFrame.url)
    const expected = new URL(options.rendererUrl())
    actual.hash = expected.hash = ''
    actual.search = expected.search = ''
    if (actual.href !== expected.href) throw new Error('PLUGIN_SESSION_DENIED')
    if (!observed.has(event.sender)) {
      observed.add(event.sender)
      const retire = () => {
        documents.set(event.sender, (documents.get(event.sender) ?? 0) + 1)
        for (const [key, session] of sessions) {
          if (session.senderId !== event.sender.id) continue
          sessions.delete(key)
          if (session.selection) void session.invoke({ path: '/_host/deselection', method: 'POST',
            selection: session.selection, timeoutMs: 9000 }).catch(() => undefined)
        }
      }
      event.sender.once('destroyed', retire)
      event.sender.on('render-process-gone', retire)
      event.sender.on('did-start-navigation', (_event, _url, sameDocument, mainFrame) => { if (mainFrame && !sameDocument) retire() })
    }
  }
  const transport = (handle: string, senderId: number, pluginId: string): CapturedPluginTransport => {
    const session = sessions.get(handle)
    if (!session || session.senderId !== senderId || session.pluginId !== pluginId) throw new Error('STALE_SCOPE')
    return session.invoke
  }
  ipcMain.handle('hermes:plugin:session:open', async (event, request: PluginSessionOpen) => {
    authorize(event)
    if (!request || typeof request.pluginId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(request.pluginId) || !request.owner) {
      throw new Error('PLUGIN_SESSION_DENIED')
    }
    for (const value of [request.owner.connectionId, request.owner.profile]) {
      if (value !== null && (typeof value !== 'string' || !value || value.length > 128 || /[\/\\\u0000-\u001f]/.test(value))) {
        throw new Error('PLUGIN_SESSION_DENIED')
      }
    }
    if (sessions.size + opening >= 64) throw new Error('PLUGIN_SESSION_BUSY')
    const document = documents.get(event.sender) ?? 0
    opening += 1
    try {
      const invoke = await options.capture(request)
      authorize(event)
      if (document !== (documents.get(event.sender) ?? 0)) throw new Error('STALE_SCOPE')
      const handle = randomBytes(24).toString('hex')
      sessions.set(handle, { senderId: event.sender.id, pluginId: request.pluginId, invoke })
      return handle
    } catch {
      throw new Error('PLUGIN_SESSION_UNAVAILABLE')
    } finally { opening -= 1 }
  })
  ipcMain.handle('hermes:plugin:session:call', async (event, handle: string, request: PluginSessionCall): Promise<PluginSessionReply> => {
    authorize(event)
    const session = sessions.get(handle)
    if (!session || session.senderId !== event.sender.id) throw new Error('STALE_SCOPE')
    if (!request || typeof request.path !== 'string' || !request.path.startsWith('/') || request.path.startsWith('//') ||
        /[\\#%\u0000-\u001f]/.test(request.path) || request.path.split('?')[0].split('/').some(part => part === '.' || part === '..')) {
      throw new Error('PLUGIN_SESSION_DENIED')
    }
    pluginSelectionHeaders(`/api/plugins/${session.pluginId}${request.path}`, request.selection)
    const parsed = new URL(request.path, 'https://plugin.invalid')
    parsed.searchParams.delete('profile')
    const path = parsed.pathname + parsed.search
    try {
      const value = await session.invoke({ ...request, path })
      if (path === '/_host/selection' && value && typeof value === 'object' &&
          'scope_id' in value && typeof value.scope_id === 'string' && /^[a-f0-9]{32}$/.test(value.scope_id) &&
          'epoch' in value && typeof value.epoch === 'number' && Number.isSafeInteger(value.epoch)) {
        session.selection = { client: request.selection.client, scope: value.scope_id, epoch: value.epoch }
        if (sessions.get(handle) !== session) {
          await session.invoke({ path: '/_host/deselection', method: 'POST', selection: session.selection, timeoutMs: 9000 })
        }
      } else if (path === '/_host/deselection') session.selection = undefined
      return { ok: true, value }
    } catch (error) { return pluginSessionFailure(error) }
  })
  ipcMain.handle('hermes:plugin:session:close', async (event, handle: string) => {
    authorize(event)
    const session = sessions.get(handle)
    if (session && session.senderId !== event.sender.id) throw new Error('STALE_SCOPE')
    sessions.delete(handle)
    if (session?.selection) {
      try { await session.invoke({ path: '/_host/deselection', method: 'POST', selection: session.selection, timeoutMs: 9000 }) }
      catch { throw new Error('PLUGIN_SESSION_UNAVAILABLE') }
    }
  })
  return { transport }
}
