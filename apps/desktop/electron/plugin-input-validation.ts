/** C0 controls are forbidden in plugin routing input; wire/header names also
 * reject DEL. Keep the ranges explicit without embedding controls in regexes. */
export function hasPluginControlCharacters(value: string, includeDelete = false): boolean {
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index)

    if (code < 32 || (includeDelete && code === 127)) {return true}
  }

  return false
}

export function isPluginOwnerComponent(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0 && value.length <= 128 &&
    !value.includes('/') && !value.includes('\\') && !hasPluginControlCharacters(value))
}
