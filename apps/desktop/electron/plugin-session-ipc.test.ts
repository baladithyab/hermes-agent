// @vitest-environment node
import { BrowserWindow, ipcMain } from 'electron'
import type { IpcMainInvokeEvent } from 'electron'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { pluginSessionFailure } from './plugin-session-contract'
import type { PluginSessionCall } from './plugin-session-contract'
import { registerPluginSessionIpc } from './plugin-session-ipc'

vi.mock('electron', () => ({ BrowserWindow: { fromWebContents: vi.fn() }, ipcMain: { handle: vi.fn() } }))
beforeEach(() => vi.clearAllMocks())

function setup() {
  const frame = { url: 'file:///app/index.html#chat' }
  const sender = { id: 7, mainFrame: frame, isDestroyed: () => false, once: vi.fn(), on: vi.fn() }
  const owner = { webContents: sender, isDestroyed: () => false }
  vi.mocked(BrowserWindow.fromWebContents).mockReturnValue(owner as unknown as BrowserWindow)

  const invoke = vi.fn(async (request: PluginSessionCall) => request.path === '/_host/selection'
    ? { scope_id: 'a'.repeat(32), epoch: 1 } : { captured: 'old-backend', path: request.path })

  const capture = vi.fn(async () => invoke)
  const registry = registerPluginSessionIpc({ isTrustedWindow: () => true, rendererUrl: () => 'file:///app/index.html', capture })
  const handler = (channel: string) => vi.mocked(ipcMain.handle).mock.calls.find(([name]) => name === channel)![1]

  return { sender, frame, capture, invoke, registry, event: { sender, senderFrame: frame } as unknown as IpcMainInvokeEvent,
    open: handler('hermes:plugin:session:open'), call: handler('hermes:plugin:session:call'), close: handler('hermes:plugin:session:close') }
}

const request = { pluginId: 'hermes-cpa', owner: { connectionId: 'old-backend', profile: 'alpha' } }
const selection = { client: 'client-with-sixteen-characters', scope: 'a'.repeat(32), epoch: 1 }

describe('captured plugin transport IPC', () => {
  it('captures once, removes caller profile overrides and closes terminally', async () => {
    const h = setup()
    const handle = await h.open(h.event, request)
    await expect(h.call(h.event, handle, { path: '/console/lock?profile=other&x=1', method: 'POST', selection })).resolves.toEqual({
      ok: true, value: { captured: 'old-backend', path: '/console/lock?x=1' }
    })
    expect(h.capture).toHaveBeenCalledExactlyOnceWith(request)
    expect(h.registry.transport(handle, 7, 'hermes-cpa')).toBe(h.invoke)
    expect(() => h.registry.transport(handle, 8, 'hermes-cpa')).toThrow('STALE_SCOPE')
    expect(() => h.registry.transport(handle, 7, 'other')).toThrow('STALE_SCOPE')
    await h.close(h.event, handle)
    await expect(h.call(h.event, handle, { path: '/console/lock', selection })).rejects.toThrow('STALE_SCOPE')
  })

  it('rejects guests and namespace escapes without invoking the backend', async () => {
    const h = setup()
    const guest = { ...h.event, senderFrame: { url: h.frame.url } } as unknown as IpcMainInvokeEvent
    await expect(h.open(guest, request)).rejects.toThrow('PLUGIN_SESSION_DENIED')
    const handle = await h.open(h.event, request)

    for (const path of ['/../api/config', '//evil.test/', '/%2e%2e/config', '/console\\lock']) {
      await expect(h.call(h.event, handle, { path, selection })).rejects.toThrow('PLUGIN_SESSION_DENIED')
    }

    expect(h.invoke).not.toHaveBeenCalled()
  })

  it('preserves hash navigation and sends full-navigation deselection only through the captured transport', async () => {
    const h = setup()
    const handle = await h.open(h.event, request)
    await h.call(h.event, handle, { path: '/_host/selection', method: 'POST', selection: { ...selection, scope: undefined, epoch: 0 } })
    const navigate = h.sender.on.mock.calls.find(([event]) => event === 'did-start-navigation')![1]
    navigate({}, h.frame.url, true, true)
    expect(h.registry.transport(handle, 7, 'hermes-cpa')).toBe(h.invoke)
    navigate({}, h.frame.url, false, true)
    expect(() => h.registry.transport(handle, 7, 'hermes-cpa')).toThrow('STALE_SCOPE')
    expect(h.invoke).toHaveBeenLastCalledWith({ path: '/_host/deselection', method: 'POST', selection, timeoutMs: 9000 })
  })

  it('recognizes exact pre-dispatch busy but neither generic429 nor ambiguous errors', () => {
    const busy = Object.assign(new Error('429: {"error":"PLUGIN_NOT_FORWARDED_BUSY","forwarded":false}'), { statusCode: 429 })
    expect(pluginSessionFailure(busy)).toEqual({ ok: false, code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false })

    for (const error of [new Error('outcome unknown with SECRET'), Object.assign(new Error('429: busy'), { statusCode: 429 }),
      Object.assign(new Error('429: {"error":"PLUGIN_NOT_FORWARDED_BUSY","forwarded":true}'), { statusCode: 429 })]) {
      expect(pluginSessionFailure(error)).toEqual({ ok: false, code: 'PLUGIN_REQUEST_FAILED', forwarded: 'unknown' })
    }
  })
})
