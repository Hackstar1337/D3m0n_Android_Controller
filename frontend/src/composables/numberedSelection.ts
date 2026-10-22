/** Select only explicitly requested phone numbers; never fall back to all devices. */
export function selectNumberedPhones(input: string, numbers: Record<string, number>, online: string[]): string[] {
  const parts = input.trim().split(/[\s,;]+/).filter(Boolean)
  if (!parts.length) throw new Error('Enter phone numbers, for example 1-5 or 1, 3, 5.')
  const wanted = new Set<number>()
  for (const part of parts) {
    const match = /^#?(\d{1,5})(?:-#?(\d{1,5}))?$/.exec(part)
    if (!match) throw new Error(`Invalid selection: ${part}. Use 1-5 or 1, 3, 5.`)
    const a = Number(match[1]), b = Number(match[2] || match[1])
    if (a < 1 || b < 1 || Math.abs(a - b) > 999) throw new Error('Phone numbers must be positive; choose at most 1,000 numbers at a time.')
    for (let n = Math.min(a, b); n <= Math.max(a, b); n++) wanted.add(n)
    if (wanted.size > 1000) throw new Error('Choose at most 1,000 numbers at a time.')
  }
  const byNumber = new Map<number, string>()
  for (const serial of online) if (numbers[serial]) byNumber.set(numbers[serial], serial)
  const requested = [...wanted].sort((a, b) => a - b)
  const missing = requested.filter(n => !byNumber.has(n))
  if (missing.length) throw new Error(`Phone ${missing.slice(0, 10).join(', ')}${missing.length > 10 ? '…' : ''} is not connected or has no number. Selection was not changed.`)
  return requested.map(n => byNumber.get(n)!)
}
