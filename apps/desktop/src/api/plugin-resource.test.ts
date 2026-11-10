import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'
import { setApiRequestBackend, setApiRequestConnection, setApiRequestProfile } from './client'
import { pluginRest, presentPluginResource } from './plugins'
import type { HermesApiRequest } from '@/global'
import type { PluginResourceBridgeRequest, PluginResourceBridgeResult } from '../../electron/plugin-resource-contract'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

const resource = { documentId: 'cdce3827-ef75-4770-b081-491a58125eb2', grantId: 'G'.repeat(43),
  entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: Date.now() / 1000 + 300 }
const result = { url: 'hermes-plugin-resource://p-' + 'a'.repeat(48) + '/__hermes_plugin_resource__', handleId: 'p-' + 'a'.repeat(48) }

function bridge(open: (request: PluginResourceBridgeRequest) => Promise<PluginResourceBridgeResult>) {
  let epoch = 0
  const api = vi.fn(async (request: HermesApiRequest) => {
    if (request.path.includes('/_host/selection') || request.path.includes('/_host/deselection')) {
      epoch += 1
      return { scope_id: epoch.toString(16).padStart(32, '0'), epoch }
    }
    return { api_version: 1, data: { ok: true } }
  })
  const closePluginResource = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(window, 'hermesDesktop', { configurable: true,
    value: { api, presentPluginResource: open, closePluginResource } })
  return { api, closePluginResource }
}

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('selection-bound desktop resource presenter', () => {
  it('uses the identical acknowledged owner/token as core REST, and closes on selection change', async () => {
    const open = vi.fn().mockResolvedValue(result)
    const host = bridge(open)
    setApiRequestConnection('remote-alpha')
    setApiRequestProfile('research')
    await pluginRest('resource-same-session', '/console/unlock', { method: 'POST', selectionBound: true })
    const handle = await presentPluginResource('resource-same-session', resource)
    const core = host.api.mock.calls.find(([request]) => request.path.endsWith('/console/unlock'))![0]
    expect(open).toHaveBeenCalledWith({ ...resource, pluginId: 'resource-same-session',
      owner: { connectionId: 'remote-alpha', profile: 'research' }, selection: core.pluginSelection })
    expect(handle.url).toBe(result.url)
    setApiRequestProfile('different')
    expect(host.closePluginResource).toHaveBeenCalledWith(result.handleId)
    await handle.close()
    expect(host.closePluginResource).toHaveBeenCalledTimes(1)
  })

  it('discards and closes a late main-process response after the selection changed', async () => {
    const opened = deferred<PluginResourceBridgeResult>()
    const started = deferred<void>()
    const host = bridge(async () => { started.resolve(); return opened.promise })
    setApiRequestConnection('remote-original')
    setApiRequestProfile('alpha')
    const pending = presentPluginResource('resource-late-session', resource)
    const failure = expect(pending).rejects.toThrow('STALE_SCOPE')
    await started.promise
    setApiRequestProfile('beta')
    opened.resolve(result)
    await failure
    expect(host.closePluginResource).toHaveBeenCalledTimes(1)
    expect(host.closePluginResource).toHaveBeenCalledWith(result.handleId)
  })

  it('closes a presentation that finishes after the owning plugin is disposed', async () => {
    const opened = deferred<PluginResourceBridgeResult>()
    const started = deferred<void>()
    const host = bridge(async () => { started.resolve(); return opened.promise })
    setApiRequestConnection('remote-plugin')
    setApiRequestProfile('alpha')
    const disposers: Array<() => void> = []
    const context = createPluginContext('resource-disposed-plugin', dispose => disposers.push(dispose))
    const pending = context.os.presentPluginResource!(resource)
    const failure = expect(pending).rejects.toThrow('RESOURCE_REVOKED')
    await started.promise
    disposers.forEach(dispose => dispose())
    opened.resolve(result)
    await failure
    expect(host.closePluginResource).toHaveBeenCalledTimes(1)
    expect(host.closePluginResource).toHaveBeenCalledWith(result.handleId)
  })
})

it('retires a legacy resource presentation when the resolved backend changes under unchanged labels', async () => {
  setApiRequestBackend({ baseUrl: 'http://resource-old.local', token: 'old-generated', mode: 'remote' })
  const open = vi.fn().mockResolvedValue(result)
  const captured = bridge(open)
  const presented = await presentPluginResource('resource-backend-change', resource)
  setApiRequestBackend({ baseUrl: 'http://resource-new.local', token: 'new-generated', mode: 'remote' })
  await presented.close()
  expect(captured.closePluginResource).toHaveBeenCalledExactlyOnceWith(result.handleId)
})
