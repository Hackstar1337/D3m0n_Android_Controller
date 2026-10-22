// Fit screen: execute the real solveFitLayout/lanIp from PhoneAdminView.vue and check readable identity width at 20/50 devices.
import fs from 'node:fs'
const src = fs.readFileSync('src/views/PhoneAdminView.vue', 'utf8')
const results = []
const check = (ok, name) => results.push([!!ok, name])

const constStart = src.indexOf('const FIT_ASPECT')
const constEnd = src.indexOf('\n', src.indexOf('const FIT_MIN_HEIGHT'))
const fnStart = src.indexOf('function solveFitLayout')
const fnEnd = src.indexOf('\nfunction computeFitLayout')
check(constStart > 0 && fnStart > 0 && fnEnd > fnStart, 'solver present in source')
const body = src.slice(constStart, constEnd) + '\n' + src.slice(fnStart, fnEnd).replace(/: number/g, '') + '\nreturn { solveFitLayout, FIT_ASPECT, FIT_MIN_HEIGHT }'
const { solveFitLayout, FIT_ASPECT, FIT_MIN_HEIGHT } = new Function(body)()
const ssStart = src.indexOf('function lanIp')
const ssEnd = src.indexOf('\nfunction tileLabel')
const lanIp = new Function(src.slice(ssStart, ssEnd).replace('(d: any): string', '(d)').replace(' as string', '') + '\nreturn lanIp')()

// The Glass Hub sits above the stream: tile chrome = hub height + 2px border, compact hub when the full one leaves < HUB_COMPACT_BELOW.
const hubConst = name => Number((new RegExp('const ' + name + ' = (\\d+)').exec(src) || [])[1])
const HUB_FULL = hubConst('HUB_FULL_HEIGHT'), HUB_COMPACT = hubConst('HUB_COMPACT_HEIGHT'), HUB_BELOW = hubConst('HUB_COMPACT_BELOW')
check(HUB_FULL > HUB_COMPACT && HUB_COMPACT > 0 && HUB_BELOW > FIT_MIN_HEIGHT, 'hub height constants present')
const GAP = 6, CHROME = 2
function fits(n, W, H) {
  let chrome = HUB_FULL + CHROME, r = solveFitLayout(n, W, Math.max(H, FIT_MIN_HEIGHT + chrome), GAP, GAP, chrome)
  if (r.height < HUB_BELOW) { chrome = HUB_COMPACT + CHROME; r = solveFitLayout(n, W, Math.max(H, FIT_MIN_HEIGHT + chrome), GAP, GAP, chrome) }
  const rows = Math.ceil(n / r.columns)
  const tileW = (W - (r.columns - 1) * GAP) / r.columns
  const total = rows * (r.height + chrome) + (rows - 1) * GAP
  return { ...r, rows, hub: chrome === HUB_FULL + CHROME ? 'full' : 'compact', tileW: Math.round(tileW), total: Math.round(total), ok: total <= H + 0.5 && r.height <= tileW * FIT_ASPECT + 1 }
}
// Content area of the Live grid below its toolbar (sidebar + chrome removed) at common desktop sizes.
const fhd50 = fits(50, 1688, 770), qhd50 = fits(50, 2328, 1110), lap50 = fits(50, 1100, 440), fhd20 = fits(20, 1688, 770)
console.log('1080p x50', fhd50, '\n1440p x50', qhd50, '\n768p x50', lap50, '\n1080p x20', fhd20)
check(fhd50.tileW >= 140 && fhd50.height >= FIT_MIN_HEIGHT, `50 phones keep full identity width at 1080p; scroll if required (${fhd50.columns}x${fhd50.rows}, ${fhd50.height}px previews)`)
check(qhd50.ok && qhd50.tileW >= 140 && qhd50.height >= 120, `50 phones fit one 1440p screen (${qhd50.columns}x${qhd50.rows}, ${qhd50.height}px previews)`)
check(lap50.height >= FIT_MIN_HEIGHT && lap50.tileW >= 140 && lap50.total > 440, `50 phones on laptop preserve readable caps and scroll (${lap50.columns} columns, ${lap50.height}px)`)
check(fhd20.ok && fhd20.height >= 180 && fhd20.tileW >= 140, `20 phones get large previews at 1080p (${fhd20.height}px)`)
check(fhd20.hub === 'full' && fits(20, 1754, 700).hub === 'full', '20 phones keep the full hub (name + IP) at 1080p')
check(fhd50.hub === 'compact' && fhd50.ok, `50 phones at 1080p use the compact hub and fit one screen (${fhd50.height}px)`)
const one = fits(1, 1688, 770)
check(one.columns === 1 && one.ok, 'a single phone uses one column')
const many = solveFitLayout(200, 900, 300, GAP, GAP, CHROME)
check(many.columns > 0 && many.height === FIT_MIN_HEIGHT, 'too many phones fall back to a minimum readable size and scroll')
check(lanIp({ serial: '192.168.50.37:5555' }) === '192.168.50.37' && lanIp({ serial: 'R58M123ABCDE' }) === 'R58M123ABCDE' && lanIp({ serial: 'R58M1', ip: '10.0.0.5' }) === '10.0.0.5', 'tiles show the complete IP')

// Wiring contracts
check(src.includes("{ value: 'fit', label: 'Fit screen', density: 'wall'"), 'Fit screen layout preset')
check(src.includes(':ref="bindFitGrid"') && src.includes("'is-fit': isFitLayout"), 'grid bound to the fit observer')
check(src.includes("'--phone-preview-height': previewHeightCss"), 'preview height follows the fit result')
check(src.includes('{{ tileLabel(d) }}'), 'tiles show the full device name in every layout')
check(src.includes(':disabled="isFitLayout"'), 'manual columns disabled while Fit screen is active')
check(/watch\(\[isFitLayout, visibleDeviceCount, syncEnabled, syncArming, controlDevice, subTab/.test(src), 'fit recomputes on count, banner, dock and tab changes')
check(src.includes("window.removeEventListener('resize', scheduleFit)") && src.includes('fitObserver?.disconnect()'), 'fit observers cleaned up on unmount')
check(src.includes("if (isFitLayout.value) { const h = fitLayout.value.height; return h > 440 ? 'hd' : h > 300 ? 'grid' : 'wall' }"), 'auto quality follows the fitted tile size')
check(src.includes('.multi-header .ui-select { width: auto; flex: none; }') && src.includes('.multi-header .grid-view-options { flex-wrap: nowrap; }'), 'toolbar controls keep natural width')

let failed = 0
for (const [ok, name] of results) { console.log((ok ? 'PASS ' : 'FAIL ') + name); if (!ok) failed++ }
console.log(`${results.length - failed}/${results.length} fit checks passed`)
process.exit(failed ? 1 : 0)
