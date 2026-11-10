import { mkdtemp, readdir, readFile, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import type { BrowserWindow, IpcMainInvokeEvent } from 'electron'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PluginFileSaveRequest, PluginFileSaveResult } from './plugin-file-save-contract'

const host = vi.hoisted(() => ({
  exposeInMainWorld: vi.fn(),
  handlers: new Map<string, (event: IpcMainInvokeEvent, request: unknown) => Promise<PluginFileSaveResult>>(),
  event: null as unknown as IpcMainInvokeEvent,
  fromWebContents: vi.fn(),
  showSaveDialog: vi.fn(),
  invoke: vi.fn()
}))

vi.mock('electron', () => ({
  BrowserWindow: { fromWebContents: host.fromWebContents },
  dialog: { showSaveDialog: host.showSaveDialog },
  ipcMain: { handle: (name: string, handler: (event: IpcMainInvokeEvent, request: unknown) => Promise<PluginFileSaveResult>) => {
    host.handlers.set(name, handler)
  } },
  contextBridge: { exposeInMainWorld: host.exposeInMainWorld },
  ipcRenderer: { invoke: host.invoke, sendSync: () => ({}), send: vi.fn(), on: vi.fn() },
  webFrame: {}, webUtils: {}
}))

import './preload'

import { registerPluginFileSaveIpc } from './plugin-file-save-ipc'

let directory: string
let trusted = true
let destroyed = false
let bridge: { savePluginFile: (request: PluginFileSaveRequest) => Promise<PluginFileSaveResult> }
let mainFrame: { url: string }
let owner: BrowserWindow

beforeAll(() => {
  const registration = host.exposeInMainWorld.mock.calls.find(([name]) => name === 'hermesDesktop')
  expect(registration).toBeDefined()
  bridge = registration![1]
})
beforeEach(async () => {
  directory = await mkdtemp(path.join(os.tmpdir(), 'hermes-plugin-save-ipc-'))
  trusted = true
  destroyed = false
  mainFrame = { url: 'file:///app/dist/index.html?profile=test#/plugin' }
  const sender = { mainFrame, isDestroyed: () => destroyed }
  owner = { webContents: sender, isDestroyed: () => destroyed } as unknown as BrowserWindow
  host.event = { sender, senderFrame: mainFrame } as unknown as IpcMainInvokeEvent
  host.fromWebContents.mockReset().mockReturnValue(owner)
  host.showSaveDialog.mockReset().mockResolvedValue({ canceled: true })
  host.invoke.mockReset().mockImplementation(async (channel: string, request: unknown) => {
    const handler = host.handlers.get(channel)

    if (!handler) {throw new Error('Unknown IPC capability')}

    return handler(host.event, request)
  })
  registerPluginFileSaveIpc({ isTrustedWindow: () => trusted, rendererUrl: () => 'file:///app/dist/index.html' })
})
afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

describe('plugin Save capability across preload and main', () => {
  it('reports real native cancellation and then saves actual bytes through the same capability', async () => {
    const request = { suggestedFilename: 'export.json', bytes: new Uint8Array([0, 255, 13, 10]).buffer }
    await expect(bridge.savePluginFile(request)).resolves.toEqual({ status: 'cancelled' })
    expect(await readdir(directory)).toEqual([])
    const destination = path.join(directory, 'user-chosen.json')
    host.showSaveDialog.mockResolvedValue({ canceled: false, filePath: destination })
    await expect(bridge.savePluginFile(request)).resolves.toEqual({ status: 'saved' })
    expect(await readFile(destination)).toEqual(Buffer.from([0, 255, 13, 10]))
    expect(host.showSaveDialog.mock.calls[1][0]).toBe(owner)
  })

  it.each(['subframe', 'guest-window', 'wrong-document', 'destroyed', 'no-window'])('refuses %s before native UI', async kind => {
    if (kind === 'subframe') {host.event = { ...host.event, senderFrame: { url: mainFrame.url } } as IpcMainInvokeEvent}

    if (kind === 'guest-window') {trusted = false}

    if (kind === 'wrong-document') {mainFrame.url = 'file:///tmp/guest.html'}

    if (kind === 'destroyed') {destroyed = true}

    if (kind === 'no-window') {host.fromWebContents.mockReturnValue(null)}
    await expect(bridge.savePluginFile({ suggestedFilename: 'export.json', bytes: new ArrayBuffer(0) })).rejects.toThrow('not authorized')
    expect(host.showSaveDialog).not.toHaveBeenCalled()
    expect(await readdir(directory)).toEqual([])
  })

  it('checks the sender again after native consent', async () => {
    host.showSaveDialog.mockImplementation(async () => {
      mainFrame.url = 'https://guest.example.test/'

      return { canceled: false, filePath: path.join(directory, 'not-written.json') }
    })
    await expect(bridge.savePluginFile({ suggestedFilename: 'export.json', bytes: new ArrayBuffer(1) })).rejects.toThrow('no longer authorized')
    expect(await readdir(directory)).toEqual([])
  })

  it('rejects oversize bytes at preload before invoking main', async () => {
    await expect(async () => bridge.savePluginFile({
      suggestedFilename: 'export.json', bytes: new ArrayBuffer(32 * 1024 * 1024 + 1)
    })).rejects.toThrow('32 MiB')
    expect(host.invoke).not.toHaveBeenCalled()
    expect(host.showSaveDialog).not.toHaveBeenCalled()
  })
})
