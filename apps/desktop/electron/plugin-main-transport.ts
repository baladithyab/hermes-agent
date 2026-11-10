import type { BrowserWindow } from 'electron'

import type { BackendDialClaims } from './backend-dial-claim'
import { apiRequestRegistryConnectionId, pathForRegistryBackendRequest, resolveProfileApiRequest } from './connection-config'
import type { ProfileRouteOptions, RegistryBackendRequestScope } from './connection-config'
import { backendScopeKey } from './connection-registry'
import type { GatewayEndpoint } from './local-gateway'
import { withoutInteractiveOauthLogin } from './oauth-rest-request'
import type { PluginResourcePresentations } from './plugin-resource-presentation'
import { registerPluginTransportIpc } from './plugin-transport-ipc'
import type { PluginTransportRequest } from './plugin-transport-ipc'

interface BackendSnapshot extends RegistryBackendRequestScope {
  headers?: Record<string, string>
  gatewayEndpoint?: GatewayEndpoint
}

interface MainPluginTransportOptions<Connection extends BackendSnapshot> {
  presentations: PluginResourcePresentations
  isTrustedWindow: (window: BrowserWindow) => boolean
  rendererUrl: () => string
  backendDialClaims: BackendDialClaims
  profileRouteOptions: (profile: string | null, request: { method: string; path: string }) => ProfileRouteOptions
  ensureRegistryBackend: (id: string, profile: string | null) => Promise<Connection>
  ensureBackend: (profile: string | null, options: { request: { method: string; path: string } }) => Promise<Connection>
  fetchJsonForBackend: (connection: Connection, path: string, options: PluginTransportRequest) => Promise<unknown>
}

/** Capture the routed descriptor once. Later selection changes cannot redirect
 * a resource/session handle or substitute another owner's native grant. */
export function registerMainPluginTransport<Connection extends BackendSnapshot>(options: MainPluginTransportOptions<Connection>): void {
  registerPluginTransportIpc({
    presentations: options.presentations,
    isTrustedWindow: options.isTrustedWindow,
    rendererUrl: options.rendererUrl,
    capture: async (owner, path) => {
      const routed = { ...owner, method: 'POST', path }
      const registryId = apiRequestRegistryConnectionId(routed)
      const route = resolveProfileApiRequest(owner.profile, path, options.profileRouteOptions(owner.profile, routed))

      const connection = registryId
        ? await options.backendDialClaims.run(backendScopeKey(registryId, owner.profile), () => options.ensureRegistryBackend(registryId, owner.profile))
        : await options.ensureBackend(route.backendProfile, { request: { method: 'POST', path } })

      const captured = {
        ...connection,
        headers: { ...connection.headers },
        ...(connection.gatewayEndpoint ? {
          gatewayEndpoint: { ...connection.gatewayEndpoint, capabilities: [...connection.gatewayEndpoint.capabilities] }
        } : {})
      }

      return {
        requestPath: registryId ? pathForRegistryBackendRequest(path, owner.profile, captured, 'POST') : route.requestPath,
        invoke: (requestPath, requestOptions) => withoutInteractiveOauthLogin(() => options.fetchJsonForBackend(captured, requestPath, requestOptions))
      }
    }
  })
}
