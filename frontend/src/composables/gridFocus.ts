import { ref, watch } from 'vue'
/**
 * Live grid focus mode: hides the sidebar and top bar so the phones get the whole screen.
 * A layout preference only (never sensitive), kept separate from the memory-only shell
 * navigation state. Esc or the Exit focus button turns it off.
 */
const GRID_FOCUS_KEY = 'd3m0n.grid-focus.v1'
function readGridFocus(): boolean {
  try { return typeof localStorage !== 'undefined' && localStorage.getItem(GRID_FOCUS_KEY) === '1' } catch { return false }
}
export const gridFocus = ref(readGridFocus())
watch(gridFocus, value => { try { localStorage.setItem(GRID_FOCUS_KEY, value ? '1' : '0') } catch {} })
