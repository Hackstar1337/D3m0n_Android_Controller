<script setup lang="ts">
import { computed } from 'vue'

// Static, trusted 24x24 line-icon markup (stroke-based, Lucide-style geometry).
const ICONS: Record<string, string> = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  console: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3"/><path d="M13 15h4"/>',
  windows: '<rect x="3" y="3" width="12" height="12" rx="2"/><path d="M9 21h10a2 2 0 0 0 2-2V9"/>',
  sliders: '<path d="M4 6h10"/><path d="M18 6h2"/><circle cx="16" cy="6" r="2"/><path d="M4 12h3"/><path d="M11 12h9"/><circle cx="9" cy="12" r="2"/><path d="M4 18h12"/><path d="M20 18h0"/><circle cx="18" cy="18" r="2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18"/><path d="M12 3a14 14 0 0 0 0 18"/>',
  shield: '<path d="M12 21s7-3.5 7-9.5V5.5L12 3 5 5.5v6C5 17.5 12 21 12 21z"/><path d="m9 12 2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  box: '<path d="M21 8 12 3 3 8v8l9 5 9-5V8z"/><path d="m3 8 9 5 9-5"/><path d="M12 13v8"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-3-3 1.3-1.3a4 4 0 0 0-5-5L5 13 3 11l8-8 1.7 1.7z" transform="scale(.92) translate(1 1)"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  tool: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 1.5V4M15 1.5V4M9 20v2.5M15 20v2.5M1.5 9H4M1.5 15H4M20 9h2.5M20 15h2.5"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  flask: '<path d="M9 3h6"/><path d="M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3"/><path d="M7 15h10"/>',
  pulse: '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
  monitor: '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  book: '<path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H11v16H5.5A1.5 1.5 0 0 0 4 20.5z"/><path d="M20 4.5A1.5 1.5 0 0 0 18.5 3H13v16h5.5a1.5 1.5 0 0 1 1.5 1.5z"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 3v5h5"/><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 21v-5h-5"/>',
  panel: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/>',
  phone: '<rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  wifi: '<path d="M2 8.5a15 15 0 0 1 20 0"/><path d="M5 12a10.5 10.5 0 0 1 14 0"/><path d="M8.5 15.5a5.5 5.5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/>',
  usb: '<path d="M12 3v14"/><path d="m9 6 3-3 3 3"/><circle cx="12" cy="19" r="2"/><path d="M12 13 7 10V8"/><path d="m12 11 5-3V6.5"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
  unlock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 7.7-1.5"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9"/><path d="m16 7 3 3"/><path d="m18 5 2 2"/>',
  upload: '<path d="M12 15V3"/><path d="m7 8 5-5 5 5"/><path d="M20 15v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4"/>',
  download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M20 15v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 17 20l1-13"/><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5"/><circle cx="12" cy="17.3" r=".6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.8" r=".6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h4"/>',
  maximize: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  record: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  hash: '<path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  filter: '<path d="M3 5h18l-7 8.5V19l-4 2v-7.5z"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18.5 14a6.5 6.5 0 0 1 3 6"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  terminal: '<path d="m5 7 5 5-5 5"/><path d="M12 17h7"/>',
  logs: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
  eye: '<path d="M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7-9.5-7-9.5-7z"/><circle cx="12" cy="12" r="3"/>',
  dots: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  home: '<path d="M3 11 12 3l9 8"/><path d="M5 9.5V20h14V9.5"/>',
  back: '<path d="M10 7 5 12l5 5"/><path d="M5 12h14"/>',
  square: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  warning: '<path d="M12 3 22 20H2z"/><path d="M12 9v5"/><path d="M12 18h.01"/>',
  botplay: '<rect x="4" y="6" width="16" height="13" rx="3"/><path d="M8 3v3M16 3v3M8 13h.01M16 13h.01"/><path d="m11 16 3-2-3-2z"/>',
  power: '<path d="M12 3v8"/><path d="M17.7 6.6a8 8 0 1 1-11.4 0"/>',
  bot: '<rect x="5" y="8" width="14" height="11" rx="3.5"/><path d="M12 8V5.5"/><circle cx="12" cy="4.2" r="1.2"/><path d="M9.5 12.8v1.6M14.5 12.8v1.6"/><path d="M3 12.5v3M21 12.5v3"/>',
  shieldLock: '<path d="M12 21s7-3.5 7-9.5V5.5L12 3 5 5.5v6C5 17.5 12 21 12 21z"/><circle cx="12" cy="10.6" r="1.7"/><path d="M12 12.3v3"/>',
}

const props = withDefaults(defineProps<{ name: string; size?: number; strokeWidth?: number; title?: string }>(), { size: 16, strokeWidth: 1.75, title: '' })
const markup = computed(() => ICONS[props.name] || ICONS.square)
</script>

<template>
  <svg
    class="ui-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    :aria-hidden="title ? undefined : 'true'"
    :role="title ? 'img' : undefined"
    focusable="false"
  ><title v-if="title">{{ title }}</title><g v-html="markup"></g></svg>
</template>

<style scoped>
.ui-icon { flex: none; display: inline-block; vertical-align: middle; }
</style>
