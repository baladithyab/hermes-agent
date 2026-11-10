export interface PluginSelectionToken {
  client: string
  scope?: string
  epoch: number
}

/** Only comparison tokens cross this bridge. Authentication headers remain
 * Electron-owned; a renderer cannot provide a credential or a home here. */
export function pluginSelectionHeaders(path: string, token?: PluginSelectionToken): Record<string, string> {
  if (token === undefined) {return {}}

  if (!/^\/api\/plugins\/[a-zA-Z0-9_-]+\//.test(path) ||
      typeof token.client !== 'string' || !/^[a-zA-Z0-9_-]{16,128}$/.test(token.client) ||
      !Number.isSafeInteger(token.epoch) || token.epoch < 0 ||
      (token.scope !== undefined && !/^[a-f0-9]{32}$/.test(token.scope))) {
    throw new Error('Invalid plugin selection token')
  }

  return {
    'X-Hermes-Plugin-Client': token.client,
    'X-Hermes-Plugin-Epoch': String(token.epoch),
    ...(token.scope ? { 'X-Hermes-Plugin-Scope': token.scope } : {})
  }
}
