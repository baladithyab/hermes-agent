import type { PluginSelectionToken } from './plugin-selection'

export interface PluginSessionOwner { connectionId: string | null; profile: string | null }
export interface PluginSessionOpen { pluginId: string; owner: PluginSessionOwner }
export interface PluginSessionCall {
  path: string
  method?: string
  body?: unknown
  timeoutMs?: number
  selection: PluginSelectionToken
}
export type PluginSessionReply =
  | { ok: true; value: unknown }
  | { ok: false; code: 'PLUGIN_NOT_FORWARDED_BUSY'; forwarded: false }
  | { ok: false; code: 'PLUGIN_REQUEST_FAILED'; forwarded: 'unknown' }
export interface PluginSessionBridge {
  open(request: PluginSessionOpen): Promise<string>
  call(handle: string, request: PluginSessionCall): Promise<PluginSessionReply>
  close(handle: string): Promise<void>
}

export function pluginSessionFailure(error: unknown): PluginSessionReply {
  if (error instanceof Error && 'statusCode' in error && error.statusCode === 429 && error.message.startsWith('429: ')) {
    try {
      const body: unknown = JSON.parse(error.message.slice(5))

      if (body && typeof body === 'object' && 'error' in body && body.error === 'PLUGIN_NOT_FORWARDED_BUSY' &&
          'forwarded' in body && body.forwarded === false) {
        return { ok: false, code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false }
      }
    } catch { /* Not an admission response. */ }
  }

  return { ok: false, code: 'PLUGIN_REQUEST_FAILED', forwarded: 'unknown' }
}

export function unwrapPluginSessionReply(reply: PluginSessionReply): unknown {
  if (reply.ok === true) {return reply.value}
  throw Object.assign(new Error(reply.code), { code: reply.code, forwarded: reply.forwarded })
}
