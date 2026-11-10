import { mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { SaveDialogOptions, SaveDialogReturnValue } from 'electron'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fsPumpDeps } from './gateway-file-download'
import { savePluginFile } from './plugin-file-save'
import { PLUGIN_FILE_SAVE_MAX_BYTES } from './plugin-file-save-contract'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => { resolve = res })
  return { promise, resolve }
}

let directory: string
beforeEach(async () => { directory = await mkdtemp(path.join(os.tmpdir(), 'hermes-plugin-save-')) })
afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

describe('trusted plugin file save', () => {
  it('preserves cancellation even if the dialog returns a path', async () => {
    const destination = path.join(directory, 'untouched.json')
    await writeFile(destination, 'original')
    const showSaveDialog = vi.fn(async () => ({ canceled: true, filePath: destination }))
    await expect(savePluginFile({ suggestedFilename: 'export.json', bytes: new ArrayBuffer(8) }, {
      isAuthorized: () => true, showSaveDialog
    })).resolves.toEqual({ status: 'cancelled' })
    expect(await readFile(destination, 'utf8')).toBe('original')
    expect(await readdir(directory)).toEqual(['untouched.json'])
  })

  it('saves the complete 32 MiB export boundary only to the native-selected path and returns no path', async () => {
    const destination = path.join(directory, 'chosen.json')
    await writeFile(destination, 'previous contents')
    const data = new Uint8Array(PLUGIN_FILE_SAVE_MAX_BYTES).fill(117)
    let shown: SaveDialogOptions | undefined
    const result = await savePluginFile({
      suggestedFilename: 'export.json', bytes: data.buffer,
      path: path.join(directory, 'guest-selected.json')
    }, {
      isAuthorized: () => true,
      showSaveDialog: async options => {
        shown = options
        return { canceled: false, filePath: destination }
      }
    })
    expect(result).toEqual({ status: 'saved' })
    expect((await readFile(destination)).equals(Buffer.from(data.buffer))).toBe(true)
    expect(await readdir(directory)).toEqual(['chosen.json'])
    expect(shown?.defaultPath).toBe('export.json')
    expect(shown?.properties).toContain('showOverwriteConfirmation')
    if (process.platform !== 'win32') {
      expect((await stat(destination)).mode & 0o777).toBe(0o600)
    }
  })

  it.each([
    { suggestedFilename: '../escape', bytes: new ArrayBuffer(0) },
    { suggestedFilename: '/tmp/escape', bytes: new ArrayBuffer(0) },
    { suggestedFilename: 'C:\\escape', bytes: new ArrayBuffer(0) },
    { suggestedFilename: '\\\\server\\escape', bytes: new ArrayBuffer(0) },
    { suggestedFilename: 'CON.json', bytes: new ArrayBuffer(0) },
    { suggestedFilename: 'name\n.json', bytes: new ArrayBuffer(0) },
    { suggestedFilename: 'é'.repeat(128), bytes: new ArrayBuffer(0) },
    { suggestedFilename: 'export.json', bytes: new Uint8Array(8) },
    { suggestedFilename: 'export.json', bytes: { byteLength: 1 } },
    { suggestedFilename: 'export.json', bytes: new ArrayBuffer(PLUGIN_FILE_SAVE_MAX_BYTES + 1) }
  ])('rejects invalid names/types/size before a dialog or disk write (%#)', async request => {
    const showSaveDialog = vi.fn(async () => ({ canceled: false, filePath: path.join(directory, 'file') }))
    await expect(savePluginFile(request, { isAuthorized: () => true, showSaveDialog })).rejects.toThrow()
    expect(showSaveDialog).not.toHaveBeenCalled()
    expect(await readdir(directory)).toEqual([])
  })

  it('captures bytes before the dialog and refuses authority revoked while it is open', async () => {
    const decision = deferred<SaveDialogReturnValue>()
    let authorized = true
    const destination = path.join(directory, 'export.json')
    const pending = savePluginFile({ suggestedFilename: 'export.json', bytes: new ArrayBuffer(1) }, {
      isAuthorized: () => authorized, showSaveDialog: () => decision.promise
    })
    authorized = false
    decision.resolve({ canceled: false, filePath: destination })
    await expect(pending).rejects.toThrow('no longer authorized')
    expect(await readdir(directory)).toEqual([])

    const bytes = new Uint8Array([1, 2, 3])
    const selected = deferred<SaveDialogReturnValue>()
    const captured = savePluginFile({ suggestedFilename: 'export.json', bytes: bytes.buffer }, {
      isAuthorized: () => true, showSaveDialog: () => selected.promise
    })
    bytes.fill(9)
    selected.resolve({ canceled: false, filePath: destination })
    await expect(captured).resolves.toEqual({ status: 'saved' })
    expect(await readFile(destination)).toEqual(Buffer.from([1, 2, 3]))
  })

  it('does not open a dialog for an unauthorized sender', async () => {
    const showSaveDialog = vi.fn()
    await expect(savePluginFile({ suggestedFilename: 'export.json', bytes: new ArrayBuffer(0) }, {
      isAuthorized: () => false, showSaveDialog
    })).rejects.toThrow('not authorized')
    expect(showSaveDialog).not.toHaveBeenCalled()
  })

  it('reports write failure without deleting the old destination or leaving a partial export', async () => {
    const destination = path.join(directory, 'previous.json')
    await writeFile(destination, 'keep me')
    await expect(savePluginFile({ suggestedFilename: 'export.json', bytes: new ArrayBuffer(1) }, {
      isAuthorized: () => true,
      showSaveDialog: async () => ({ canceled: false, filePath: destination }),
      pump: { ...fsPumpDeps(), rename: async () => { throw new Error('fixture rename failure') } }
    })).rejects.toThrow('fixture rename failure')
    expect(await readFile(destination, 'utf8')).toBe('keep me')
    expect(await readdir(directory)).toEqual(['previous.json'])
  })
})
