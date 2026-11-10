// @vitest-environment node
import { BrowserWindow, ipcMain } from 'electron'
import type { IpcMainInvokeEvent } from 'electron'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registerPluginResourceIpc } from './plugin-resource-ipc'
import { PluginResourcePresentations } from './plugin-resource-presentation'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })

  return { promise, resolve }
}

vi.mock('electron', () => ({ BrowserWindow: { fromWebContents: vi.fn() }, ipcMain: { handle: vi.fn() } }))

const request = () => ({ pluginId: 'hermes-cpa', documentId: 'cdce3827-ef75-4770-b081-491a58125eb2', grantId: 'G'.repeat(43),
  entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: Date.now() / 1000 + 300,
  owner: { connectionId: 'remote', profile: 'alpha' },
  selection: { client: 'client-with-sixteen-characters', scope: 'a'.repeat(32), epoch: 1 } })

function setup() {
  const frame = { url: 'file:///app/index.html?profile=alpha' }
  const sender = { id: 7, mainFrame: frame, isDestroyed: () => false, once: vi.fn(), on: vi.fn() }
  const owner = { webContents: sender, isDestroyed: () => false }
  vi.mocked(BrowserWindow.fromWebContents).mockReturnValue(owner as unknown as BrowserWindow)
  const presentations = new PluginResourcePresentations()
  const invoke = vi.fn().mockResolvedValue({ api_version: 1, data: { ok: true, closed: true } })
  const capture = vi.fn().mockResolvedValue(invoke)
  const trusted = vi.fn().mockReturnValue(true)
  registerPluginResourceIpc({ presentations, isTrustedWindow: trusted, rendererUrl: () => 'file:///app/index.html', capture })
  const handler = (channel: string) => vi.mocked(ipcMain.handle).mock.calls.find(([name]) => name === channel)![1]
  const event = { sender, senderFrame: frame } as unknown as IpcMainInvokeEvent

  return { frame, sender, owner, presentations, invoke, capture, trusted, event,
    open: handler('hermes:plugin:resource:open'), close: handler('hermes:plugin:resource:close') }
}

beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers(); vi.setSystemTime(1_000_000) })
afterEach(() => vi.useRealTimers())

describe('trusted resource presenter IPC', () => {
  it('accepts only the known main renderer document, not its opaque guest or another file URL', async () => {
    const h = setup()
    const guest = { ...h.event, senderFrame: { url: h.frame.url } } as unknown as IpcMainInvokeEvent
    await expect(h.open(guest, request())).rejects.toThrow('not authorized')
    h.frame.url = 'file:///private/other.html'
    await expect(h.open(h.event, request())).rejects.toThrow('not authorized')
    h.frame.url = 'file:///app/index.html'
    h.trusted.mockReturnValue(false)
    await expect(h.open(h.event, request())).rejects.toThrow('not authorized')
    expect(h.capture).not.toHaveBeenCalled()
  })

  it('captures owner once and rechecks sender after the asynchronous backend capture', async () => {
    const h = setup()
    const pendingCapture = deferred<unknown>()
    const started = deferred<void>()
    h.capture.mockImplementation(() => { started.resolve();

 return pendingCapture.promise })
    const pending = h.open(h.event, request())
    const failed = expect(pending).rejects.toThrow('RESOURCE_UNAVAILABLE')
    await started.promise
    h.frame.url = 'https://untrusted.test/'
    pendingCapture.resolve(h.invoke)
    await failed
    expect(h.invoke).not.toHaveBeenCalled()
  })

  it('returns only the local capability URL/handle and revokes it on renderer disposal', async () => {
    const h = setup()
    const opened = await h.open(h.event, request())
    expect(Object.keys(opened).sort()).toEqual(['handleId', 'url'])
    expect(opened.url).toMatch(/^hermes-plugin-resource:\/\/p-[a-f0-9]{48}\/__hermes_plugin_resource__$/)
    expect(h.capture).toHaveBeenCalledWith(request(), 7)
    const destroyed = h.sender.once.mock.calls.find(([event]) => event === 'destroyed')![1]
    destroyed()
    expect((await h.presentations.handle(new Request(opened.url))).status).toBe(410)
  })

  it('preserves same-document hash navigation but retires on full navigation', async () => {
    const h = setup()
    const opened = await h.open(h.event, request())
    const navigate = h.sender.on.mock.calls.find(([event]) => event === 'did-start-navigation')![1]
    h.frame.url += '#/chat/other'
    navigate({}, h.frame.url, true, true)
    expect((await h.presentations.handle(new Request(opened.url))).status).toBe(200)
    navigate({}, h.frame.url, false, true)
    expect((await h.presentations.handle(new Request(opened.url))).status).toBe(410)
  })

  it('never serializes captured transport exceptions through IPC', async () => {
    const h = setup()
    h.capture.mockRejectedValue(new Error('https://private/?token=SECRET'))
    await expect(h.open(h.event, request())).rejects.toThrow(/^RESOURCE_UNAVAILABLE$/)
  })
})
