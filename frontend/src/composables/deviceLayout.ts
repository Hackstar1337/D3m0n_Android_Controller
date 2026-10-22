export interface DevicePanel { device: string; left: number; top: number; width: number; height: number; z: number }
function number(value: unknown, fallback: number, min: number, max: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(min, Math.min(max, Math.round(value))) : fallback
}
export function cleanLayout(value: unknown, allowed: Set<string>, viewportWidth: number): DevicePanel[] {
  if (!Array.isArray(value)) return []
  const result: DevicePanel[] = [], seen = new Set<string>()
  const maxWidth = Math.max(260, viewportWidth - 20)
  for (const item of value.slice(0, 50)) {
    if (!item || typeof item !== 'object') continue
    const p = item as Record<string, unknown>
    if (typeof p.device !== 'string' || !allowed.has(p.device) || seen.has(p.device)) continue
    seen.add(p.device)
    const width = number(p.width, Math.min(340, maxWidth), 260, Math.min(1000, maxWidth))
    result.push({ device: p.device, width, height: number(p.height, 690, 480, 1400),
      left: number(p.left, 10, 0, Math.max(0, viewportWidth - width - 10)), top: number(p.top, 10, 0, 15000), z: result.length + 1 })
  }
  return result
}
export function tileLayout(panels: DevicePanel[], viewportWidth: number): DevicePanel[] {
  const columns = Math.max(1, Math.floor(viewportWidth / 350))
  const width = Math.max(260, Math.min(420, Math.floor((viewportWidth - 20) / columns) - 12))
  return panels.map((p, i) => ({ ...p, left: 10 + (i % columns) * (width + 12), top: 10 + Math.floor(i / columns) * 710, width, height: 690, z: i + 1 }))
}
