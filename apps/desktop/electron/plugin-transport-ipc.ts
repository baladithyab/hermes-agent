import type { BrowserWindow } from 'electron'

import { registerPluginFileSaveIpc } from './plugin-file-save-ipc'
import { MAX_PLUGIN_RESOURCE_WIRE } from './plugin-resource-contract'
import { registerPluginResourceIpc } from './plugin-resource-ipc'
import type { PluginResourcePresentations } from './plugin-resource-presentation'
import { pluginSelectionHeaders } from './plugin-selection'
import type { PluginSessionOpen, PluginSessionOwner } from './plugin-session-contract'
import { registerPluginSessionIpc } from './plugin-session-ipc'
import type { CapturedPluginTransport } from './plugin-session-ipc'

interface CapturedPluginBackend {
  requestPath: string
  invoke: (path: string, options: PluginTransportRequest) => Promise<unknown>
}

export interface PluginTransportRequest {
  method?: string
  body?: unknown
  timeoutMs?: number
  headers?: Record<string, string>
  singleAttempt: true
  maxResponseBytes: number
}

interface PluginTransportOptions {
  isTrustedWindow: (window: BrowserWindow) => boolean
  rendererUrl: () => string
  presentations: PluginResourcePresentations
  /** Resolve once; invoke retains the descriptor and its authentication path. */
  capture: (owner: PluginSessionOwner, path: string) => Promise<CapturedPluginBackend>
}

const EXTENDED_RESPONSE_ROUTES = new Set([
  '/console/prepare', '/console/result', '/console/file',
  '/console/resource', '/console/browser', '/console/plugin'
])

async function captureSession(options: PluginTransportOptions, request: PluginSessionOpen): Promise<CapturedPluginTransport> {
  const prefix = `/api/plugins/${request.pluginId}`
  const captured = await options.capture(request.owner, `${prefix}/_host/selection`)
  const profile = new URL(captured.requestPath, 'https://plugin.invalid').searchParams.get('profile')

  return call => {
    const path = prefix + call.path
    const parsed = new URL(path, 'https://plugin.invalid')
    parsed.searchParams.delete('profile')

    if (profile !== null) {parsed.searchParams.set('profile', profile)}

    return captured.invoke(parsed.pathname + parsed.search, {
      method: call.method,
      body: call.body,
      timeoutMs: call.timeoutMs,
      headers: pluginSelectionHeaders(path, call.selection),
      singleAttempt: true,
      maxResponseBytes: EXTENDED_RESPONSE_ROUTES.has(call.path.split('?')[0])
        ? MAX_PLUGIN_RESOURCE_WIRE : 8 * 1024 * 1024
    })
  }
}

/** Native plugin capabilities share the captured owner, never a resource URL. */
export function registerPluginTransportIpc(options: PluginTransportOptions): void {
  const authorization = { isTrustedWindow: options.isTrustedWindow, rendererUrl: options.rendererUrl }
  registerPluginFileSaveIpc(authorization)

  const sessions = registerPluginSessionIpc({
    ...authorization,
    capture: request => captureSession(options, request)
  })

  registerPluginResourceIpc({
    ...authorization,
    presentations: options.presentations,
    capture: async (request, senderId) => {
      if (request.sessionId) {
        const invoke = sessions.transport(request.sessionId, senderId, request.pluginId)

        return (body, close) => invoke({
          path: close ? '/console/resource/close' : '/console/resource',
          method: 'POST', body, selection: request.selection, timeoutMs: 60_000
        })
      }

      const path = `/api/plugins/${request.pluginId}/console/resource`
      const captured = await options.capture(request.owner, path)
      const closePath = new URL(captured.requestPath, 'https://plugin.invalid')
      closePath.pathname += '/close'
      const headers = pluginSelectionHeaders(path, request.selection)

      return (body, close) => captured.invoke(close ? closePath.pathname + closePath.search : captured.requestPath, {
        method: 'POST', body, headers, timeoutMs: 60_000,
        singleAttempt: true, maxResponseBytes: MAX_PLUGIN_RESOURCE_WIRE
      })
    }
  })
}
