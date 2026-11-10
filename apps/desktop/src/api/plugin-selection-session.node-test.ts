import assert from 'node:assert/strict'
import { test } from 'node:test'

import { PluginSelectionSession } from './plugin-selection-session.js'

const owner = (profile: string) => ({ profile, connectionId: 'local' })
const tick = () => new Promise(resolve => setImmediate(resolve))

test('switch invalidates queued reads and deselects old owner before selecting new', async () => {
  const calls: string[] = []
  let release!: () => void
  const held = new Promise<void>(resolve => { release = resolve })

  const session = new PluginSelectionSession(async (o, path, token) => {
    calls.push(`${o.profile}:${path}:${token.epoch}`)

    if (calls.length === 1) {await held}

    return { scope_id: 'a'.repeat(32), epoch: token.epoch + 1 }
  }, () => 'client_identity_1234')

  session.change(owner('A'))
  const stale = session.acquire()
  const refused = assert.rejects(stale, /STALE_SCOPE/)
  await tick()
  session.change(owner('B'))
  release()
  await refused
  const current = await session.acquire()
  assert.equal(current.owner.profile, 'B')
  assert.deepEqual(calls, ['A:selection:0', 'A:deselection:1', 'B:selection:0'])
  assert.equal(session.current(current), true)
  session.change(owner('A'))
  assert.equal(session.current(current), false)
  const back = await session.acquire()
  assert.equal(back.token.epoch, 3)
  assert.deepEqual(calls.slice(3), ['B:deselection:1', 'A:selection:2'])
})

test('lost deselection acknowledgment latches closed, never selects another host', async () => {
  const calls: string[] = []

  const session = new PluginSelectionSession(async (o, path, token) => {
    calls.push(`${o.profile}:${path}`)

    if (path === 'deselection') {throw new Error('network lost')}

    return { scope_id: 'b'.repeat(32), epoch: token.epoch + 1 }
  }, () => 'client_identity_1234')

  session.change(owner('A'))
  await session.acquire()
  session.change(owner('B'))
  await assert.rejects(session.acquire(), /network lost/)
  session.change(owner('A'))
  await assert.rejects(session.acquire(), /network lost/)
  assert.deepEqual(calls, ['A:selection', 'A:deselection'])
})

test('late responses and rapid ABA are invalid even before switch acknowledgment', async () => {
  const session = new PluginSelectionSession(async (_o, _p, token) => ({
    scope_id: 'c'.repeat(32), epoch: token.epoch + 1
  }), () => 'client_identity_1234')

  session.change(owner('A'))
  const original = await session.acquire()
  session.change(owner('B'))
  session.change(owner('A'))
  assert.equal(session.current(original), false)
  const next = await session.acquire()
  assert.equal(next.owner.profile, 'A')
  assert.notEqual(original.token.epoch, next.token.epoch)
})
