import crypto from 'node:crypto'
import http from 'node:http'
import https from 'node:https'

import { htmlResponseError, httpStatusError, jsonAgentFor, withRetry } from './api-transport'
import { DEFAULT_FETCH_TIMEOUT_MS, resolveTimeoutMs } from './hardening'
import { nativeGatewayHttpHeaders } from './local-gateway'

// Assemble a single-file multipart/form-data body (FastAPI `UploadFile`
// endpoints, e.g. kanban attachments). Hand-rolled because node's http has no
// FormData and the payload is one file — a dependency would be overkill.
function multipartBody(upload) {
  const boundary = `----hermes-${crypto.randomBytes(12).toString('hex')}`
  const filename = String(upload.filename || 'file').replace(/["\r\n]/g, '_')

  const body = Buffer.concat([
    Buffer.from(
      `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="file"; filename="${filename}"\r\n` +
        `Content-Type: ${upload.contentType || 'application/octet-stream'}\r\n\r\n`
    ),
    Buffer.from(upload.bytes),
    Buffer.from(`\r\n--${boundary}--\r\n`)
  ])

  return { body, contentType: `multipart/form-data; boundary=${boundary}` }
}

/** The authenticated JSON transport retains per-attempt native grants and the
 * existing never-replay-after-send retry policy. Main supplies its header store. */
export function fetchAuthenticatedJson(url, token, options: any, optionsForFetcher: {
  hermesHome: string
  headersForRemoteRequest: (url: string) => Record<string, string>
}) {
  // Retry policy lives in api-transport.ts: idempotent verbs retry on any
  // transient transport error; POST/PUT/DELETE only when the request provably
  // never reached the server (see shouldRetryRequest) — never double-submit.
  return withRetry(
    async (requestState: any) => {
      // Mint inside each retry: a grant authorizes exactly one HTTP request.
      const nativeHeaders = options.gatewayDescriptor
        ? await nativeGatewayHttpHeaders(options.gatewayDescriptor, url, optionsForFetcher.hermesHome, options.method || 'GET', options.upload ? undefined : options.body)
        : null

      return new Promise((resolve, reject) => {
        const { body, contentType } = options.upload
          ? multipartBody(options.upload)
          : {
              body: options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body)),
              contentType: 'application/json'
            }

        const parsed = new URL(url)
        const client = parsed.protocol === 'https:' ? https : http
        const agent = jsonAgentFor(parsed.protocol)
        const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

          return
        }

        const req = client.request(
          parsed,
          {
            agent,
            method: options.method || 'GET',
            headers: {
              ...optionsForFetcher.headersForRemoteRequest(url),
              ...(options.headers || {}),
              'Content-Type': contentType,
              ...(nativeHeaders || { 'X-Hermes-Session-Token': token }),
              // RFC 8252 native flow authenticates the gated gateway with a bearer
              // token instead of the loopback session-token header. When
              // ``options.bearer`` is set we send Authorization: Bearer <token>;
              // the gateway's OAuth gate verifies it via the provider stack with
              // no cookie involved.
              ...(options.bearer ? { Authorization: `Bearer ${options.bearer}` } : {}),
              ...(body ? { 'Content-Length': String(body.length) } : {})
            }
          },
          res => {
            const chunks: Buffer[] = []
            let received = 0
            let oversized = false
            res.on('error', reject)
            res.on('data', chunk => {
              if (oversized) {return}
              received += chunk.length

              if (options.maxResponseBytes !== undefined && received > options.maxResponseBytes) {
                oversized = true
                chunks.length = 0
                const error = new Error('Bounded plugin response is too large')
                reject(error)
                req.destroy(error)

                return
              }

              chunks.push(chunk)
            })
            res.on('end', () => {
              if (oversized) {return}
              const text = Buffer.concat(chunks).toString('utf8')

              if ((res.statusCode || 500) >= 400) {
                reject(httpStatusError(res.statusCode, text, res.statusMessage))

                return
              }

              // http.request never follows redirects, so any 3xx -- with an HTML
              // login page, an empty body, whatever -- is the request bouncing
              // off a proxy or a scheme/slash mismatch, never JSON.
              if (res.statusCode >= 300) {
                reject(htmlResponseError(url, res.statusCode, res.headers.location))

                return
              }

              if (!text) {
                resolve(null)

                return
              }

              // A 2xx response whose body is HTML means the request fell through
              // to the SPA index.html (e.g. an unregistered /api path). JSON.parse
              // would throw an opaque `Unexpected token '<'` here, so surface a
              // clear diagnostic with the offending URL instead.
              const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
              const contentType = String(res.headers['content-type'] || '')

              if (looksHtml || contentType.includes('text/html')) {
                reject(htmlResponseError(url, res.statusCode))

                return
              }

              try {
                resolve(JSON.parse(text))
              } catch {
                reject(new Error(`Invalid JSON from ${url} (status ${res.statusCode}): ${text.slice(0, 200)}`))
              }
            })
          }
        )

        req.on('error', reject)
        req.setTimeout(timeoutMs, () => {
          req.destroy(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
        })

        // From here the request goes on the wire: a later transport error can no
        // longer prove the server didn't process it, so non-idempotent verbs must
        // not be retried past this point.
        requestState.bodySent = true

        if (body) {
          req.write(body)
        }

        req.end()
      })
    },
    { method: options.method || 'GET', maxRetries: options.singleAttempt ? 0 : undefined }
  )
}
