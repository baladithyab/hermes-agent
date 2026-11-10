import type { PluginSelectionToken } from './plugin-selection'

export const PLUGIN_RESOURCE_SCHEME = 'hermes-plugin-resource'
export const PLUGIN_RESOURCE_WRAPPER_PATH = '/__hermes_plugin_resource__'
export const MAX_PLUGIN_RESOURCE_BODY = 32 * 1024 * 1024
export const MAX_PLUGIN_RESOURCE_WIRE = 48 * 1024 * 1024

export interface PluginResourceRequest {
  documentId: string
  grantId: string
  entryPath: string
  /** Owner Unix seconds. Desktop uses an independent local lifetime <=5min. */
  expiresAt: number
}
export interface PluginResourcePresentation {
  url: string
  close: () => Promise<void>
}
export interface PluginResourceBridgeRequest extends PluginResourceRequest {
  pluginId: string
  sessionId?: string
  owner: { connectionId: string | null; profile: string | null }
  selection: PluginSelectionToken
}
export interface PluginResourceBridgeResult { url: string; handleId: string }

export function isPluginResourceUrl(source: string): boolean {
  try {
    const url = new URL(source)
    return url.protocol === `${PLUGIN_RESOURCE_SCHEME}:` && /^p-[a-f0-9]{48}$/.test(url.hostname) &&
      !url.port && !url.username && !url.password
  } catch { return false }
}

export function redactPluginResourceUrls(text: string): string {
  return text.replace(/hermes-plugin-resource:\/\/\S+/gi, '[plugin-resource]')
    .replace(/p-[a-f0-9]{48}\S*/gi, '[plugin-resource]')
}
