import fs from 'node:fs'
import type { SaveDialogOptions, SaveDialogReturnValue } from 'electron'

import { fsPumpDeps, type PumpDeps, saveDialogFilters, writeBufferToFile } from './gateway-file-download'

import { parsePluginFileSaveRequest, type PluginFileSaveResult } from './plugin-file-save-contract'

export interface PluginFileSaveDeps {
  isAuthorized: () => boolean
  showSaveDialog: (options: SaveDialogOptions) => Promise<SaveDialogReturnValue>
  pump?: PumpDeps
}

/** Trusted renderer capability. Native UI alone chooses the destination and
 *  confirms replacement. No chosen path is returned to plugin/guest code. */
export async function savePluginFile(value: unknown, deps: PluginFileSaveDeps): Promise<PluginFileSaveResult> {
  if (!deps.isAuthorized()) {
    throw new Error('File save sender is not authorized')
  }
  const request = parsePluginFileSaveRequest(value)
  // Capture the bounded bytes before awaiting user input. Even an in-process
  // caller cannot change what will be saved while the dialog is open.
  const bytes = Buffer.from(new Uint8Array(request.bytes))
  const selection = await deps.showSaveDialog({
    title: 'Save File',
    defaultPath: request.suggestedFilename,
    filters: saveDialogFilters(request.suggestedFilename),
    properties: ['showOverwriteConfirmation']
  })
  if (selection.canceled) {
    return { status: 'cancelled' }
  }
  if (!selection.filePath) {
    throw new Error('Save dialog returned no destination')
  }
  if (!deps.isAuthorized()) {
    throw new Error('File save sender is no longer authorized')
  }
  // Reuse the download path's exclusive temporary file and atomic rename.
  // Provider exports must not inherit world-readable default file modes.
  await writeBufferToFile(bytes, selection.filePath, deps.pump ?? {
    ...fsPumpDeps(),
    createWriteStream: tempPath => fs.createWriteStream(tempPath, { flags: 'wx', mode: 0o600 })
  })
  return { status: 'saved' }
}
