export function deviceWindowUrl(serial: string): string {
  const url = new URL('/', location.origin)
  url.searchParams.set('device-window', serial)
  return url.toString()
}
export function openDeviceWindow(serial: string, event?: MouseEvent): void {
  const url = deviceWindowUrl(serial)
  const name = 'd3m0n-device-' + serial.replace(/[^A-Za-z0-9_-]/g, '_')
  const index = Math.abs(Array.from(serial).reduce((value, ch) => value + ch.charCodeAt(0), 0)) % 8
  const left = 30 + index * 36, top = 35 + index * 28
  const popup = window.open(url, name, `popup=yes,width=520,height=940,resizable=yes,scrollbars=yes,left=${left},top=${top}`)
  if (popup) {
    event?.preventDefault()
    try { popup.opener = null; popup.focus() } catch {}
  }
  // If a popup is blocked, do not prevent the anchor's default target=_blank
  // navigation. This still gives each device an independent browser tab.
}
