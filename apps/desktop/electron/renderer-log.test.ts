import { describe, expect, it, vi } from 'vitest'

import { attachRendererConsoleCapture, formatRendererBoundaryReport, formatRendererConsoleLine } from './renderer-log'

type ConsoleMessageEvent = {
  level?: unknown
  message?: unknown
  sourceId?: unknown
  lineNumber?: unknown
}

type ConsoleMessageHandler = (event: ConsoleMessageEvent) => void

function createWindowHarness() {
  let handler: ConsoleMessageHandler | undefined

  const win = {
    webContents: {
      on: (_event: 'console-message', listener: ConsoleMessageHandler) => {
        handler = listener
      }
    }
  }

  return { win, getHandler: () => handler }
}

describe('formatRendererConsoleLine', () => {
  it('formats the canonical Electron console-message event at error level', () => {
    const line = formatRendererConsoleLine('hud', {
      level: 'error',
      message: 'Minified React error #310',
      sourceId: 'file:///app/index.js',
      lineNumber: 13
    })

    expect(line).toBe('[renderer console:hud] Minified React error #310 (file:///app/index.js:13)')
  })

  it('drops a canonical non-error string level', () => {
    expect(formatRendererConsoleLine('main', { level: 'info', message: 'x', sourceId: 's', lineNumber: 1 })).toBeNull()
  })

  it('drops malformed event objects', () => {
    expect(formatRendererConsoleLine('main', {})).toBeNull()
  })

  it('never persists opaque CPAMC guest errors, including forged source URLs', () => {
    for (const url of ['blob:file:///guest', 'data:text/html,guest', 'about:srcdoc']) {
      expect(formatRendererConsoleLine('main', { level: 'error', message: 'provider-secret', sourceId: url, lineNumber: 1 })).toBeNull()
      expect(formatRendererConsoleLine('main', { level: 'error', message: 'provider-secret',
        sourceId: 'file:///app/spoofed.js', lineNumber: 1, frame: { url, parent: { url: 'file:///app/index.html' } } })).toBeNull()
    }
  })
})

describe('attachRendererConsoleCapture', () => {
  it('registers a single-argument listener, logs errors, and skips the rest', () => {
    const log = vi.fn()
    const capture = createWindowHarness()

    attachRendererConsoleCapture(capture.win, 'quick-entry', log)

    const handler = capture.getHandler()
    expect(handler).toHaveLength(1)

    handler?.({ level: 'error', message: 'crash', sourceId: 'src', lineNumber: 2 })
    handler?.({ level: 'debug', message: 'debug', sourceId: 'src', lineNumber: 3 })

    expect(log).toHaveBeenCalledTimes(1)
    expect(log).toHaveBeenCalledWith('[renderer console:quick-entry] crash (src:2)')
  })

  it('reports signature drift once across renderer windows', () => {
    const log = vi.fn()
    const firstCapture = createWindowHarness()
    const secondCapture = createWindowHarness()

    attachRendererConsoleCapture(firstCapture.win, 'main', log)
    attachRendererConsoleCapture(secondCapture.win, 'hud', log)

    firstCapture.getHandler()?.({})
    secondCapture.getHandler()?.({})

    expect(log).toHaveBeenCalledTimes(1)
    expect(log).toHaveBeenCalledWith(
      '[renderer console] Electron console-message signature drift detected; renderer errors may not be captured'
    )
  })
})

describe('formatRendererBoundaryReport', () => {
  it('carries window label, boundary label, message, and component stack', () => {
    const report = formatRendererBoundaryReport(
      'main',
      'root',
      'Minified React error #310',
      '\n    at Gde (index.js:13)\n    at C_ (index.js:13)'
    )

    expect(report).toContain('[renderer crash:main] [error-boundary:root] Minified React error #310')
    expect(report).toContain('at Gde (index.js:13)')
  })

  it('survives a malformed payload and clamps oversized fields', () => {
    const report = formatRendererBoundaryReport(undefined, null, 'x'.repeat(10_000), 'y'.repeat(10_000))

    expect(report).toContain('[renderer crash:unknown] [error-boundary:unknown]')
    expect(report.length).toBeLessThan(7_000)
  })
})

it('capture persists only the native main frame when frame authority is available', () => {
  const mainFrame = { url: 'file:///app/index.html' }
  const log = vi.fn()
  const on = vi.fn()
  attachRendererConsoleCapture({ webContents: { mainFrame, on } }, 'main', log)
  const listener = on.mock.calls[0][1]
  const error = { level: 'error', message: 'private-provider-value', sourceId: 'file:///app/forged.js', lineNumber: 1 }
  listener({ ...error, frame: { url: 'blob:file:///guest', parent: mainFrame } })
  listener(error)
  expect(log).not.toHaveBeenCalled()
  listener({ ...error, message: 'trusted host failure', frame: mainFrame })
  expect(log).toHaveBeenCalledExactlyOnceWith('[renderer console:main] trusted host failure (file:///app/forged.js:1)')
})
