<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import { dashboardAuth, refreshDashboardAuth, unlockDashboard } from '@/composables/dashboardAuth'

const password = ref('')
const reveal = ref(false)
const input = ref<HTMLInputElement | null>(null)
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null

const waitSeconds = computed(() => Math.max(0, Math.ceil((dashboardAuth.retryUntil - now.value) / 1000)))
const blocked = computed(() => waitSeconds.value > 0)
const waitLabel = computed(() => {
  const s = waitSeconds.value
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s}s`
})

async function submit() {
  if (blocked.value || dashboardAuth.busy) return
  const value = password.value
  password.value = ''
  reveal.value = false
  const ok = await unlockDashboard(value)
  if (!ok) await nextTick(() => input.value?.focus())
}

async function retry() {
  dashboardAuth.phase = 'checking'
  await refreshDashboardAuth()
}

watch(() => dashboardAuth.phase, phase => { if (phase === 'locked') nextTick(() => input.value?.focus()) })

onMounted(() => {
  ticker = setInterval(() => { now.value = Date.now() }, 1000)
  nextTick(() => input.value?.focus())
})
onBeforeUnmount(() => { if (ticker) clearInterval(ticker) })
</script>

<template>
  <div class="lock-screen">
    <div class="lock-backdrop" aria-hidden="true"></div>
    <main class="lock-card" aria-labelledby="lock-title">
      <header class="lock-brand">
        <img src="/mascot/01-front-view.png" alt="" class="lock-brand__mark" />
        <div class="lock-brand__text">
          <strong>D3m0n</strong>
          <span>in the droid</span>
        </div>
        <span class="lock-brand__badge"><UiIcon name="shield" :size="12" />Protected</span>
      </header>

      <section v-if="dashboardAuth.phase === 'checking'" class="lock-body lock-body--state" aria-live="polite">
        <span class="lock-spinner" aria-hidden="true"></span>
        <h1 id="lock-title" class="lock-title">Connecting to the fleet</h1>
        <p class="lock-sub">Checking the dashboard session…</p>
      </section>

      <section v-else-if="dashboardAuth.phase === 'offline'" class="lock-body lock-body--state" aria-live="polite">
        <div class="lock-state-icon is-warn"><UiIcon name="wifi" :size="20" /></div>
        <h1 id="lock-title" class="lock-title">Dashboard server unreachable</h1>
        <p class="lock-sub">The phone host or its tunnel may be restarting. It recovers automatically; try again in a moment.</p>
        <button type="button" class="lock-submit" @click="retry"><UiIcon name="refresh" :size="14" />Try again</button>
      </section>

      <section v-else-if="dashboardAuth.phase === 'unconfigured'" class="lock-body lock-body--state" aria-live="polite">
        <div class="lock-state-icon is-danger"><UiIcon name="alert" :size="20" /></div>
        <h1 id="lock-title" class="lock-title">Unlock password not configured</h1>
        <p class="lock-sub">The server has no dashboard password set, so public access stays locked. Set it on the phone host with <code>python -m ops.dashboard_auth set-password</code>.</p>
        <button type="button" class="lock-submit is-quiet" @click="retry"><UiIcon name="refresh" :size="14" />Check again</button>
      </section>

      <form v-else class="lock-body" autocomplete="on" @submit.prevent="submit">
        <div class="lock-heading">
          <div class="lock-state-icon"><UiIcon name="lock" :size="18" /></div>
          <div>
            <h1 id="lock-title" class="lock-title">Unlock dashboard</h1>
            <p class="lock-sub">Enter the dashboard password to access every device, stream and tool.</p>
          </div>
        </div>

        <p v-if="dashboardAuth.notice && !dashboardAuth.error" class="lock-notice" role="status"><UiIcon name="info" :size="13" />{{ dashboardAuth.notice }}</p>

        <input type="text" name="username" value="d3m0n-dashboard" autocomplete="username" class="visually-hidden" tabindex="-1" aria-hidden="true" readonly />
        <label class="lock-label" for="dashboard-password">Password</label>
        <div class="lock-field" :class="{ 'has-error': !!dashboardAuth.error }">
          <UiIcon name="key" :size="15" class="lock-field__icon" />
          <input
            id="dashboard-password"
            ref="input"
            v-model="password"
            :type="reveal ? 'text' : 'password'"
            name="password"
            autocomplete="current-password"
            spellcheck="false"
            autocapitalize="off"
            maxlength="256"
            placeholder="Dashboard password"
            :disabled="dashboardAuth.busy || blocked"
            :aria-invalid="!!dashboardAuth.error"
            aria-describedby="lock-feedback"
          />
          <button type="button" class="lock-field__toggle" :aria-pressed="reveal" :aria-label="reveal ? 'Hide password' : 'Show password'" :title="reveal ? 'Hide password' : 'Show password'" @click="reveal = !reveal">
            <UiIcon name="eye" :size="15" />
          </button>
        </div>

        <p id="lock-feedback" class="lock-feedback" :class="{ 'is-error': !!dashboardAuth.error }" role="alert" aria-live="assertive">
          <template v-if="dashboardAuth.error"><UiIcon name="alert" :size="13" />{{ dashboardAuth.error }}</template>
        </p>

        <button type="submit" class="lock-submit" :disabled="dashboardAuth.busy || blocked || !password">
          <span v-if="dashboardAuth.busy" class="lock-spinner is-small" aria-hidden="true"></span>
          <UiIcon v-else name="unlock" :size="14" />
          {{ dashboardAuth.busy ? 'Verifying…' : blocked ? `Locked out · ${waitLabel}` : 'Unlock dashboard' }}
        </button>

        <footer class="lock-meta">
          <span><UiIcon name="clock" :size="12" />Stays unlocked in this browser for {{ dashboardAuth.sessionDays }} days</span>
          <span><UiIcon name="lock" :size="12" />Lock anytime from the sidebar</span>
        </footer>
      </form>
    </main>
    <p class="lock-foot">D3m0n in the droid · Android fleet control</p>
  </div>
</template>

<style scoped>
.lock-screen {
  position: fixed; inset: 0; display: grid; place-items: center; align-content: center; gap: 18px;
  background: var(--ui-bg); color: var(--ui-text-1); font-family: var(--ui-font); overflow: auto; padding: 32px;
}
.lock-backdrop {
  position: absolute; inset: 0; pointer-events: none;
  background:
    radial-gradient(900px 480px at 50% -10%, rgba(63, 208, 168, 0.07), transparent 70%),
    linear-gradient(var(--ui-border) 1px, transparent 1px) 0 0 / 48px 48px,
    linear-gradient(90deg, var(--ui-border) 1px, transparent 1px) 0 0 / 48px 48px;
  opacity: 0.55;
  mask-image: radial-gradient(ellipse 70% 60% at 50% 40%, #000 30%, transparent 85%);
}
.lock-card {
  position: relative; width: min(420px, 100%); background: var(--ui-surface-1);
  border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-lg); box-shadow: var(--ui-shadow-pop);
}
.lock-brand { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--ui-border); }
.lock-brand__mark { width: 30px; height: 30px; object-fit: contain; }
.lock-brand__text { display: grid; line-height: 1.1; }
.lock-brand__text strong { font-size: 14px; font-weight: 700; letter-spacing: 0.01em; }
.lock-brand__text span { font-size: var(--ui-fs-2xs); color: var(--ui-text-3); letter-spacing: 0.04em; }
.lock-brand__badge {
  margin-left: auto; display: inline-flex; align-items: center; gap: 5px; height: 22px; padding: 0 8px;
  border: 1px solid var(--ui-accent-line); border-radius: 999px; background: var(--ui-accent-soft);
  color: var(--ui-accent); font-size: var(--ui-fs-2xs); font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
}
.lock-body { display: grid; gap: 10px; padding: 22px 22px 18px; }
.lock-body--state { justify-items: center; text-align: center; padding: 30px 26px 26px; gap: 10px; }
.lock-heading { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 6px; }
.lock-state-icon {
  flex: none; width: 36px; height: 36px; display: grid; place-items: center; border-radius: var(--ui-radius);
  background: var(--ui-accent-soft); color: var(--ui-accent); border: 1px solid var(--ui-accent-line);
}
.lock-state-icon.is-warn { background: var(--ui-warn-soft); color: var(--ui-warn); border-color: rgba(240, 180, 76, 0.35); }
.lock-state-icon.is-danger { background: var(--ui-danger-soft); color: var(--ui-danger); border-color: rgba(242, 109, 109, 0.35); }
.lock-title { margin: 0; font-size: var(--ui-fs-xl); font-weight: 650; letter-spacing: -0.01em; color: var(--ui-text-1); }
.lock-sub { margin: 4px 0 0; font-size: var(--ui-fs-sm); line-height: 1.5; color: var(--ui-text-3); }
.lock-sub code { font-family: var(--ui-mono); font-size: var(--ui-fs-xs); color: var(--ui-text-2); background: var(--ui-surface-3); padding: 1px 5px; border-radius: var(--ui-radius-xs); }
.lock-notice {
  display: flex; align-items: center; gap: 7px; margin: 0; padding: 8px 10px; border-radius: var(--ui-radius-sm);
  background: var(--ui-info-soft); color: var(--ui-info); font-size: var(--ui-fs-xs);
}
.lock-label { font-size: var(--ui-fs-xs); font-weight: 600; color: var(--ui-text-2); letter-spacing: 0.02em; }
.lock-field {
  display: flex; align-items: center; height: var(--ui-control-h-lg); background: var(--ui-surface-sunken);
  border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius); transition: border-color 0.15s var(--ui-ease), box-shadow 0.15s var(--ui-ease);
}
.lock-field:focus-within { border-color: var(--ui-border-focus); box-shadow: 0 0 0 3px var(--ui-accent-soft); }
.lock-field.has-error { border-color: var(--ui-danger); }
.lock-field__icon { margin: 0 4px 0 11px; color: var(--ui-text-4); flex: none; }
.lock-field input {
  flex: 1; min-width: 0; height: 100%; border: 0; outline: 0; background: transparent; color: var(--ui-text-1);
  font: inherit; font-size: var(--ui-fs-md); padding: 0 8px; letter-spacing: 0.02em; box-shadow: none; border-radius: 0;
}
/* The outer .lock-field frame shows focus; suppress the global input focus ring on the inner input. */
.lock-field input:focus, .lock-field input:focus-visible, .lock-field input:hover { border: 0; outline: 0; box-shadow: none; }
.lock-field input:-webkit-autofill { -webkit-text-fill-color: var(--ui-text-1); box-shadow: 0 0 0 40px var(--ui-surface-sunken) inset; transition: background-color 9999s; }
.lock-field input::placeholder { color: var(--ui-text-4); }
.lock-field input:disabled { cursor: not-allowed; opacity: 0.6; }
.lock-field__toggle {
  flex: none; width: 34px; height: 100%; display: grid; place-items: center; border: 0; background: transparent;
  color: var(--ui-text-3); cursor: pointer; border-left: 1px solid var(--ui-border); border-radius: 0 var(--ui-radius) var(--ui-radius) 0;
}
.lock-field__toggle:hover, .lock-field__toggle[aria-pressed='true'] { color: var(--ui-accent); background: var(--ui-surface-2); }
.lock-feedback { min-height: 18px; margin: 0; display: flex; align-items: center; gap: 6px; font-size: var(--ui-fs-xs); color: var(--ui-text-3); }
.lock-feedback.is-error { color: var(--ui-danger); }
.lock-submit {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: var(--ui-control-h-lg); width: 100%;
  border: 1px solid var(--ui-accent-strong); border-radius: var(--ui-radius); background: var(--ui-accent); color: var(--ui-on-accent);
  font: inherit; font-size: var(--ui-fs-md); font-weight: 650; cursor: pointer; transition: background 0.15s var(--ui-ease);
}
.lock-body--state .lock-submit { width: auto; padding: 0 18px; margin-top: 8px; }
.lock-submit:hover:not(:disabled) { background: var(--ui-accent-hover); }
.lock-submit:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.lock-submit:disabled { cursor: not-allowed; background: var(--ui-surface-3); border-color: var(--ui-border-strong); color: var(--ui-text-3); }
.lock-submit.is-quiet { background: var(--ui-surface-2); border-color: var(--ui-border-strong); color: var(--ui-text-1); }
.lock-meta {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 14px; margin-top: 6px; padding-top: 14px;
  border-top: 1px solid var(--ui-border); font-size: var(--ui-fs-2xs); color: var(--ui-text-4);
}
.lock-meta span { display: inline-flex; align-items: center; gap: 5px; }
.lock-foot { position: relative; margin: 0; font-size: var(--ui-fs-2xs); color: var(--ui-text-4); letter-spacing: 0.04em; }
.lock-spinner {
  width: 22px; height: 22px; border-radius: 50%; border: 2px solid var(--ui-border-strong); border-top-color: var(--ui-accent);
  animation: lock-spin 0.8s linear infinite;
}
.lock-spinner.is-small { width: 14px; height: 14px; border-width: 2px; border-color: rgba(3, 19, 13, 0.25); border-top-color: currentColor; }
.visually-hidden { position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@keyframes lock-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .lock-spinner { animation-duration: 2.4s; } }
</style>
