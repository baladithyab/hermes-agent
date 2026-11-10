import { Buffer } from 'node:buffer'
import { randomBytes } from 'node:crypto'
import { Readable, Writable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { setImmediate as yieldDecode } from 'node:timers/promises'
import { constants, createBrotliDecompress, createGunzip, createInflate, createInflateRaw, createZstdDecompress, Inflate } from 'node:zlib'
import type { BrotliDecompress, Gunzip, InflateRaw, ZstdDecompress } from 'node:zlib'

import { pluginSelectionHeaders } from './plugin-selection'
import {
  isPluginResourceUrl, MAX_PLUGIN_RESOURCE_BODY, PLUGIN_RESOURCE_SCHEME, PLUGIN_RESOURCE_WRAPPER_PATH
} from './plugin-resource-contract'
import type { PluginResourceBridgeRequest, PluginResourceBridgeResult } from './plugin-resource-contract'

export type ResourceInvoker = (body: Record<string, unknown>, close: boolean) => Promise<unknown>
interface Presentation {
  request: PluginResourceBridgeRequest
  senderId: number
  origin: string
  invoke: ResourceInvoker
  current: () => boolean
  timer: NodeJS.Timeout
  deadline: number
  active: Set<NativeResourceResponse | AbortController>
}
const ROOT = /^\/v0\/resource\/plugins\/[A-Za-z0-9_-]+\//
const SAFE_HEADERS = new Set(['content-type', 'content-encoding', 'content-disposition', 'content-language', 'etag', 'last-modified'])
const ZSTD_WINDOW_LOG_MAX = 23
// One codec runs at a time: Brotli <= 16 MiB, capped zstd <= 8 MiB, zlib <= 32 KiB.
const MAX_CODEC_WINDOW = 16 * 1024 * 1024
// Brotli's three Huffman groups need < 3 MiB. The remainder covers codec state,
// headers, stream queues and chunk views, coalesced into the codec's output slabs.
const DECODE_METADATA_ALLOWANCE = 4 * 1024 * 1024
const CONTENT_DECODERS: Readonly<Record<string,
  ((chunks: Buffer[]) => Gunzip | Inflate | InflateRaw | BrotliDecompress | ZstdDecompress) | undefined
>> = {
  gzip: () => createGunzip(),
  'x-gzip': () => createGunzip(),
  deflate: createDeflateDecoder,
  br: () => createBrotliDecompress({ params: {
    [constants.BROTLI_DECODER_PARAM_LARGE_WINDOW]: 0,
    // Otherwise a resize briefly retains both the old and new ring buffers.
    [constants.BROTLI_DECODER_PARAM_DISABLE_RING_BUFFER_REALLOCATION]: 1
  } }),
  zstd: () => createZstdDecompress({ params: { [constants.ZSTD_d_windowLogMax]: ZSTD_WINDOW_LOG_MAX } })
}


export function validateResourcePath(path: unknown): string {
  if (typeof path !== 'string' || path.length > 4096 || !ROOT.test(path) || /[\\?#\u0000-\u001f\u007f]/.test(path)) {
    throw new Error('RESOURCE_PATH_DENIED')
  }
  const decoded = decodeURIComponent(path)
  if (/[\\\u0000-\u001f\u007f]/.test(decoded) || decoded.split('/').some(part => part === '.' || part === '..')) {
    throw new Error('RESOURCE_PATH_DENIED')
  }
  return path
}

export function parseResourceBridgeRequest(raw: unknown, _now = Date.now()): PluginResourceBridgeRequest {
  if (!raw || typeof raw !== 'object') throw new Error('RESOURCE_REQUEST_INVALID')
  const request = raw as PluginResourceBridgeRequest
  if (typeof request.pluginId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(request.pluginId) ||
      typeof request.documentId !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(request.documentId) ||
      typeof request.grantId !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(request.grantId) ||
      !Number.isFinite(request.expiresAt) || request.expiresAt <= 0 ||
      !request.owner || typeof request.owner !== 'object' || !request.selection) {
    throw new Error('RESOURCE_REQUEST_INVALID')
  }
  for (const value of [request.owner.connectionId, request.owner.profile]) {
    if (value !== null && (typeof value !== 'string' || !value || value.length > 128 || /[\/\\\u0000-\u001f]/.test(value))) {
      throw new Error('RESOURCE_REQUEST_INVALID')
    }
  }
  pluginSelectionHeaders(`/api/plugins/${request.pluginId}/console/resource`, request.selection)
  if (!request.selection.scope) throw new Error('RESOURCE_SELECTION_REQUIRED')
  return { pluginId: request.pluginId, sessionId: request.sessionId, documentId: request.documentId, grantId: request.grantId,
    entryPath: validateResourcePath(request.entryPath), expiresAt: request.expiresAt,
    owner: { ...request.owner }, selection: { ...request.selection } }
}

function headersFor(origin: string, wrapper = false): Headers {
  return new Headers({
    'Content-Security-Policy': `sandbox allow-scripts allow-forms; default-src 'none'; base-uri 'none'; object-src 'none'; worker-src 'none'; script-src 'unsafe-inline' ${origin}; style-src 'unsafe-inline' ${origin}; img-src data: blob: ${origin}; font-src ${origin}; media-src ${origin}; connect-src ${origin}; form-action ${origin}; frame-src ${wrapper ? origin : "'none'"}`,
    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
    'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Accept', Vary: 'Origin'
  })
}

/** Match Chromium's acceptance of raw as well as zlib-wrapped deflate. */
function createDeflateDecoder(chunks: Buffer[]): Inflate | InflateRaw {
  let header = 0
  let bytes = 0
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length && bytes < 2; i++, bytes++) header = (header << 8) | chunk[i]
    if (bytes === 2) break
  }
  const method = header >> 8
  return bytes === 2 && (method & 15) === 8 && (method >> 4) <= 7 && header % 31 === 0
    ? createInflate() : createInflateRaw()
}

/** Node 24.15 needs frame-aligned writes and does not reject unfinished zstd frames.
 * Validate RFC 8878 boundaries without copying payloads; the codec validates
 * block contents, checksums, dictionaries and the window limit.
 * https://github.com/nodejs/node/blob/v24.15.0/src/node_zlib.cc */
async function* validatedZstdChunks(chunks: Buffer[], checkCurrent: () => void): AsyncGenerator<Buffer> {
  let remaining = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  let index = 0
  let offset = 0
  const read = (bytes: number): number => {
    if (bytes > remaining) throw new Error('RESOURCE_RESPONSE_INVALID')
    let value = 0
    for (let i = 0; i < bytes; i++) {
      while (offset === chunks[index].length) { index++; offset = 0 }
      value += chunks[index][offset++] * 2 ** (8 * i)
    }
    remaining -= bytes
    return value
  }
  const skip = (bytes: number) => {
    if (bytes > remaining) throw new Error('RESOURCE_RESPONSE_INVALID')
    remaining -= bytes
    while (bytes > 0) {
      const take = Math.min(bytes, chunks[index].length - offset)
      offset += take
      bytes -= take
      if (offset === chunks[index].length) { index++; offset = 0 }
    }
  }
  let steps = 0
  while (remaining > 0) {
    // Even a body made entirely of tiny/skippable frames must let close and
    // the shared 15 s decode deadline interrupt validation.
    if (++steps % 1024 === 0) { await yieldDecode(); checkCurrent() }
    let frameIndex = index
    let frameOffset = offset
    const magic = read(4)
    if (magic >= 0x184d2a50 && magic <= 0x184d2a5f) {
      skip(read(4))
    } else {
      if (magic !== 0xfd2fb528) throw new Error('RESOURCE_RESPONSE_INVALID')
      const descriptor = read(1)
      const singleSegment = (descriptor & 32) !== 0
      const contentSizeFlag = descriptor >> 6
      const dictionaryFlag = descriptor & 3
      skip((singleSegment ? 0 : 1) + (dictionaryFlag === 0 ? 0 : 2 ** (dictionaryFlag - 1)) +
        (contentSizeFlag === 0 ? Number(singleSegment) : 2 ** contentSizeFlag))
      let last = false
      while (!last) {
        if (++steps % 1024 === 0) { await yieldDecode(); checkCurrent() }
        const block = read(3)
        const type = (block >> 1) & 3
        if (type === 3) throw new Error('RESOURCE_RESPONSE_INVALID')
        last = (block & 1) !== 0
        skip(type === 1 ? 1 : block >> 3)
      }
      if (descriptor & 4) skip(4)
    }
    while (frameIndex < index || (frameIndex === index && frameOffset < offset)) {
      const chunk = chunks[frameIndex]
      const end = frameIndex === index ? offset : chunk.length
      if (end > frameOffset) {
        yield frameOffset === 0 && end === chunk.length ? chunk : chunk.subarray(frameOffset, end)
      }
      frameIndex++
      frameOffset = 0
    }
  }
}

/** Electron protocol.handle consumes Response bytes without HTTP content decoding.
 * Decode before exposing any body so corrupt/oversized encodings cannot deliver a prefix. */
async function decodeResourceBody(
  chunks: Buffer[], encoding: string | null, current: () => boolean, signal?: AbortSignal
): Promise<Buffer[]> {
  const encodings = encoding === null ? [] : encoding.toLowerCase().split(',').map(value => value.trim())
  if (encodings.some(value => value !== 'identity' && !Object.hasOwn(CONTENT_DECODERS, value))) {
    throw new Error('RESOURCE_RESPONSE_INVALID')
  }
  const codings = encodings.filter(value => value !== 'identity')
  // Identity and coded-empty bodies need neither codec workspace nor a decode
  // timer. In particular, an empty coded redirect must remain a redirect.
  if (!codings.length || chunks.every(chunk => chunk.length === 0)) {
    try {
      if (!signal?.aborted && current()) return chunks
    } catch { /* A retired owner cannot expose even an identity body. */ }
    throw new Error('RESOURCE_REVOKED')
  }
  const lifetime = new AbortController()
  const abort = () => lifetime.abort()
  const checkCurrent = () => {
    try {
      if (!lifetime.signal.aborted && current()) return
    } catch { /* A retired owner is never a reason to expose buffered bytes. */ }
    abort()
    throw new Error('RESOURCE_REVOKED')
  }
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
  const timer = setTimeout(abort, 15_000)
  timer.unref?.()
  try {
    checkCurrent()
    for (const coding of codings.reverse()) {
      const inputSize = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
      if (inputSize === 0) break
      checkCurrent()
      let decoder = CONTENT_DECODERS[coding]!(chunks)
      const decoded: Buffer[] = []
      let size = 0
      for (;;) {
        try {
          const input = coding === 'zstd' ? validatedZstdChunks(chunks, checkCurrent) : chunks
          await pipeline(Readable.from(input), decoder, new Writable({
            write(chunk: Buffer, _encoding, callback) {
              try {
                checkCurrent()
                size += chunk.length
                if (size > MAX_PLUGIN_RESOURCE_BODY) throw new Error('RESOURCE_RESPONSE_INVALID')
                // Node emits adjacent slices of an output slab on short writes.
                // Keep one view per slab so stacked codings cannot multiply metadata.
                const previous = decoded.at(-1)
                if (previous && previous.buffer === chunk.buffer && previous.byteOffset + previous.length === chunk.byteOffset) {
                  decoded[decoded.length - 1] = Buffer.from(chunk.buffer, previous.byteOffset, previous.length + chunk.length)
                } else {
                  decoded.push(chunk)
                }
                callback()
              } catch (error) {
                callback(error instanceof Error ? error : new Error('RESOURCE_RESPONSE_INVALID'))
              }
            }
          }), { signal: lifetime.signal })
          break
        } catch (error) {
          checkCurrent()
          // A legal raw stored block can begin with bytes resembling a zlib
          // header. Chromium falls back before output, not after decoding starts.
          // Only buffered bytes are retried; the native GET is never replayed.
          if (coding === 'deflate' && decoder instanceof Inflate && size === 0 &&
              error instanceof Error && 'code' in error && error.code === 'Z_DATA_ERROR') {
            decoder = createInflateRaw()
            continue
          }
          throw error
        }
      }
      // Some codecs otherwise accept a valid prefix followed by unused bytes.
      if (decoder.bytesWritten !== inputSize) throw new Error('RESOURCE_RESPONSE_INVALID')
      chunks = decoded
    }
    checkCurrent()
    return chunks
  } catch {
    throw new Error(lifetime.signal.aborted ? 'RESOURCE_REVOKED' : 'RESOURCE_RESPONSE_INVALID')
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

/** Keep the native buffer admitted until the protocol reader drains/cancels it.
 * A small pull queue avoids accumulating full responses for slow guests. */
class NativeResourceResponse extends Response {
  readonly completed: Promise<void>
  readonly abort: () => void

  constructor(chunks: Buffer[], status: number, headers: Headers, current: () => boolean) {
    let resolveCompleted!: () => void
    const completed = new Promise<void>(resolve => { resolveCompleted = resolve })
    let pending: Buffer[] | undefined = chunks
    let index = 0
    let offset = 0
    let controller: ReadableStreamDefaultController<Uint8Array> | undefined
    let finished = false
    let timer: NodeJS.Timeout | undefined
    const finish = () => {
      finished = true
      clearTimeout(timer)
      pending = undefined
      resolveCompleted()
    }
    const abort = () => {
      if (finished) return
      controller?.error(new Error('RESOURCE_REVOKED'))
      finish()
    }
    const stream = new ReadableStream<Uint8Array>({
      start(value) { controller = value },
      pull(value) {
        try {
          if (!current()) { abort(); return }
        } catch { abort(); return }
        if (!pending || index === pending.length) { value.close(); finish(); return }
        const chunk = pending[index]
        const end = Math.min(offset + 65536, chunk.length)
        value.enqueue(Uint8Array.from(chunk.subarray(offset, end)))
        offset = end
        if (offset === chunk.length) {
          index += 1
          offset = 0
        }
        if (index === pending.length) {
          value.close()
          finish()
        }
      },
      cancel() { finish() }
    })
    super(stream, { status, headers })
    timer = setTimeout(abort, 15_000)
    timer.unref?.()
    this.completed = completed
    this.abort = abort
  }
}

export async function resourceResponse(
  envelope: unknown, origin: string, path: string, current: () => boolean = () => true, signal?: AbortSignal
): Promise<Response> {
  if (!envelope || typeof envelope !== 'object' || !('api_version' in envelope) || envelope.api_version !== 1 || !('data' in envelope)) {
    throw new Error('RESOURCE_RESPONSE_INVALID')
  }
  const value = envelope.data
  if (!value || typeof value !== 'object') throw new Error('RESOURCE_RESPONSE_INVALID')
  const data = value as { ok?: unknown; status?: unknown; headers?: unknown; body_base64?: unknown }
  if (data.ok !== true || !Number.isInteger(data.status) || typeof data.status !== 'number' || data.status < 200 || data.status > 599 ||
      typeof data.body_base64 !== 'string' || data.body_base64.length > 4 * Math.ceil(MAX_PLUGIN_RESOURCE_BODY / 3) ||
      data.body_base64.length % 4 !== 0 || /[^A-Za-z0-9+/=]/.test(data.body_base64)) {
    throw new Error('RESOURCE_RESPONSE_INVALID')
  }
  const padding = data.body_base64.endsWith('==') ? 2 : data.body_base64.endsWith('=') ? 1 : 0
  if (data.body_base64.indexOf('=') !== (padding ? data.body_base64.length - padding : -1)) {
    throw new Error('RESOURCE_RESPONSE_INVALID')
  }
  let chunks: Buffer[] = [Buffer.from(data.body_base64, 'base64')]
  const noBody = [204, 205, 304].includes(data.status)
  if (chunks[0].length > MAX_PLUGIN_RESOURCE_BODY || (noBody && chunks[0].length)) throw new Error('RESOURCE_RESPONSE_INVALID')
  const entries = Array.isArray(data.headers) ? data.headers :
    data.headers && typeof data.headers === 'object' ? Object.entries(data.headers) : null
  if (!entries || entries.length > 64) throw new Error('RESOURCE_RESPONSE_INVALID')
  const headers = headersFor(origin)
  let budget = 0
  for (const pair of entries) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== 'string' || typeof pair[1] !== 'string' ||
        /[\u0000-\u001f\u007f]/.test(pair[0] + pair[1])) throw new Error('RESOURCE_RESPONSE_INVALID')
    const [name, value] = pair
    budget += name.length + value.length
    if (budget > 128 * 1024) throw new Error('RESOURCE_RESPONSE_INVALID')
    const key = name.toLowerCase()
    if (key === 'content-encoding') headers.append(key, value)
    else if (SAFE_HEADERS.has(key)) headers.set(key, value)
    else if (key === 'location') {
      if (!value.startsWith('/') || value.startsWith('//') ||
          !validateResourcePath(value.split(/[?#]/, 1)[0]).startsWith(ROOT.exec(path)![0])) {
        throw new Error('RESOURCE_REDIRECT_DENIED')
      }
      headers.set('Location', value)
    }
  }
  if (!noBody) chunks = await decodeResourceBody(chunks, headers.get('Content-Encoding'), current, signal)
  headers.delete('Content-Encoding')
  headers.set('Content-Length', String(chunks.reduce((sum, chunk) => sum + chunk.length, 0)))
  return noBody ? new Response(null, { status: data.status, headers }) : new NativeResourceResponse(chunks, data.status, headers, current)
}

export class PluginResourcePresentations {
  private records = new Map<string, Presentation>()
  private tail: Promise<void> = Promise.resolve()
  private pending = 0
  private bufferedBytes = 0
  private bufferWaiters = new Set<() => void>()

  private releaseBytes(size: number): void {
    this.bufferedBytes -= size
    for (const wake of this.bufferWaiters) wake()
  }

  private async reserveBytes(size: number): Promise<void> {
    const deadline = this.clock() + 10_000
    while (this.bufferedBytes + size > 192 * 1024 * 1024) {
      const remaining = deadline - this.clock()
      if (remaining <= 0) throw new Error('RESOURCE_BUSY')
      await new Promise<void>(resolve => {
        const wake = () => { clearTimeout(timer); this.bufferWaiters.delete(wake); resolve() }
        const timer = setTimeout(wake, remaining)
        this.bufferWaiters.add(wake)
      })
    }
    this.bufferedBytes += size
  }
  constructor(private clock: () => number = Date.now) {}

  open(raw: unknown, senderId: number, invoke: ResourceInvoker, current: () => boolean): PluginResourceBridgeResult {
    const request = parseResourceBridgeRequest(raw, this.clock())
    if (!current()) throw new Error('RESOURCE_REVOKED')
    for (const [key, record] of this.records) {
      if (!this.live(key, record)) this.remove(key)
    }
    if (this.records.size >= 32) throw new Error('RESOURCE_BUSY')
    const handleId = `p-${randomBytes(24).toString('hex')}`
    const origin = `${PLUGIN_RESOURCE_SCHEME}://${handleId}`
    // Owner wall clocks may be ahead OR behind Desktop. Local lifetime is
    // independent and capped; every invocation still checks owner expiry.
    const deadline = this.clock() + 300_000
    const timer = setTimeout(() => { this.remove(handleId) }, 300_000)
    timer.unref?.()
    this.records.set(handleId, { request, senderId, origin, invoke, current, timer, deadline, active: new Set() })
    return { handleId, url: origin + PLUGIN_RESOURCE_WRAPPER_PATH }
  }

  private remove(key: string): Presentation | undefined {
    const record = this.records.get(key)
    if (record) {
      clearTimeout(record.timer)
      this.records.delete(key)
      for (const response of record.active) response.abort()
    }
    return record
  }

  private live(key: string, record: Presentation): boolean {
    return this.records.get(key) === record && record.deadline > this.clock() && record.current()
  }

  private async dispatch<T>(work: () => Promise<T>): Promise<T> {
    if (this.pending >= 16) throw new Error('RESOURCE_BUSY')
    this.pending += 1
    let expired = false
    let rejectDeadline!: (reason: Error) => void
    const deadline = new Promise<never>((_resolve, reject) => { rejectDeadline = reject })
    const timer = setTimeout(() => { expired = true; rejectDeadline(new Error('RESOURCE_BUSY')) }, 10_000)
    timer.unref?.()
    const call = this.tail.then(() => {
      clearTimeout(timer)
      if (expired) throw new Error('RESOURCE_BUSY')
      return work()
    })
    this.tail = call.then(() => undefined, () => undefined).finally(() => { this.pending -= 1 })
    return Promise.race([call, deadline])
  }

  async close(handleId: string, senderId: number): Promise<void> {
    const record = this.records.get(handleId)
    if (!record) return
    if (record.senderId !== senderId) throw new Error('RESOURCE_REVOKED')
    this.remove(handleId)
    await record.invoke({ document_id: record.request.documentId, grant_id: record.request.grantId }, true)
  }

  retireSender(senderId: number): void {
    for (const [key, record] of this.records) {
      if (record.senderId === senderId) void this.close(key, senderId).catch(() => undefined)
    }
  }

  async handle(request: Request): Promise<Response> {
    try {
      if (!isPluginResourceUrl(request.url)) throw new Error('RESOURCE_GONE')
      const url = new URL(request.url)
      const key = url.hostname
      const record = this.records.get(key)
      if (!record || !this.live(key, record)) throw new Error('RESOURCE_GONE')
      const headers = headersFor(record.origin)
      if (!['GET', 'OPTIONS'].includes(request.method)) return new Response(null, { status: 405, headers })
      if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers })
      if (url.pathname === PLUGIN_RESOURCE_WRAPPER_PATH && !url.search) {
        const source = (record.origin + record.request.entryPath).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;')
        headers.set('Content-Security-Policy', headersFor(record.origin, true).get('Content-Security-Policy')!)
        headers.set('Content-Type', 'text/html;charset=utf-8')
        return new Response(`<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;overflow:hidden}</style></head><body><iframe title="Plugin resource" sandbox="allow-scripts allow-forms" referrerpolicy="no-referrer" src="${source}"></iframe></body></html>`, { headers })
      }
      const path = validateResourcePath(url.pathname)
      const accept = request.headers.get('Accept') ?? ''
      const query = [...url.searchParams.entries()]
      if (!path.startsWith(ROOT.exec(record.request.entryPath)![0]) || url.search.length > 65537 || query.length > 256 || accept.length > 1024) {
        throw new Error('RESOURCE_PATH_DENIED')
      }
      if (this.pending >= 16) return new Response(null, { status: 429, headers })
      return await this.dispatch(async () => {
        // Preserve the 80 MiB wire/input allowance and two 32 MiB layer buffers.
        // Explicit codec windows plus bounded metadata make this 164 MiB, so an
        // unread 24 MiB response leaves room under the unchanged 192 MiB cap.
        const reservation = 80 * 1024 * 1024 + 2 * MAX_PLUGIN_RESOURCE_BODY + MAX_CODEC_WINDOW + DECODE_METADATA_ALLOWANCE
        await this.reserveBytes(reservation)
        let retained = 0
        const decoding = new AbortController()
        try {
          if (!this.live(key, record)) throw new Error('RESOURCE_REVOKED')
          // Exactly one captured POST, including after ambiguous failures.
          const envelope = await record.invoke({ document_id: record.request.documentId,
            grant_id: record.request.grantId, path: decodeURIComponent(path), query, accept }, false)
          if (!this.live(key, record)) throw new Error('RESOURCE_REVOKED')
          record.active.add(decoding)
          const response = await resourceResponse(envelope, record.origin, path, () => this.live(key, record), decoding.signal)
          if (!this.live(key, record)) {
            if (response instanceof NativeResourceResponse) response.abort()
            throw new Error('RESOURCE_REVOKED')
          }
          if (response instanceof NativeResourceResponse) {
            retained = Number(response.headers.get('Content-Length'))
            const size = retained
            record.active.add(response)
            void response.completed.then(() => { record.active.delete(response); this.releaseBytes(size) })
          }
          return response
        } finally {
          record.active.delete(decoding)
          this.releaseBytes(reservation - retained)
        }
      })
    } catch (error) {
      if (error instanceof Error && error.message === 'RESOURCE_BUSY') {
        return new Response(null, { status: 429, headers: { 'Cache-Control': 'no-store' } })
      }
      return new Response('Resource unavailable', { status: 410, headers: { 'Cache-Control': 'no-store' } })
    }
  }
}
