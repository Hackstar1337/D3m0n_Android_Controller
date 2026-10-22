<script setup lang="ts">
/** Confirmation dialog for rebooting phones; preflight data comes from useFleetReboot. */
import { computed, nextTick, ref, watch } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import { canConfirmReboot, rebootPlan, type RebootDialog } from '@/composables/fleetReboot'

const props = defineProps<{ dialog: RebootDialog | null; label: (serial: string) => string }>()
const emit = defineEmits<{ cancel: []; confirm: []; acknowledge: [value: boolean] }>()

const plan = computed(() => props.dialog ? rebootPlan(props.dialog.devices, props.dialog.checks) : null)
const canConfirm = computed(() => canConfirmReboot(props.dialog))
const box = ref<HTMLElement | null>(null)
function list(serials: string[]): string { return serials.map(props.label).join(', ') }
const title = computed(() => {
  const devices = props.dialog?.devices || []
  return devices.length === 1 ? 'Reboot phone ' + props.label(devices[0] || '') + '?' : `Reboot ${devices.length} phones?`
})
const confirmText = computed(() => {
  if (props.dialog?.busy) return 'Rebooting…'
  const count = plan.value?.eligible.length || 0
  return `Reboot ${count} ${count === 1 ? 'phone' : 'phones'}`
})
watch(() => !!props.dialog, open => { if (open) void nextTick(() => box.value?.focus()) })
</script>

<template>
  <Teleport to="body">
    <div v-if="dialog" class="reboot-dialog-backdrop" @click.self="emit('cancel')">
      <div ref="box" class="reboot-dialog" role="alertdialog" aria-modal="true" aria-labelledby="reboot-dialog-title" tabindex="-1" @keydown.esc="emit('cancel')">
        <h3 id="reboot-dialog-title"><UiIcon name="refresh" :size="15" />{{ title }}</h3>
        <p class="reboot-dialog__phones">Phones: {{ list(dialog.devices) }}</p>
        <p v-if="dialog.loading" class="reboot-dialog__muted">Checking USB connections…</p>
        <template v-else-if="plan && dialog.checks">
          <div v-if="plan.withoutUsb.length" class="reboot-dialog__warn">
            <UiIcon name="alert" :size="14" />
            <div>
              <strong>Not on USB: {{ list(plan.withoutUsb) }}</strong>
              <span>After rebooting, {{ plan.withoutUsb.length === 1 ? 'this phone goes offline and stays' : 'these phones go offline and stay' }} off the dashboard until plugged into the Ubuntu PC by USB. It then reconnects automatically.</span>
            </div>
          </div>
          <p v-if="plan.withUsb.length" class="reboot-dialog__ok"><UiIcon name="usb" :size="13" />On USB, reconnects automatically: {{ list(plan.withUsb) }}</p>
          <p v-if="plan.busy.length" class="reboot-dialog__muted">Skipped — Agent task running: {{ list(plan.busy) }}</p>
          <p v-if="plan.offline.length" class="reboot-dialog__muted">Skipped — offline: {{ list(plan.offline) }}</p>
          <p v-if="plan.already.length" class="reboot-dialog__muted">Already rebooting: {{ list(plan.already) }}</p>
          <label v-if="plan.withoutUsb.length" class="reboot-dialog__ack">
            <input type="checkbox" :checked="dialog.acknowledged" @change="emit('acknowledge', ($event.target as HTMLInputElement).checked)" />
            I understand {{ plan.withoutUsb.length === 1 ? 'this phone' : 'these phones' }} will stay offline until plugged in by USB
          </label>
        </template>
        <p v-if="dialog.error" class="reboot-dialog__error">{{ dialog.error }}</p>
        <div class="reboot-dialog__actions">
          <button type="button" class="reboot-dialog__btn" @click="emit('cancel')">Cancel</button>
          <button type="button" class="reboot-dialog__btn reboot-dialog__confirm" :disabled="!canConfirm" @click="emit('confirm')">{{ confirmText }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.reboot-dialog-backdrop { position: fixed; inset: 0; z-index: 4000; display: grid; place-items: center; background: rgba(4, 6, 9, .62); backdrop-filter: blur(3px); }
.reboot-dialog { width: min(440px, calc(100vw - 32px)); padding: 18px 18px 14px; border: 1px solid var(--ui-border-strong); border-radius: 12px; background: var(--ui-surface-1); box-shadow: var(--ui-shadow-pop); color: var(--ui-text-2); font-size: var(--ui-fs-sm); outline: none; }
.reboot-dialog h3 { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; color: var(--ui-text-1); font-size: 15px; font-weight: 650; }
.reboot-dialog h3 :deep(svg) { color: var(--ui-danger); }
.reboot-dialog p { margin: 6px 0; }
.reboot-dialog__phones { color: var(--ui-text-1); font-variant-numeric: tabular-nums; }
.reboot-dialog__muted { color: var(--ui-text-3); }
.reboot-dialog__ok { display: flex; align-items: center; gap: 6px; color: var(--ui-success); }
.reboot-dialog__warn { display: flex; gap: 10px; margin: 10px 0; padding: 10px 12px; border: 1px solid rgba(240, 180, 76, .35); border-radius: 8px; background: var(--ui-warn-soft); color: var(--ui-warn); }
.reboot-dialog__warn div { display: flex; flex-direction: column; gap: 3px; }
.reboot-dialog__warn span { color: var(--ui-text-2); line-height: 1.4; }
.reboot-dialog__ack { display: flex; align-items: flex-start; gap: 8px; margin: 10px 0 2px; color: var(--ui-text-1); line-height: 1.35; cursor: pointer; }
.reboot-dialog__ack input { margin-top: 2px; accent-color: var(--ui-danger); }
.reboot-dialog__error { color: var(--ui-danger); }
.reboot-dialog__actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.reboot-dialog__btn { height: 30px; padding: 0 14px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); background: var(--ui-surface-3); color: var(--ui-text-1); font: inherit; font-weight: 600; cursor: pointer; }
.reboot-dialog__btn:hover:not(:disabled) { background: var(--ui-surface-hover); }
.reboot-dialog__btn:disabled { opacity: .45; cursor: not-allowed; }
.reboot-dialog__confirm:not(:disabled) { background: var(--ui-danger-soft); border-color: rgba(242, 109, 109, .5); color: #ffb0b0; }
.reboot-dialog__confirm:hover:not(:disabled) { background: rgba(242, 109, 109, .2); color: #ffd0d0; }
</style>
