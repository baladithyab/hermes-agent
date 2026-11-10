import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { expect, test } from 'vitest'

import { fetchAuthenticatedJson } from './authenticated-json-fetch'

async function listen(server: http.Server): Promise<string> {
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })

  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`
}

async function close(server: http.Server): Promise<void> {
  server.closeAllConnections()
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
}

test('real HTTP retains configured headers, per-request selection and owning authentication', async () => {
  const server = http.createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify(request.headers))
  })
  const origin = await listen(server)

  try {
    const headers = await fetchAuthenticatedJson(`${origin}/api/plugins/generated/items`, 'generated-owner-token', {
      headers: { 'X-Hermes-Plugin-Scope': 'generated-selection' }, singleAttempt: true, timeoutMs: 2000
    }, {
      hermesHome: '/unused-generated-token-mode-home',
      headersForRemoteRequest: () => ({ 'X-Configured-Client': 'generated-configured-client' })
    })

    expect(headers).toMatchObject({
      'x-configured-client': 'generated-configured-client',
      'x-hermes-plugin-scope': 'generated-selection',
      'x-hermes-session-token': 'generated-owner-token'
    })
  } finally {
    await close(server)
  }
})

test('a POST accepted by the real server is never replayed after its response is lost', async () => {
  let accepted = 0
  const server = http.createServer(request => {
    accepted += 1
    request.socket.destroy()
  })
  const origin = await listen(server)

  try {
    await expect(fetchAuthenticatedJson(`${origin}/api/plugins/generated/accept`, 'generated-owner-token', {
      method: 'POST', body: { generated: true }, timeoutMs: 2000
    }, { hermesHome: '/unused-generated-token-mode-home', headersForRemoteRequest: () => ({}) })).rejects.toThrow()
    expect(accepted).toBe(1)
  } finally {
    await close(server)
  }
})
