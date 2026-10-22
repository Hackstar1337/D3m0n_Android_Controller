<script setup lang="ts">
// The full application (streams, polling, WebSockets) is mounted only after the
// dashboard is unlocked, and unmounted again if the session ends or is locked.
import { onMounted } from 'vue'
import App from './App.vue'
import DashboardLock from './components/DashboardLock.vue'
import { dashboardAuth, refreshDashboardAuth } from './composables/dashboardAuth'

onMounted(() => { void refreshDashboardAuth() })
</script>

<template>
  <App v-if="dashboardAuth.phase === 'unlocked'" />
  <DashboardLock v-else />
</template>
