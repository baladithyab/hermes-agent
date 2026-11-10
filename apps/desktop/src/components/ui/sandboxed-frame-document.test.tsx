// @vitest-environment jsdom
import { Blob as NodeBlob } from 'node:buffer'
import { createHash, webcrypto } from 'node:crypto'
import { TextEncoder as NodeTextEncoder } from 'node:util'

import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MAX_VERIFIED_DOCUMENT_BYTES, SandboxedFrame } from './sandboxed-frame'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })

  return { promise, resolve }
}

const created = vi.fn<(blob: Blob) => string>()
const revoked = vi.fn<(url: string) => void>()

beforeEach(() => {
  created.mockReset().mockImplementation(() => `blob:https://host.test/${created.mock.calls.length}`)
  revoked.mockReset()
  const NativeURL = URL
  vi.stubGlobal('URL', class extends NativeURL {
    static createObjectURL = created
    static revokeObjectURL = revoked
  })
  vi.stubGlobal('Blob', NodeBlob)
  vi.stubGlobal('TextEncoder', NodeTextEncoder)
  vi.stubGlobal('crypto', webcrypto)
})

afterEach(() => {
  cleanup()
  window.document.documentElement.removeAttribute('data-hermes-desktop-host')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function documentFor(html: string) {
  return { html, sha256: createHash('sha256').update(html, 'utf8').digest('hex') }
}

describe('verified document sandbox', () => {
  it('uses the existing browser guest shell after verification without relaxing its sandbox', async () => {
    window.document.documentElement.dataset.hermesDesktopHost = 'browser'
    vi.stubGlobal('__HERMES_BASE_PATH__', '/hermes')
    vi.stubGlobal('__HERMES_UI_SURFACE__', 'webapp')
    const document = documentFor('<p>Verified browser guest</p>')

    const { container } = render(<SandboxedFrame sandbox="allow-scripts allow-same-origin allow-popups" title="Verified browser"
      verifiedDocument={document} />)

    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('src')).toContain('/hermes/assets/webapp-guest.html')
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    const send = vi.spyOn(frame.contentWindow!, 'postMessage')
    fireEvent.load(frame)
    expect(send).toHaveBeenCalledWith({ type: 'hermes-preview-document', html: document.html, sandbox: 'allow-scripts' }, '*')
  })

  it('verifies exact UTF-8 bytes before minting a host URL and keeps an opaque sandbox', async () => {
    const document = documentFor('<!doctype html><p>日本語</p>')
    const digest = deferred<ArrayBuffer>()
    vi.stubGlobal('crypto', { subtle: { digest: () => digest.promise } })

    const { container, unmount } = render(<SandboxedFrame
      sandbox="ALLOW-SAME-ORIGIN allow-scripts allow-popups allow-top-navigation" title="Verified console"
      verifiedDocument={document}
    />)

    expect(SandboxedFrame.supportsVerifiedDocument).toBe(true)
    expect(container.querySelector('iframe')).toBeNull()
    expect(created).not.toHaveBeenCalled()
    await act(async () => {
      digest.resolve(await webcrypto.subtle.digest('SHA-256', new NodeTextEncoder().encode(document.html)))
      await digest.promise
    })
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('src')).toBe('blob:https://host.test/1')
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(frame.hasAttribute('srcdoc')).toBe(false)
    expect(frame.hasAttribute('allow')).toBe(false)
    expect(created.mock.calls[0][0].size).toBe(Buffer.byteLength(document.html))
    expect(await created.mock.calls[0][0].text()).toBe(document.html)
    unmount()
    expect(revoked).toHaveBeenCalledExactlyOnceWith('blob:https://host.test/1')
  })

  it.each([
    null, {}, { html: 1, sha256: '0'.repeat(64) }, { html: '<p>x</p>', sha256: 'invalid' },
    { html: '<p>x</p>', sha256: '0'.repeat(64) },
    { html: 'x'.repeat(MAX_VERIFIED_DOCUMENT_BYTES + 1), sha256: '0'.repeat(64) },
    // Below the UTF-16 character limit but above the UTF-8 byte limit.
    { html: 'é'.repeat(MAX_VERIFIED_DOCUMENT_BYTES / 2 + 1), sha256: '0'.repeat(64) }
  ])('rejects malformed, mismatched and oversized documents without allocating a URL (%#)', async document => {
    const failed = vi.fn()

    const { container } = render(<SandboxedFrame
      onVerificationError={failed} title="Rejected" verifiedDocument={document as never}
    />)

    await waitFor(() => expect(failed).toHaveBeenCalledTimes(1))
    expect(container.querySelector('iframe')).toBeNull()
    expect(created).not.toHaveBeenCalled()
  })

  it('accepts the 8 MiB boundary without relaxing the ordinary data URL cap', async () => {
    const document = documentFor('x'.repeat(MAX_VERIFIED_DOCUMENT_BYTES))
    const { container, rerender } = render(<SandboxedFrame title="Large" verifiedDocument={document} />)
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    expect(created.mock.calls[0][0].size).toBe(MAX_VERIFIED_DOCUMENT_BYTES)
    rerender(<SandboxedFrame src={`data:text/html,${'x'.repeat(2 * 1024 * 1024)}`} title="Ordinary" />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(revoked).toHaveBeenCalledExactlyOnceWith('blob:https://host.test/1')
    rerender(<SandboxedFrame src="data:text/html,<p>small</p>" title="Ordinary" />)
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe('data:text/html,<p>small</p>')
  })

  it('ignores out-of-order verification and verification completed after unmount', async () => {
    const first = deferred<ArrayBuffer>()
    const second = deferred<ArrayBuffer>()
    const third = deferred<ArrayBuffer>()
    const digest = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise).mockReturnValueOnce(third.promise)
    vi.stubGlobal('crypto', { subtle: { digest } })
    const a = documentFor('first')
    const b = documentFor('second')
    const c = documentFor('third')
    const { container, rerender, unmount } = render(<SandboxedFrame title="A" verifiedDocument={a} />)
    rerender(<SandboxedFrame title="B" verifiedDocument={b} />)
    await act(async () => {
      second.resolve(await webcrypto.subtle.digest('SHA-256', new NodeTextEncoder().encode(b.html)))
      await second.promise
    })
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe('blob:https://host.test/1')
    await act(async () => {
      first.resolve(await webcrypto.subtle.digest('SHA-256', new NodeTextEncoder().encode(a.html)))
      await first.promise
    })
    expect(created).toHaveBeenCalledTimes(1)
    expect(container.querySelector('iframe')?.getAttribute('title')).toBe('B')
    rerender(<SandboxedFrame title="C" verifiedDocument={c} />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(revoked).toHaveBeenCalledExactlyOnceWith('blob:https://host.test/1')
    unmount()
    await act(async () => {
      third.resolve(await webcrypto.subtle.digest('SHA-256', new NodeTextEncoder().encode(c.html)))
      await third.promise
    })
    expect(created).toHaveBeenCalledTimes(1)
    expect(revoked).toHaveBeenCalledTimes(1)
  })

  it('does not reuse a revoked URL when returning to the same document', async () => {
    const a = documentFor('original')
    const b = documentFor('replacement')
    const { container, rerender } = render(<SandboxedFrame title="A" verifiedDocument={a} />)
    await waitFor(() => expect(created).toHaveBeenCalledTimes(1))
    const pending = deferred<ArrayBuffer>()
    vi.stubGlobal('crypto', { subtle: { digest: () => pending.promise } })
    rerender(<SandboxedFrame title="B" verifiedDocument={b} />)
    rerender(<SandboxedFrame title="A again" verifiedDocument={a} />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(revoked).toHaveBeenCalledExactlyOnceWith('blob:https://host.test/1')
  })
})

describe('host-owned resource presentation frames', () => {
  it('accepts only capability-shaped custom origins and retains the opaque sandbox', () => {
    const src = 'hermes-plugin-resource://p-' + 'a'.repeat(48) + '/__hermes_plugin_resource__'

    const { container, rerender } = render(<SandboxedFrame sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-top-navigation" src={src}
      title="Resource" />)

    expect(SandboxedFrame.supportsPluginResources).toBe(true)
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('src')).toBe(src)
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-forms')
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')

    for (const invalid of ['hermes-plugin-resource://arbitrary/private', 'hermes-plugin-resource://user@p-' + 'a'.repeat(48) + '/x',
      'blob:https://host.test/caller-owned']) {
      rerender(<SandboxedFrame src={invalid} title="Rejected" />)
      expect(container.querySelector('iframe')).toBeNull()
    }

    expect(created).not.toHaveBeenCalled()
  })
})
