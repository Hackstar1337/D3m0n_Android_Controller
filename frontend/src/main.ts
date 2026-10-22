import './assets/main.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import DashboardRoot from './DashboardRoot.vue'
import { installDashboardAuthGuard } from './composables/dashboardAuth'

// Detect an ended session on any API call and show the lock screen again.
installDashboardAuthGuard()

const app = createApp(DashboardRoot)
app.use(createPinia())
app.mount('#app')
