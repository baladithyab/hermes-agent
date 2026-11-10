import assert from 'node:assert/strict'
import { test } from 'node:test'
import { pluginSelectionHeaders } from './plugin-selection.js'

test('selection bridge permits only comparison tokens on plugin routes', () => {
  assert.deepEqual(pluginSelectionHeaders('/api/status'), {})
  const token = { client: 'generated-window-id', scope: 'a'.repeat(32), epoch: 7 }
  assert.deepEqual(pluginSelectionHeaders('/api/plugins/hermes-cpa/config', token), {
    'X-Hermes-Plugin-Client': token.client,
    'X-Hermes-Plugin-Scope': token.scope,
    'X-Hermes-Plugin-Epoch': '7'
  })
  for (const invalid of [{ ...token, epoch: true }, { ...token, epoch: -1 },
    { ...token, client: 'injected\r\nheader' }, { ...token, scope: 'bad' }]) {
    assert.throws(() => pluginSelectionHeaders('/api/plugins/hermes-cpa/config', invalid as never))
  }
  assert.throws(() => pluginSelectionHeaders('/api/status', token))
})
