// @vitest-environment node
import { Buffer } from 'node:buffer'
import { EventEmitter } from 'node:events'

import { describe, expect, it, vi } from 'vitest'

import { wireOauthSessionResponse } from './oauth-session-response'

class ResponseStream extends EventEmitter {
  statusCode = 200
  headers: Record<string, string | string[] | undefined> = { 'content-type': 'application/json' }
}

describe('bounded captured OAuth JSON transport', () => {
  it('counts bytes across chunks, aborts at the bound and never resolves a truncated result', () => {
    const response = new ResponseStream()
    const resolve = vi.fn()
    const reject = vi.fn()
    const abort = vi.fn()
    const clearTimer = vi.fn()
    wireOauthSessionResponse(response, { url: 'https://captured.test/api/plugins/demo/console/resource',
      isTimedOut: () => false, clearTimer, resolve, reject, abort, maxResponseBytes: 10 })
    response.emit('data', Buffer.alloc(9, 32))
    expect(reject).not.toHaveBeenCalled()
    response.emit('data', Buffer.from('xx'))
    response.emit('data', Buffer.alloc(100))
    response.emit('end')
    expect(reject).toHaveBeenCalledTimes(1)
    expect(reject.mock.calls[0][0].message).toBe('Bounded plugin response is too large')
    expect(abort).toHaveBeenCalledTimes(1)
    expect(clearTimer).toHaveBeenCalledTimes(1)
    expect(resolve).not.toHaveBeenCalled()
  })

  it('accepts an exact bound and leaves ordinary callers compatible without a resource allowance', () => {
    for (const maxResponseBytes of [11, undefined]) {
      const response = new ResponseStream()
      const resolve = vi.fn()
      const reject = vi.fn()
      wireOauthSessionResponse(response, { url: 'https://captured.test/api/status', isTimedOut: () => false,
        clearTimer: vi.fn(), resolve, reject, maxResponseBytes })
      response.emit('data', Buffer.from('{"ok":'))
      response.emit('data', Buffer.from('true}'))
      response.emit('end')
      expect(resolve).toHaveBeenCalledWith({ ok: true })
      expect(reject).not.toHaveBeenCalled()
    }
  })
})
