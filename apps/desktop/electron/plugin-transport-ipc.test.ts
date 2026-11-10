import { beforeEach, describe, expect, it, vi } from 'vitest'

import { registerPluginFileSaveIpc } from './plugin-file-save-ipc'
import { MAX_PLUGIN_RESOURCE_WIRE } from './plugin-resource-contract'
import { registerPluginResourceIpc } from './plugin-resource-ipc'
import type { PluginResourcePresentations } from './plugin-resource-presentation'
import type { PluginSessionOwner } from './plugin-session-contract'
import { registerPluginSessionIpc } from './plugin-session-ipc'
import { registerPluginTransportIpc } from './plugin-transport-ipc'

vi.mock('./plugin-file-save-ipc', () => ({ registerPluginFileSaveIpc: vi.fn() }))
vi.mock('./plugin-resource-ipc', () => ({ registerPluginResourceIpc: vi.fn() }))
vi.mock('./plugin-session-ipc', () => ({ registerPluginSessionIpc: vi.fn() }))

const owner = { connectionId: 'source-connection', profile: 'renderer-alias' }
const selection = { client: 'client_with_sixteen_characters', scope: 'a'.repeat(32), epoch: 3 }

beforeEach(() => vi.clearAllMocks())

function setup() {
  const invoke = vi.fn(async (_path: string, _options: unknown) => ({ ok: true }))
  const sessionInvoke = vi.fn(async () => ({ ok: true }))
  const transport = vi.fn(() => sessionInvoke)
  vi.mocked(registerPluginSessionIpc).mockReturnValue({ transport })

  const capture = vi.fn(async (_owner: PluginSessionOwner, path: string) => ({
    requestPath: `${path}?profile=actual-owner`, invoke
  }))

  registerPluginTransportIpc({
    presentations: {} as PluginResourcePresentations,
    isTrustedWindow: () => true,
    rendererUrl: () => 'file:///app/index.html',
    capture
  })

  return {
    capture, invoke, sessionInvoke, transport,
    sessions: vi.mocked(registerPluginSessionIpc).mock.calls[0][0],
    resources: vi.mocked(registerPluginResourceIpc).mock.calls[0][0]
  }
}

describe('captured plugin backend transport', () => {
  it('keeps the resolved owner, comparison headers and single-attempt operation options', async () => {
    const h = setup()
    const invoke = await h.sessions.capture({ pluginId: 'demo', owner })
    await invoke({ path: '/console/result?profile=other&part=1', method: 'POST', selection, body: { operation: 'owned' } })
    expect(h.capture).toHaveBeenCalledExactlyOnceWith(owner, '/api/plugins/demo/_host/selection')
    expect(h.invoke).toHaveBeenCalledExactlyOnceWith('/api/plugins/demo/console/result?part=1&profile=actual-owner', {
      method: 'POST', body: { operation: 'owned' }, timeoutMs: undefined,
      singleAttempt: true, maxResponseBytes: MAX_PLUGIN_RESOURCE_WIRE,
      headers: {
        'X-Hermes-Plugin-Client': selection.client,
        'X-Hermes-Plugin-Epoch': '3',
        'X-Hermes-Plugin-Scope': selection.scope
      }
    })
    expect(registerPluginFileSaveIpc).toHaveBeenCalledTimes(1)
  })

  it('closes a direct resource through the same captured descriptor and profile', async () => {
    const h = setup()

    const request = { pluginId: 'demo', owner, selection, documentId: 'd'.repeat(32), grantId: 'g'.repeat(32),
      entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 12345 }

    const invoke = await h.resources.capture(request, 7)
    await invoke({ action: 'read' }, false)
    await invoke({ action: 'close' }, true)
    expect(h.capture).toHaveBeenCalledExactlyOnceWith(owner, '/api/plugins/demo/console/resource')
    expect(h.invoke.mock.calls.map(([path]) => path)).toEqual([
      '/api/plugins/demo/console/resource?profile=actual-owner',
      '/api/plugins/demo/console/resource/close?profile=actual-owner'
    ])
  })

  it('uses the sender-bound session transport for a session resource without resolving another connection', async () => {
    const h = setup()

    const invoke = await h.resources.capture({ pluginId: 'demo', owner, selection, sessionId: 'captured-handle',
      documentId: 'd'.repeat(32), grantId: 'g'.repeat(32), entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 12345 }, 7)

    await invoke({ action: 'close' }, true)
    expect(h.capture).not.toHaveBeenCalled()
    expect(h.transport).toHaveBeenCalledExactlyOnceWith('captured-handle', 7, 'demo')
    expect(h.sessionInvoke).toHaveBeenCalledExactlyOnceWith({ path: '/console/resource/close', method: 'POST',
      body: { action: 'close' }, selection, timeoutMs: 60_000 })
  })
})
