import { $apiBackendRevision, $apiRequestScope } from './client'
import { unwrapPluginSessionReply } from '../../electron/plugin-session-contract'
import type { PluginSessionCall } from '../../electron/plugin-session-contract'
import type { PluginSelectionToken } from '../../electron/plugin-selection'
import type { PluginResourceRequest, PluginResourcePresentation } from '../../electron/plugin-resource-contract'

export interface PluginSessionOptions { method?: string; body?: unknown; timeoutMs?: number }
export interface PluginSession {
  rest(path: string, options?: PluginSessionOptions): Promise<unknown>
  presentResource(request: PluginResourceRequest): Promise<PluginResourcePresentation>
  onRetire(callback: () => void | Promise<void>): () => void
  close(): Promise<void>
}
const sessions = new Map<string, PluginSession>()

/** A mount owns one main-process backend snapshot, not the ambient REST door. */
export function createPluginSession(pluginId: string): PluginSession {
  const bridge = window.hermesDesktop.pluginSession
  if (!bridge) throw new Error('PLUGIN_SESSION_UNSUPPORTED')
  const owner = { ...$apiRequestScope.get() }
  const ownerKey = JSON.stringify(owner)
  const backendRevision = $apiBackendRevision.get()
  const previous = sessions.get(pluginId)
  const previousClosed = previous?.close() ?? Promise.resolve()
  const opening = bridge.open({ pluginId, owner })
  let token: PluginSelectionToken = { client: crypto.randomUUID(), epoch: 0 }
  let state: 'open' | 'retiring' | 'closed' = 'open'
  let closing: Promise<void> | undefined
  let unsubscribe: (() => void) | undefined
  let unsubscribeBackend: (() => void) | undefined
  const callbacks = new Set<() => void | Promise<void>>()
  const resources = new Set<() => Promise<void>>()
  const call = async (path: string, options: PluginSessionOptions = {}): Promise<unknown> => {
    const request: PluginSessionCall = { ...options, path, selection: { ...token } }
    return unwrapPluginSessionReply(await bridge.call(await opening, request))
  }
  const ready = previousClosed.then(async () => {
    const acknowledgment = await call('/_host/selection', { method: 'POST', timeoutMs: 9000 })
    if (!acknowledgment || typeof acknowledgment !== 'object' || !('scope_id' in acknowledgment) ||
        typeof acknowledgment.scope_id !== 'string' || !/^[a-f0-9]{32}$/.test(acknowledgment.scope_id) ||
        !('epoch' in acknowledgment) || typeof acknowledgment.epoch !== 'number' ||
        !Number.isSafeInteger(acknowledgment.epoch) || acknowledgment.epoch <= token.epoch) throw new Error('STALE_SCOPE')
    token = { ...token, scope: acknowledgment.scope_id, epoch: acknowledgment.epoch }
  })
  void ready.catch(() => undefined)
  void opening.catch(() => undefined)
  const current = () => state === 'open' && backendRevision === $apiBackendRevision.get() &&
    ownerKey === JSON.stringify($apiRequestScope.get())
  const session: PluginSession = {
    async rest(path, options) {
      await ready
      const revoking = path === '/console/lock' || path === '/console/resource/close'
      if (!current() && !(state === 'retiring' && revoking)) throw new Error('STALE_SCOPE')
      const result = await call(path, options)
      if (!current() && !revoking) throw new Error('STALE_SCOPE')
      return result
    },
    async presentResource(request) {
      await ready
      if (!current()) throw new Error('STALE_SCOPE')
      const desktop = window.hermesDesktop
      if (!desktop.presentPluginResource || !desktop.closePluginResource) throw new Error('RESOURCE_UNSUPPORTED')
      const result = await desktop.presentPluginResource({ ...request, pluginId, owner, selection: { ...token }, sessionId: await opening })
      let pending: Promise<void> | undefined
      const close = () => {
        if (!pending) { resources.delete(close); pending = desktop.closePluginResource!(result.handleId) }
        return pending
      }
      if (!current()) { await close().catch(() => undefined); throw new Error('STALE_SCOPE') }
      resources.add(close)
      return { url: result.url, close }
    },
    onRetire(callback) {
      if (state !== 'open') throw new Error('STALE_SCOPE')
      callbacks.add(callback)
      return () => { callbacks.delete(callback) }
    },
    close() {
      if (closing) return closing
      state = 'retiring'
      unsubscribe?.()
      unsubscribeBackend?.()
      closing = (async () => {
        try {
          await ready
          const resourceCleanup = Promise.allSettled([...resources].map(close => close()))
          // Callbacks may only use the two revocation routes while retiring.
          // Both their identifiers and transport remain pinned to this owner.
          await Promise.allSettled([...callbacks].map(callback => Promise.resolve().then(callback)))
          await resourceCleanup
          await call('/_host/deselection', { method: 'POST', timeoutMs: 9000 })
        } finally {
          state = 'closed'
          callbacks.clear()
          await opening.then(handle => bridge.close(handle))
        }
      })()
      void closing.catch(() => undefined)
      return closing
    }
  }
  sessions.set(pluginId, session)
  unsubscribe = $apiRequestScope.subscribe(() => { if (!current()) void session.close().catch(() => undefined) })
  unsubscribeBackend = $apiBackendRevision.subscribe(() => { if (!current()) void session.close().catch(() => undefined) })
  return session
}
