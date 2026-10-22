// Parses common proxy endpoint notations locally (nothing is sent anywhere).
//   http://user:pass@host:port   socks5://host:port   https://host:port
//   user:pass@host:port          host:port            host:port:user:pass
//   user:pass:host:port          [2001:db8::1]:8080
export type ProxyKind = 'http' | 'https' | 'socks5'
export type ParsedEndpoint = {
  proxy_type: ProxyKind
  host: string
  port: number
  username: string | null
  password: string | null
}
export type ParseResult = { ok: true; value: ParsedEndpoint } | { ok: false; error: string }

const SCHEMES = new Map<string, ProxyKind>([
  ['http', 'http'],
  ['https', 'https'],
  ['socks5', 'socks5'],
  ['socks5h', 'socks5'],
  ['socks', 'socks5'],
])

function decode(value: string): string {
  try { return decodeURIComponent(value) } catch { return value }
}

function asPort(text: string | undefined): number | null {
  if (!text || !/^\d{1,5}$/.test(text)) return null
  const port = Number(text)
  return port >= 1 && port <= 65535 ? port : null
}

function hostOk(host: string): boolean {
  if (!host || host.length > 253) return false
  if (/^\[[0-9A-Fa-f:.]+\]$/.test(host)) return true
  return /^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$/.test(host)
}

function splitHostPort(text: string): { host: string; port: number | null; rest: string[] } | null {
  const bracket = /^(\[[^\]]+\]):(\d{1,5})(?::(.*))?$/.exec(text)
  if (bracket) return { host: bracket[1]!, port: asPort(bracket[2]), rest: bracket[3] !== undefined ? bracket[3].split(':') : [] }
  const parts = text.split(':')
  if (parts.length < 2) return null
  return { host: parts[0]!, port: asPort(parts[1]), rest: parts.slice(2) }
}

/** True when a value typed into the Host field looks like a full endpoint rather than a bare host. */
export function looksLikeEndpoint(value: string): boolean {
  const text = value.trim()
  return text.includes('://') || text.includes('@') || /^[^:\s]+:\d{1,5}(?::|$)/.test(text) || /^\[[^\]]+\]:\d/.test(text)
}

export function parseProxyEndpoint(input: string, fallbackType: ProxyKind = 'http'): ParseResult {
  let text = input.trim()
  if (!text) return { ok: false, error: 'Paste a proxy endpoint first.' }
  if (/\s/.test(text)) return { ok: false, error: 'An endpoint must not contain spaces.' }
  let proxyType: ProxyKind = fallbackType
  let username: string | null = null
  let password: string | null = null

  const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):\/\//.exec(text)
  if (scheme) {
    const kind = SCHEMES.get(scheme[1]!.toLowerCase())
    if (!kind) return { ok: false, error: 'Unsupported scheme. Use http, https or socks5.' }
    proxyType = kind
    text = text.slice(scheme[0].length)
  }

  let host = ''
  let port: number | null = null
  const at = text.lastIndexOf('@')
  if (at >= 0) {
    const userinfo = text.slice(0, at)
    const hostPart = text.slice(at + 1).replace(/[/?#].*$/, '')
    const colon = userinfo.indexOf(':')
    username = decode(colon >= 0 ? userinfo.slice(0, colon) : userinfo)
    password = colon >= 0 ? decode(userinfo.slice(colon + 1)) : null
    const split = splitHostPort(hostPart)
    if (!split || split.rest.length) return { ok: false, error: 'Expected user:pass@host:port.' }
    host = split.host
    port = split.port
  } else {
    const cleaned = text.replace(/\/+$/, '')
    const split = splitHostPort(cleaned)
    if (!split) return { ok: false, error: 'Include a port, for example host:8080.' }
    const swapped = split.port !== null && split.rest.length === 2 && asPort(split.rest[1]) !== null
      && !split.host.includes('.') && !split.host.startsWith('[') && split.rest[0]!.includes('.')
    if (swapped) {
      // user:numericpass:host.example:port
      username = split.host
      password = cleaned.split(':')[1]!
      host = split.rest[0]!
      port = asPort(split.rest[1])
    } else if (split.port !== null) {
      host = split.host
      port = split.port
      if (split.rest.length) {
        username = split.rest[0] ?? ''
        password = split.rest.length > 1 ? split.rest.slice(1).join(':') : null
      }
    } else {
      // user:pass:host:port
      const parts = cleaned.split(':')
      if (parts.length === 4 && asPort(parts[3]) !== null) {
        username = parts[0]!
        password = parts[1]!
        host = parts[2]!
        port = asPort(parts[3])
      } else {
        return { ok: false, error: 'Could not find a valid port (1-65535).' }
      }
    }
  }

  if (!hostOk(host)) return { ok: false, error: 'The proxy host is not a valid hostname or IP address.' }
  if (port === null) return { ok: false, error: 'Enter a proxy port from 1 to 65535.' }
  if (username === '') username = null
  if (username === null) password = null
  return { ok: true, value: { proxy_type: proxyType, host, port, username, password } }
}
