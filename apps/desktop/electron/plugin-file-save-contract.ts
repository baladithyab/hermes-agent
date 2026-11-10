export const PLUGIN_FILE_SAVE_MAX_BYTES = 32 * 1024 * 1024

export interface PluginFileSaveRequest {
  suggestedFilename: string
  bytes: ArrayBuffer
}

export type PluginFileSaveResult = { status: 'saved' } | { status: 'cancelled' }

/** Validate on both sides of IPC, before allocating a snapshot or opening a
 *  dialog. A suggestion is a basename, never a path or Windows device name. */
export function parsePluginFileSaveRequest(value: unknown): PluginFileSaveRequest {
  if (!value || typeof value !== 'object' || !('suggestedFilename' in value) || !('bytes' in value)) {
    throw new Error('Invalid file save request')
  }
  const { suggestedFilename, bytes } = value
  if (
    typeof suggestedFilename !== 'string' ||
    !suggestedFilename.trim() ||
    suggestedFilename === '.' || suggestedFilename === '..' ||
    suggestedFilename.length > 255 ||
    new TextEncoder().encode(suggestedFilename).byteLength > 255 ||
    /[<>:"/\\|?*\u0000-\u001f\u007f]/.test(suggestedFilename) ||
    /[. ]$/.test(suggestedFilename) ||
    /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(suggestedFilename)
  ) {
    throw new Error('File save requires a suggested filename, not a path')
  }
  if (!(bytes instanceof ArrayBuffer) || bytes.byteLength > PLUGIN_FILE_SAVE_MAX_BYTES) {
    throw new Error('File save requires an ArrayBuffer of at most 32 MiB')
  }
  return { suggestedFilename, bytes }
}
