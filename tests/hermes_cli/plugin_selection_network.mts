import assert from 'node:assert/strict'
import { PluginSelectionSession } from '../../apps/desktop/src/api/plugin-selection-session.ts'

const base = process.env.TEST_PLUGIN_URL!
const authName = process.env.TEST_PLUGIN_AUTH_HEADER!
const auth = process.env.TEST_PLUGIN_AUTH!
const headers = token => ({ [authName]: auth,
  'X-Hermes-Plugin-Client': token.client,
  'X-Hermes-Plugin-Epoch': String(token.epoch),
  ...(token.scope ? { 'X-Hermes-Plugin-Scope': token.scope } : {}) })
const session = new PluginSelectionSession(async (_owner, action, token) => {
  const response = await fetch(`${base}/_host/${action}`, {
    method: 'POST', headers: headers(token), signal: AbortSignal.timeout(5000)
  })
  assert.equal(response.status, 200)
  return response.json()
})
const read = lease => fetch(`${base}/scope`, { headers: headers(lease.token), signal: AbortSignal.timeout(5000) })
session.change({ profile: null, connectionId: 'test-route-A' })
const first = await session.acquire()
assert.equal((await read(first)).status, 200)
session.change({ profile: null, connectionId: 'test-route-B' })
const second = await session.acquire()
assert.equal((await read(first)).status, 409)
assert.equal((await read(second)).status, 200)
session.change({ profile: null, connectionId: 'test-route-A' })
const third = await session.acquire()
assert.equal((await read(first)).status, 409)
assert.equal((await read(second)).status, 409)
const result = await read(third)
assert.equal(result.status, 200)
assert.notEqual((await result.json()).pid, process.pid)
assert.ok(third.token.epoch > first.token.epoch)
console.log('Node selection session -> authenticated loopback host -> plugin child: PASS (two route labels, same backend)')
