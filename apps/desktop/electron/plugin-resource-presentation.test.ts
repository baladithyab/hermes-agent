// @vitest-environment node
import { Buffer } from 'node:buffer'
import { promisify } from 'node:util'
import {
  brotliCompressSync, constants, deflateRawSync, deflateSync, gzip, gzipSync, zstdCompress, zstdCompressSync
} from 'node:zlib'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MAX_PLUGIN_RESOURCE_BODY, PLUGIN_RESOURCE_WRAPPER_PATH, redactPluginResourceUrls } from './plugin-resource-contract'
import type { PluginResourceBridgeRequest } from './plugin-resource-contract'
import { parseResourceBridgeRequest, PluginResourcePresentations, resourceResponse } from './plugin-resource-presentation'
import { formatRendererConsoleLine } from './renderer-log'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })

  return { promise, resolve }
}

const entry = '/v0/resource/plugins/demo/index.html'

const request = (): PluginResourceBridgeRequest => ({
  pluginId: 'hermes-cpa', documentId: 'cdce3827-ef75-4770-b081-491a58125eb2', grantId: 'G'.repeat(43),
  entryPath: entry, expiresAt: Date.now() / 1000 + 300,
  owner: { connectionId: 'captured-remote', profile: 'alpha' },
  selection: { client: 'client-with-sixteen-characters', scope: 'a'.repeat(32), epoch: 1 }
})

const native = (body: Buffer = Buffer.from('<script type="module" src="./module.js"></script>'), headers: [string, string][] = []) => ({
  api_version: 1, data: { ok: true, status: 200, headers: [['content-type', 'text/html'], ...headers], body_base64: body.toString('base64') }
})

const origin = (url: string): string => url.slice(0, url.indexOf(PLUGIN_RESOURCE_WRAPPER_PATH))
const compressGzip = promisify(gzip)
const compressZstd = promisify(zstdCompress)

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_000_000) })
afterEach(() => vi.useRealTimers())

describe('isolated native resource protocol', () => {
  it('serves a host ancestor wrapper without invoking native GET or injecting a console bridge', async () => {
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockResolvedValue(native())
    const opened = store.open(request(), 7, invoke, () => true)
    const response = await store.handle(new Request(opened.url))
    const html = await response.text()
    expect(html).toContain(`src="${origin(opened.url)}${entry}"`)
    expect(html).toContain('sandbox="allow-scripts allow-forms"')
    expect(html).not.toContain('allow-same-origin')
    expect(html).not.toContain('MessagePort')
    expect(response.headers.get('Content-Security-Policy')).toContain(`frame-src ${origin(opened.url)}`)
    expect(response.headers.get('Content-Security-Policy')).not.toContain("'self'")
    expect(response.headers.get('Referrer-Policy')).toBe('no-referrer')
    expect(invoke).not.toHaveBeenCalled()
  })

  it('preserves bytes and native query order, decodes paths once, and never forwards guest credentials', async () => {
    const store = new PluginResourcePresentations()
    const bytes = Buffer.from([0, 255, 17, 28])
    const invoke = vi.fn().mockResolvedValue(native(bytes, [['Set-Cookie', 'SECRET'], ['Authorization', 'SECRET']]))
    const opened = store.open(request(), 7, invoke, () => true)

    const response = await store.handle(new Request(`${origin(opened.url)}/v0/resource/plugins/demo/%E4%B8%AD%20%252e.js?profile=other&code=secret&x=&x=2`, {
      headers: { Authorization: 'Bearer GUEST', Cookie: 'GUEST', Accept: 'application/javascript', 'X-Hermes-Session-Token': 'GUEST' }
    }))

    expect(Buffer.from(await response.arrayBuffer())).toEqual(bytes)
    expect(invoke).toHaveBeenCalledExactlyOnceWith({ document_id: request().documentId, grant_id: request().grantId,
      path: '/v0/resource/plugins/demo/中 %2e.js', query: [['profile', 'other'], ['code', 'secret'], ['x', ''], ['x', '2']], accept: 'application/javascript' }, false)
    expect(response.headers.get('Set-Cookie')).toBeNull()
    expect(response.headers.get('Authorization')).toBeNull()
    expect(response.headers.get('Access-Control-Allow-Origin')).toBe('*')
    expect(response.headers.get('Content-Security-Policy')).toContain('sandbox allow-scripts allow-forms;')
    expect(response.headers.get('Access-Control-Allow-Credentials')).toBeNull()
  })

  it('denies unknown grants, arbitrary routes and methods without invoking native GET', async () => {
    const store = new PluginResourcePresentations()
    const invoke = vi.fn()
    const opened = store.open(request(), 7, invoke, () => true)

    for (const path of ['/api/config', '/login', '/v0/resource/plugins/other/index.html']) {
      expect((await store.handle(new Request(origin(opened.url) + path))).status).toBe(410)
    }

    expect((await store.handle(new Request('hermes-plugin-resource://p-' + 'b'.repeat(48) + entry))).status).toBe(410)

    for (const method of ['HEAD', 'POST', 'PUT', 'DELETE']) {
      expect((await store.handle(new Request(origin(opened.url) + entry, { method }))).status).toBe(405)
    }

    expect((await store.handle(new Request(origin(opened.url) + entry, { method: 'OPTIONS' }))).status).toBe(204)
    expect(invoke).not.toHaveBeenCalled()
  })

  it('withholds a completed response after close, never replays an uncertain effect, and rejects the wrong sender', async () => {
    const store = new PluginResourcePresentations()
    const gate = deferred<unknown>()
    const started = deferred<void>()

    const invoke = vi.fn(async (_body, close) => { if (close) {return {};} started.resolve();

 return gate.promise })

    const opened = store.open(request(), 7, invoke, () => true)
    await expect(store.close(opened.handleId, 8)).rejects.toThrow('RESOURCE_REVOKED')
    const pending = store.handle(new Request(origin(opened.url) + entry))
    await started.promise
    const closing = store.close(opened.handleId, 7)
    gate.resolve(native(Buffer.from('provider-secret')))
    expect((await pending).status).toBe(410)
    await closing
    expect(invoke.mock.calls.filter(([, close]) => !close)).toHaveLength(1)
    expect((await store.handle(new Request(opened.url))).status).toBe(410)
  })

  it('expires grants and suppresses bytes after the captured sender/document is retired', async () => {
    const store = new PluginResourcePresentations()
    let current = true
    const invoke = vi.fn().mockResolvedValue(native())
    const opened = store.open(request(), 7, invoke, () => current)
    current = false
    expect((await store.handle(new Request(origin(opened.url) + entry))).status).toBe(410)
    current = true
    await vi.advanceTimersByTimeAsync(300_001)
    expect((await store.handle(new Request(origin(opened.url) + entry))).status).toBe(410)
    expect(invoke).not.toHaveBeenCalled()
  })

  it.each([1.25, 7200.25])('uses a local five-minute lifetime despite owner expiry clock %s', async expiresAt => {
    const store = new PluginResourcePresentations()
    const opened = store.open({ ...request(), expiresAt }, 7, vi.fn(), () => true)
    expect((await store.handle(new Request(opened.url))).status).toBe(200)
    await vi.advanceTimersByTimeAsync(299_999)
    expect((await store.handle(new Request(opened.url))).status).toBe(200)
    await vi.advanceTimersByTimeAsync(2)
    expect((await store.handle(new Request(opened.url))).status).toBe(410)
  })

  it('bounds concurrent native invocations and pending requests without retry', async () => {
    const store = new PluginResourcePresentations()
    const gate = deferred<unknown>()
    const started = deferred<void>()

    const invoke = vi.fn(() => { started.resolve();

 return gate.promise })

    let current = true
    const opened = store.open(request(), 7, invoke, () => current)
    const requests = Array.from({ length: 16 }, () => store.handle(new Request(origin(opened.url) + entry)))
    await started.promise
    expect(invoke).toHaveBeenCalledTimes(1)
    expect((await store.handle(new Request(origin(opened.url) + entry))).status).toBe(429)
    current = false
    gate.resolve(native())
    expect((await Promise.all(requests)).every(response => response.status === 410)).toBe(true)
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it('expires queued GETs without invoking or replaying them, while close bypasses the queue', async () => {
    const store = new PluginResourcePresentations()
    const gate = deferred<unknown>()
    const started = deferred<void>()

    const invoke = vi.fn(async (_body, close) => {
      if (close) {return {}}
      started.resolve()

      return gate.promise
    })

    const opened = store.open(request(), 7, invoke, () => true)
    const first = store.handle(new Request(origin(opened.url) + entry))
    await started.promise
    const queued = store.handle(new Request(origin(opened.url) + entry))
    await vi.advanceTimersByTimeAsync(10_001)
    expect((await queued).status).toBe(429)
    await store.close(opened.handleId, 7)
    expect(invoke.mock.calls.filter(([, close]) => close)).toHaveLength(1)
    gate.resolve(native())
    expect((await first).status).toBe(410)
    expect(invoke.mock.calls.filter(([, close]) => !close)).toHaveLength(1)
  })

  it('fails closed after a transport exception without exposing query/capability details', async () => {
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockRejectedValue(new Error('code=SECRET&token=SECRET'))
    const opened = store.open(request(), 7, invoke, () => true)
    const response = await store.handle(new Request(origin(opened.url) + entry))
    expect(response.status).toBe(410)
    expect(await response.text()).toBe('Resource unavailable')
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it('rejects reserved paths and caller-selected authorities while accepting skewed owner clocks', () => {
    expect(() => parseResourceBridgeRequest({ ...request(), entryPath: PLUGIN_RESOURCE_WRAPPER_PATH })).toThrow()
    expect(() => parseResourceBridgeRequest({ ...request(), entryPath: 'https://evil.test/' })).toThrow()

    for (const expiresAt of [1, Date.now() / 1000 + 3600.25]) {
      expect(parseResourceBridgeRequest({ ...request(), expiresAt }).expiresAt).toBe(expiresAt)
    }

    for (const expiresAt of [0, -1, NaN, Infinity]) {expect(() => parseResourceBridgeRequest({ ...request(), expiresAt })).toThrow()}
    expect(() => parseResourceBridgeRequest({ ...request(), selection: { client: 'forged', epoch: 1 } })).toThrow()
  })
})

describe('resource transfer admission', () => {
  it('starts the next invocation without waiting for an unread body and times delivery out', async () => {
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockResolvedValue(native(Buffer.alloc(256 * 1024, 65)))
    const opened = store.open(request(), 7, invoke, () => true)
    const first = await store.handle(new Request(origin(opened.url) + entry))
    const second = await store.handle(new Request(origin(opened.url) + entry))
    expect(invoke).toHaveBeenCalledTimes(2)
    await second.body!.cancel()
    await vi.advanceTimersByTimeAsync(15_001)
    await expect(first.arrayBuffer()).rejects.toThrow('RESOURCE_REVOKED')
    const after = await store.handle(new Request(origin(opened.url) + entry))
    expect(after.status).toBe(200)
    await after.body!.cancel()
  })

  it('closing a resource cancels its active byte stream before owner cleanup', async () => {
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockImplementation(async (_body, close) => close ? {} : native(Buffer.alloc(256 * 1024)))
    const opened = store.open(request(), 7, invoke, () => true)
    const response = await store.handle(new Request(origin(opened.url) + entry))
    await store.close(opened.handleId, 7)
    await expect(response.arrayBuffer()).rejects.toThrow('RESOURCE_REVOKED')
    expect(invoke.mock.calls.filter(([, close]) => close)).toHaveLength(1)
  })

  it('preserves 256 query pairs, controls and values larger than eight KiB', async () => {
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockResolvedValue(native())
    const opened = store.open(request(), 7, invoke, () => true)
    const query = 'value=' + 'x'.repeat(9000) + '&empty=&control=%00%0A%0D&' + Array.from({ length: 253 }, () => 'x=').join('&')
    const response = await store.handle(new Request(origin(opened.url) + entry + '?' + query))
    expect(response.status).toBe(200)
    await response.arrayBuffer()
    expect(invoke.mock.calls[0][0].query).toHaveLength(256)
    expect(invoke.mock.calls[0][0].query[2]).toEqual(['control', '\0\n\r'])
  })
})

describe('native content decoding', () => {
  const resourceOrigin = 'hermes-plugin-resource://p-' + 'a'.repeat(48)

  it.each([
    { encoding: 'gzip', compress: gzipSync },
    { encoding: 'x-gzip', compress: gzipSync },
    { encoding: 'deflate', compress: deflateSync },
    { encoding: 'deflate', compress: deflateRawSync },
    { encoding: 'br', compress: brotliCompressSync },
    { encoding: 'zstd', compress: zstdCompressSync }
  ])('delivers actual script and JSON content for $encoding using $compress.name', async ({ encoding, compress }) => {
    for (const [contentType, content] of [
      ['application/javascript', 'document.documentElement.setAttribute("data-script", "decoded");'],
      ['application/json', '{"gzip":true,"generation":"1"}']
    ]) {
      const body = Buffer.from(content)

      const response = await resourceResponse(native(compress(body), [
        ['Content-Type', contentType], ['Content-Encoding', encoding], ['Content-Length', '1']
      ]), resourceOrigin, entry)

      expect(response.headers.get('Content-Encoding')).toBeNull()
      expect(response.headers.get('Content-Length')).toBe(String(body.length))
      expect(response.headers.get('Content-Type')).toBe(contentType)
      expect(await response.text()).toBe(content)
    }
  })

  it('decodes stacked/repeated encodings in reverse order and concatenated gzip members', async () => {
    const content = Buffer.from('{"generation":"1","gzip":true}')

    for (const headers of [
      [['Content-Encoding', ' GZip , br ']],
      [['Content-Encoding', 'gzip'], ['content-encoding', 'br']]
    ] satisfies [string, string][][]) {
      const response = await resourceResponse(native(brotliCompressSync(gzipSync(content)), headers), resourceOrigin, entry)
      expect(Buffer.from(await response.arrayBuffer())).toEqual(content)
    }

    const concatenated = Buffer.concat([gzipSync(content.subarray(0, 8)), gzipSync(content.subarray(8))])
    const response = await resourceResponse(native(concatenated, [['Content-Encoding', 'gzip']]), resourceOrigin, entry)
    expect(await response.json()).toEqual({ generation: '1', gzip: true })
    const identity = await resourceResponse(native(content, [['Content-Encoding', 'identity']]), resourceOrigin, entry)
    expect(await identity.json()).toEqual({ generation: '1', gzip: true })
  })

  it('decodes raw deflate that resembles a zlib header without replaying the native GET', async () => {
    const content = Buffer.alloc(156, 65)
    // Stored block: padding makes 0x78, LEN starts with 0x9c, then NLEN and data.
    // Those first two bytes also form a valid zlib header. Finish with an empty block.
    const body = Buffer.concat([Buffer.from([0x78, 0x9c, 0, 0x63, 0xff]), content, Buffer.from([3, 0])])
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockResolvedValue(native(body, [['Content-Encoding', 'deflate']]))
    const opened = store.open(request(), 7, invoke, () => true)
    const response = await store.handle(new Request(origin(opened.url) + entry))
    expect(response.status).toBe(200)
    expect(Buffer.from(await response.arrayBuffer())).toEqual(content)
    expect(invoke).toHaveBeenCalledTimes(1)
  })

  it('decodes zstd frames and raw deflate inside stacked codings', async () => {
    const content = Buffer.from('{"native":"decoded","binary":"\\u0000"}')
    const skippable = Buffer.from([0x50, 0x2a, 0x4d, 0x18, 3, 0, 0, 0, 1, 2, 3])

    const frames = Buffer.concat([
      skippable, zstdCompressSync(content.subarray(0, 8)), skippable, zstdCompressSync(content.subarray(8)), skippable
    ])

    // Split the next frame's magic across the outer codec's output chunks.
    const boundarySkip = Buffer.alloc(16 * 1024 - 2)
    boundarySkip.writeUInt32LE(0x184d2a50, 0)
    boundarySkip.writeUInt32LE(boundarySkip.length - 8, 4)

    for (const [body, encoding] of [
      [frames, 'zstd'],
      [brotliCompressSync(frames), 'zstd, br'],
      [gzipSync(Buffer.concat([boundarySkip, frames])), 'zstd, gzip'],
      [zstdCompressSync(deflateRawSync(content)), 'deflate, zstd']
    ] satisfies [Buffer, string][]) {
      const response = await resourceResponse(native(body, [['Content-Encoding', encoding]]), resourceOrigin, entry)
      expect(response.headers.get('Content-Encoding')).toBeNull()
      expect(Buffer.from(await response.arrayBuffer())).toEqual(content)
    }
  })

  it.each(['gzip', 'x-gzip', 'deflate', 'br', 'zstd', 'gzip, zstd, br', 'identity'])(
    'preserves coded-empty responses and redirects for %s', async encoding => {
      for (const status of [200, 204, 205, 301, 302, 303, 304, 307, 308]) {
        const redirect = status >= 300 && status !== 304
        const location = '/v0/resource/plugins/demo/next?x=1&x=2#tab'
        const headers: [string, string][] = [['Content-Encoding', encoding]]

        if (redirect) {headers.push(['Location', location])}
        const envelope = native(Buffer.alloc(0), headers)
        envelope.data.status = status
        const response = await resourceResponse(envelope, resourceOrigin, entry)
        expect(response.status).toBe(status)
        expect(response.headers.get('Content-Encoding')).toBeNull()
        expect(response.headers.get('Content-Length')).toBe('0')
        expect(response.headers.get('Location')).toBe(redirect ? location : null)
        expect(await response.text()).toBe('')
      }

      // An outer coding can also produce an empty body for the next layer.
      const nested = await resourceResponse(native(gzipSync(Buffer.alloc(0)), [
        ['Content-Encoding', `${encoding}, gzip`]
      ]), resourceOrigin, entry)

      expect(await nested.text()).toBe('')
    }
  )

  it('enforces the Chromium zstd window limit and standard Brotli window range', async () => {
    // RFC 8878: no content-size field, a window descriptor, one final raw block.
    // 0x68 requests 8 MiB; 0x69 requests 9 MiB, despite the tiny decoded body.
    for (const window of [0x68, 0x69]) {
      const frame = Buffer.concat([
        Buffer.from([0x28, 0xb5, 0x2f, 0xfd, 0, window, 0x29, 0, 0]), Buffer.from('plain')
      ])

      const decoding = resourceResponse(native(frame, [['Content-Encoding', 'zstd']]), resourceOrigin, entry)

      if (window === 0x68) {expect(await (await decoding).text()).toBe('plain')}
      else {await expect(decoding).rejects.toThrow('RESOURCE_RESPONSE_INVALID')}
    }

    for (const window of [24, 25]) {
      const body = brotliCompressSync(Buffer.from('plain'), { params: {
        [constants.BROTLI_PARAM_LGWIN]: window,
        [constants.BROTLI_PARAM_LARGE_WINDOW]: window > 24 ? 1 : 0
      } })

      const decoding = resourceResponse(native(body, [['Content-Encoding', 'br']]), resourceOrigin, entry)

      if (window === 24) {expect(await (await decoding).text()).toBe('plain')}
      else {await expect(decoding).rejects.toThrow('RESOURCE_RESPONSE_INVALID')}
    }
  })

  it('rejects truncated, corrupt and trailing coded bytes before exposing a response', async () => {
    const content = Buffer.from('a valid prefix must not escape')
    const checkedZstd = zstdCompressSync(content, { params: { [constants.ZSTD_c_checksumFlag]: 1 } })
    const corruptZstd = Buffer.from(checkedZstd)
    corruptZstd[corruptZstd.length - 1] ^= 255
    const corruptDeflate = deflateSync(content)
    corruptDeflate[corruptDeflate.length - 1] ^= 255
    const bodies: [Buffer, string][] = [[corruptZstd, 'zstd'], [corruptDeflate, 'deflate']]

    for (let end = 1; end < checkedZstd.length; end++) {bodies.push([checkedZstd.subarray(0, end), 'zstd'])}

    for (const [body, encoding] of [
      [gzipSync(content), 'gzip'], [deflateSync(content), 'deflate'],
      [deflateRawSync(content), 'deflate'], [brotliCompressSync(content), 'br'],
      [checkedZstd, 'zstd']
    ] satisfies [Buffer, string][]) {
      bodies.push([body.subarray(0, -1), encoding], [Buffer.concat([body, Buffer.from([255])]), encoding])
    }

    const incompleteSkip = Buffer.from([0x50, 0x2a, 0x4d, 0x18, 2, 0, 0, 0, 1])
    bodies.push([Buffer.concat([checkedZstd, incompleteSkip]), 'zstd'])
    bodies.push([Buffer.concat([checkedZstd, checkedZstd.subarray(0, -1)]), 'zstd'])

    for (const [body, encoding] of bodies) {
      await expect(resourceResponse(native(body, [['Content-Encoding', encoding]]), resourceOrigin, entry))
        .rejects.toThrow('RESOURCE_RESPONSE_INVALID')
    }
  })

  it('rejects unsupported, truncated, corrupt and oversized encodings without replaying a native GET', async () => {
    const compressed = gzipSync(Buffer.from('{"ok":true}'))
    const corrupt = Buffer.from(compressed)
    corrupt[corrupt.length - 8] ^= 255
    const oversized = await compressGzip(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY + 1))

    for (const [body, encoding] of [
      [compressed, 'compress'], [compressed, 'constructor'], [compressed, 'gzip, unknown'],
      [compressed, 'gzip,'], [compressed.subarray(0, -1), 'gzip'], [corrupt, 'gzip'],
      [Buffer.from('not a gzip stream'), 'gzip'], [oversized, 'gzip']
    ] satisfies [Buffer, string][]) {
      const store = new PluginResourcePresentations()
      const invoke = vi.fn().mockResolvedValueOnce(native(body, [['Content-Encoding', encoding]])).mockResolvedValue(native())
      const opened = store.open(request(), 7, invoke, () => true)
      const failed = await store.handle(new Request(origin(opened.url) + entry))
      expect(failed.status).toBe(410)
      expect(await failed.text()).toBe('Resource unavailable')
      expect(invoke).toHaveBeenCalledTimes(1)
      const next = await store.handle(new Request(origin(opened.url) + entry))
      expect(next.status).toBe(200)
      await next.body!.cancel()
      expect(invoke).toHaveBeenCalledTimes(2)
    }
  })

  it.each([
    { encoding: 'gzip', compress: compressGzip },
    { encoding: 'zstd', compress: compressZstd }
  ])('accepts exactly 32 MiB decoded with $encoding and rejects one extra byte', async ({ encoding, compress }) => {
    const exact = await resourceResponse(native(await compress(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY, 65)), [
      ['Content-Encoding', encoding]
    ]), resourceOrigin, entry)

    expect((await exact.arrayBuffer()).byteLength).toBe(MAX_PLUGIN_RESOURCE_BODY)
    await expect(resourceResponse(native(await compress(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY + 1, 65)), [
      ['Content-Encoding', encoding]
    ]), resourceOrigin, entry)).rejects.toThrow('RESOURCE_RESPONSE_INVALID')
  })

  it('bounds an intermediate coding even when the final decoded body is small', async () => {
    const inner = gzipSync(Buffer.from('small final body'))
    const header = Buffer.from(inner.subarray(0, 10))
    header[3] |= 8 // A legal gzip filename, terminated before the compressed body.
    const intermediate = Buffer.concat([header, Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY, 65), Buffer.from([0]), inner.subarray(10)])
    const outer = await compressGzip(intermediate)
    await expect(resourceResponse(native(outer, [['Content-Encoding', 'gzip, gzip']]), resourceOrigin, entry))
      .rejects.toThrow('RESOURCE_RESPONSE_INVALID')
  })

  it.each([
    { encoding: 'identity', compress: async (body: Buffer) => body },
    { encoding: 'gzip', compress: compressGzip },
    { encoding: 'zstd', compress: compressZstd }
  ])('does not let an unread 24 MiB $encoding body starve an ordinary request', async ({ encoding, compress }) => {
    const large = native(await compress(Buffer.alloc(24 * 1024 * 1024, 65)), [['Content-Encoding', encoding]])
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockResolvedValueOnce(large).mockResolvedValue(native(Buffer.from('ordinary')))
    const opened = store.open(request(), 7, invoke, () => true)
    const first = await store.handle(new Request(origin(opened.url) + entry))
    expect(first.status).toBe(200)
    expect(first.headers.get('Content-Length')).toBe(String(24 * 1024 * 1024))
    const pending = store.handle(new Request(origin(opened.url) + entry))
    await vi.advanceTimersByTimeAsync(10_001)
    const ordinary = await pending
    expect(ordinary.status).toBe(200)
    expect(await ordinary.text()).toBe('ordinary')
    expect(invoke).toHaveBeenCalledTimes(2)
    await first.body!.cancel()
    const after = await store.handle(new Request(origin(opened.url) + entry))
    expect(await after.text()).toBe('ordinary')
    expect(invoke).toHaveBeenCalledTimes(3)
  })

  it('bounds concurrent unread bodies and admits again after cancellation without replay', async () => {
    const bytes = 24 * 1024 * 1024
    const store = new PluginResourcePresentations()
    const invoke = vi.fn().mockResolvedValue(native(Buffer.alloc(bytes, 65)))
    const opened = store.open(request(), 7, invoke, () => true)
    const pending = Promise.all(Array.from({ length: 9 }, () => store.handle(new Request(origin(opened.url) + entry))))
    await vi.advanceTimersByTimeAsync(10_001)
    const responses = await pending
    const admitted = responses.filter(response => response.status === 200)
    expect(admitted.length).toBeGreaterThan(0)
    expect(admitted.length * bytes).toBeLessThanOrEqual(192 * 1024 * 1024)
    expect(responses.some(response => response.status === 429)).toBe(true)
    expect(responses.every(response => response.status === 200 || response.status === 429)).toBe(true)
    expect(invoke).toHaveBeenCalledTimes(admitted.length)
    await Promise.all(admitted.map(response => response.body!.cancel()))
    const after = await store.handle(new Request(origin(opened.url) + entry))
    expect(after.status).toBe(200)
    await after.body!.cancel()
    expect(invoke).toHaveBeenCalledTimes(admitted.length + 1)
  })

  it.each([
    { encoding: 'gzip', compress: compressGzip },
    { encoding: 'zstd', compress: compressZstd }
  ])('closing during $encoding decoding withholds the result and invokes owner cleanup once', async ({ encoding, compress }) => {
    const compressed = await compress(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY, 65))
    const store = new PluginResourcePresentations()
    const received = deferred<void>()
    let invoked = false

    const invoke = vi.fn(async (_body, close) => {
      if (close) {return {}}
      invoked = true

      return native(compressed, [['Content-Encoding', encoding]])
    })

    const opened = store.open(request(), 7, invoke, () => {
      if (invoked) {received.resolve()}

      return true
    })

    const pending = store.handle(new Request(origin(opened.url) + entry))
    await received.promise
    await store.close(opened.handleId, 7)
    const response = await pending
    expect(response.status).toBe(410)
    expect(await response.text()).toBe('Resource unavailable')
    expect(invoke.mock.calls.filter(([, close]) => !close)).toHaveLength(1)
    expect(invoke.mock.calls.filter(([, close]) => close)).toHaveLength(1)
  })

  it.each([
    { encoding: 'gzip', compress: compressGzip },
    { encoding: 'zstd', compress: compressZstd }
  ])('bounds the $encoding decoding lifetime before exposing any response bytes', async ({ encoding, compress }) => {
    const compressed = await compress(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY, 65))
    const decoding = resourceResponse(native(compressed, [['Content-Encoding', encoding]]), resourceOrigin, entry)
    const rejected = expect(decoding).rejects.toThrow('RESOURCE_REVOKED')
    vi.advanceTimersByTime(15_001)
    await rejected
  })

  it.each([
    { encoding: 'gzip', compress: compressGzip },
    { encoding: 'zstd', compress: compressZstd }
  ])('aborts asynchronous $encoding decoding and revokes an unread decoded body', async ({ encoding, compress }) => {
    const compressed = await compress(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY, 65))
    const controller = new AbortController()
    const decoding = resourceResponse(native(compressed, [['Content-Encoding', encoding]]), resourceOrigin, entry, () => true, controller.signal)
    controller.abort()
    await expect(decoding).rejects.toThrow('RESOURCE_REVOKED')
    const store = new PluginResourcePresentations()
    const invoke = vi.fn(async (_body, close) => close ? {} : native(compressed, [['Content-Encoding', encoding]]))
    const opened = store.open(request(), 7, invoke, () => true)
    const response = await store.handle(new Request(origin(opened.url) + entry))
    await store.close(opened.handleId, 7)
    await expect(response.arrayBuffer()).rejects.toThrow('RESOURCE_REVOKED')
    expect(invoke.mock.calls.filter(([, close]) => !close)).toHaveLength(1)
    expect(invoke.mock.calls.filter(([, close]) => close)).toHaveLength(1)
  })

  it.each(['abort', 'deadline'])('interrupts zstd frame validation on %s', async stop => {
    const frame = zstdCompressSync(Buffer.alloc(0))
    const body = Buffer.concat(Array.from({ length: 2048 }, () => frame))
    const controller = new AbortController()
    const decoding = resourceResponse(native(body, [['Content-Encoding', 'zstd']]), resourceOrigin, entry, () => true, controller.signal)
    const rejected = expect(decoding).rejects.toThrow('RESOURCE_REVOKED')

    if (stop === 'abort') {controller.abort()}
    else {vi.advanceTimersByTime(15_001)}

    await rejected
  })

  it('honors revocation before exposing identity or coded-empty bytes', async () => {
    const controller = new AbortController()
    controller.abort()

    for (const [body, headers] of [
      [Buffer.from('identity'), []],
      [Buffer.from('identity'), [['Content-Encoding', 'identity']]],
      [Buffer.alloc(0), [['Content-Encoding', 'gzip']]]
    ] satisfies [Buffer, [string, string][]][]) {
      await expect(resourceResponse(native(body, headers), resourceOrigin, entry, () => true, controller.signal))
        .rejects.toThrow('RESOURCE_REVOKED')
      await expect(resourceResponse(native(body, headers), resourceOrigin, entry, () => false))
        .rejects.toThrow('RESOURCE_REVOKED')
    }
  })
})

describe('bounded resource envelopes and safe diagnostics', () => {
  const resourceOrigin = 'hermes-plugin-resource://p-' + 'a'.repeat(48)
  it('preserves approved path-reference redirects without following or normalizing them', async () => {
    const response = await resourceResponse(native(Buffer.alloc(0), [['Location', '/v0/resource/plugins/demo/a//other.js?code=secret#tab']]), resourceOrigin, entry)
    expect(response.headers.get('Location')).toBe('/v0/resource/plugins/demo/a//other.js?code=secret#tab')

    for (const location of ['./other.js', 'https://evil.test/', '//evil.test/', '/api/config', '/v0/resource/plugins/other/a']) {
      await expect(resourceResponse(native(Buffer.alloc(0), [['Location', location]]), resourceOrigin, entry)).rejects.toThrow()
    }
  })
  it('accepts the exact native byte boundary and rejects malformed or oversized payloads', async () => {
    const exact = Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY, 65)
    expect((await (await resourceResponse(native(exact), resourceOrigin, entry)).arrayBuffer()).byteLength).toBe(MAX_PLUGIN_RESOURCE_BODY)
    await expect(resourceResponse(native(Buffer.alloc(MAX_PLUGIN_RESOURCE_BODY + 1)), resourceOrigin, entry)).rejects.toThrow()

    for (const encoded of ['!AAA', 'A===', 'AA=A', 'A']) {
      const envelope = native()
      envelope.data.body_base64 = encoded
      await expect(resourceResponse(envelope, resourceOrigin, entry)).rejects.toThrow('RESOURCE_RESPONSE_INVALID')
    }
  })
  it('drops third-party console messages and redacts resource URL secrets in host diagnostics', () => {
    const url = resourceOrigin + entry + '?code=SECRET#TOKEN'
    expect(formatRendererConsoleLine('main', { level: 'error', sourceId: url, message: 'provider SECRET', lineNumber: 1 })).toBeNull()
    expect(formatRendererConsoleLine('main', {
      level: 'error', sourceId: 'file:///app/spoofed-source.js', message: 'provider SECRET', lineNumber: 1,
      frame: { url: 'about:blank', parent: { url, parent: null } }
    })).toBeNull()
    const line = redactPluginResourceUrls('Failed ' + url + ' after navigation')
    expect(line).toBe('Failed [plugin-resource] after navigation')
    expect(line).not.toContain('SECRET')
    expect(line).not.toContain('a'.repeat(48))
  })
})
