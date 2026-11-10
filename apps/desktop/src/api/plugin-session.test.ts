import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'

import type { PluginSessionCall, PluginSessionOpen, PluginSessionReply } from '../../electron/plugin-session-contract'

import { setApiRequestBackend, setApiRequestConnection, setApiRequestProfile } from './client'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })

  return { promise, resolve }
}

function host() {
  let sequence = 0
  const owners = new Map<string, PluginSessionOpen>()
  const calls: Array<{ handle: string; request: PluginSessionCall }> = []

  const open = vi.fn(async (request: PluginSessionOpen) => {
    const handle = `captured-${++sequence}`
    owners.set(handle, request)

    return handle
  })

  const call = vi.fn(async (handle: string, request: PluginSessionCall): Promise<PluginSessionReply> => {
    calls.push({ handle, request })

    if (request.path === '/_host/selection' || request.path === '/_host/deselection') {
      return { ok: true, value: { scope_id: 'a'.repeat(32), epoch: request.selection.epoch + 1 } }
    }

    return { ok: true, value: { owner: owners.get(handle)?.owner } }
  })

  const close = vi.fn(async (_handle: string) => undefined)
  const closePluginResource = vi.fn(async (_handle: string) => undefined)
  const presentPluginResource = vi.fn(async () => ({ handleId: 'resource-handle', url: 'hermes-plugin-resource://local/__hermes_plugin_resource__' }))
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: {
    pluginSession: { open, call, close }, presentPluginResource, closePluginResource
  } })

  return { open, call, close, calls, owners, presentPluginResource, closePluginResource }
}

afterEach(() => {
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('captured Desktop plugin sessions', () => {
  it('revokes old identifiers on the old transport before deselection and terminal close', async () => {
    const h = host()
    setApiRequestConnection('remote-a')
    setApiRequestProfile('alpha')
    const old = createPluginContext('pinned-revocation').createPluginSession()
    await old.rest('/console/unlock', { method: 'POST' })
    const oldHandle = h.calls[0].handle
    old.onRetire(async () => {
      await old.rest('/console/resource/close', { method: 'POST', body: { document_id: 'old-document', grant_id: 'old-grant' } })
      await old.rest('/console/lock', { method: 'POST', body: { lease_id: 'old-lease' } })
    })
    setApiRequestConnection('remote-b')
    setApiRequestProfile('beta')
    const replacement = createPluginContext('pinned-revocation').createPluginSession()
    await replacement.rest('/console/unlock', { method: 'POST' })
    await old.close()
    const retirement = h.calls.filter(call => call.handle === oldHandle).map(call => call.request.path)
    expect(retirement).toEqual(['/_host/selection', '/console/unlock', '/console/resource/close', '/console/lock', '/_host/deselection'])
    expect(h.owners.get(oldHandle)?.owner).toEqual({ connectionId: 'remote-a', profile: 'alpha' })
    expect(h.calls.filter(call => call.handle !== oldHandle).every(call => !call.request.body)).toBe(true)
    await expect(old.rest('/console/execute', { body: {} })).rejects.toThrow('STALE_SCOPE')
    await expect(old.rest('/console/lock')).rejects.toThrow('STALE_SCOPE')
    await replacement.close()
    expect(h.close.mock.calls.filter(([handle]) => handle === oldHandle)).toHaveLength(1)
  })

  it('suppresses late results and presents resources through the same captured main handle', async () => {
    const h = host()
    setApiRequestConnection('resource-owner')
    setApiRequestProfile('alpha')
    const session = createPluginContext('pinned-resource').createPluginSession()
    await session.rest('/ready')
    const resource = { documentId: 'old-document', grantId: 'old-grant', entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 1.25 }
    await session.presentResource(resource)
    expect(h.presentPluginResource).toHaveBeenCalledWith({ ...resource, pluginId: 'pinned-resource', sessionId: h.calls[0].handle,
      owner: { connectionId: 'resource-owner', profile: 'alpha' }, selection: h.calls[1].request.selection })
    const result = deferred<PluginSessionReply>()
    const started = deferred<void>()
    h.call.mockImplementationOnce(async () => { started.resolve();

 return result.promise })
    const pending = session.rest('/console/result')
    const rejected = expect(pending).rejects.toThrow('STALE_SCOPE')
    await started.promise
    setApiRequestProfile('beta')
    result.resolve({ ok: true, value: 'old-secret' })
    await rejected
    await session.close()
    expect(h.closePluginResource).toHaveBeenCalledExactlyOnceWith('resource-handle')
  })

  it('exposes only an explicit host refusal as not forwarded', async () => {
    const h = host()
    const session = createPluginContext('pinned-busy').createPluginSession()
    await session.rest('/ready')
    h.call.mockResolvedValueOnce({ ok: false, code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false })
    await expect(session.rest('/console/execute')).rejects.toMatchObject({ code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false })
    h.call.mockResolvedValueOnce({ ok: false, code: 'PLUGIN_REQUEST_FAILED', forwarded: 'unknown' })
    await expect(session.rest('/console/execute')).rejects.toMatchObject({ code: 'PLUGIN_REQUEST_FAILED', forwarded: 'unknown' })
    await session.close()
  })
})

it('retires on an actual backend replacement even when the selection labels do not change', async () => {
  const h = host()
  setApiRequestBackend({ baseUrl: 'http://old.local', token: 'old-generated-token', mode: 'remote' })
  const session = createPluginContext('backend-replaced').createPluginSession()
  await session.rest('/ready')
  const retire = vi.fn(async () => { await session.rest('/console/lock', { method: 'POST', body: { lease_id: 'old' } }) })
  session.onRetire(retire)
  setApiRequestBackend(null)
  expect(retire).not.toHaveBeenCalled()
  setApiRequestBackend({ baseUrl: 'http://new.local', token: 'new-generated-token', mode: 'remote' })
  await session.close()
  expect(retire).toHaveBeenCalledTimes(1)
  expect(h.calls.map(call => call.request.path)).toEqual(['/_host/selection', '/ready', '/console/lock', '/_host/deselection'])
  expect(new Set(h.calls.map(call => call.handle)).size).toBe(1)
})
