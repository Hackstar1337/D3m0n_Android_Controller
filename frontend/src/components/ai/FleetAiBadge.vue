<script setup lang="ts">
defineProps<{ active?: boolean; state?: string }>()
</script>
<template>
  <span v-if="active" class="fleet-ai-badge" :title="`Fleet AI ${state || 'active'} · native control lease remains authoritative`" role="status"><span aria-hidden="true">✦</span><span class="fleet-ai-badge__label">AI {{ state || 'active' }}</span></span>
</template>
<style scoped>
.fleet-ai-badge { display:inline-flex; align-items:center; gap:3px; padding:2px 5px; border:1px solid var(--ui-accent-line); border-radius:999px; background:var(--ui-accent-soft); color:#8eead0; font-size:9px; font-weight:700; letter-spacing:.04em; white-space:nowrap; }.fleet-ai-badge__label { text-transform:uppercase; }
</style>
