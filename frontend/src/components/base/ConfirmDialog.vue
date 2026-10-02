<!--
  ConfirmDialog — the one confirmation shape for the console.

  Title is the action as a short sentence ("Delete 3 users"), the #impact
  slot describes the blast radius, and the confirm button repeats the action
  verb instead of saying "OK". All copy is required by props — nothing is
  hardcoded here, so zh/en live in the page's i18n table.

  Cancel closes by itself (it always means "stop"); confirm only emits —
  the caller sets `loading`, awaits the work, then closes via v-model.
  Esc / backdrop cancel but are ignored while `loading` is true.
-->
<script setup lang="ts">
import { nextTick, ref, useId, watch } from 'vue'
import { LoaderCircle } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    /** Action short sentence, e.g. "Delete 3 users". */
    title: string
    /** Verb-phrase label for the confirm button, e.g. "Delete 3 users". */
    confirmText: string
    /** Label for the cancel button. */
    cancelText: string
    /** Destructive styling for the confirm button. */
    danger?: boolean
    /** Work in flight: buttons disabled, Esc/backdrop take no effect. */
    loading?: boolean
  }>(),
  { danger: false, loading: false },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'confirm'): void
  (e: 'cancel'): void
}>()

defineOptions({ inheritAttrs: false })

const uid = useId()
const titleId = `${uid}-title`
const descId = `${uid}-desc`
const panelEl = ref<HTMLElement | null>(null)
const cancelEl = ref<HTMLButtonElement | null>(null)
let restoreEl: HTMLElement | null = null

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      restoreEl = (document.activeElement as HTMLElement | null) ?? null
      await nextTick()
      // The safe action gets focus, never the destructive one.
      const target = cancelEl.value ?? panelEl.value
      target?.focus()
    } else {
      restoreEl?.focus?.()
      restoreEl = null
    }
  },
  { immediate: true },
)

function cancel() {
  if (props.loading) return
  emit('cancel')
  emit('update:modelValue', false)
}

function confirm() {
  if (props.loading) return
  emit('confirm')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    cancel()
    return
  }
  if (e.key !== 'Tab') return
  const panel = panelEl.value
  if (!panel) return
  const list = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
  if (!list.length) {
    e.preventDefault()
    panel.focus()
    return
  }
  const first = list[0]
  const last = list[list.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (e.shiftKey && (active === first || !panel.contains(active))) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="confirm-fade">
      <div v-if="modelValue" class="confirm-overlay" @click.self="cancel()">
        <div
          ref="panelEl"
          class="confirm-panel"
          :role="danger ? 'alertdialog' : 'dialog'"
          aria-modal="true"
          :aria-labelledby="titleId"
          :aria-describedby="$slots.impact ? descId : undefined"
          tabindex="-1"
          v-bind="$attrs"
          @keydown="onKeydown"
        >
          <h2 :id="titleId" class="confirm-title">{{ title }}</h2>

          <div class="confirm-body">
            <slot />
            <div v-if="$slots.impact" :id="descId" class="confirm-impact">
              <slot name="impact" />
            </div>
          </div>

          <div class="confirm-foot">
            <button
              ref="cancelEl"
              type="button"
              class="confirm-btn"
              :disabled="loading"
              @click="cancel()"
            >
              {{ cancelText }}
            </button>
            <button
              type="button"
              class="confirm-btn is-solid"
              :class="danger ? 'is-danger' : 'is-primary'"
              :disabled="loading"
              @click="confirm()"
            >
              <LoaderCircle v-if="loading" :size="14" class="confirm-spin" aria-hidden="true" />
              {{ confirmText }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.confirm-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-4);
  background: var(--overlay);
}

.confirm-panel {
  width: 440px;
  max-width: 92vw;
  padding: var(--sp-5) var(--sp-5) var(--sp-4);
  border-radius: var(--r-lg);
  background: var(--surface-raised);
  box-shadow: var(--shadow-xl);
  outline: none;
}

.confirm-title {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-md);
  font-weight: 600;
  line-height: var(--lh-tight);
  color: var(--text-primary);
}

.confirm-body {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.confirm-impact {
  margin-top: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  color: var(--text-primary);
}

.confirm-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  margin-top: var(--sp-5);
}

.confirm-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: 30px;
  padding: 0 var(--sp-4);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.confirm-btn:not(:disabled):hover {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}
.confirm-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.confirm-btn:disabled {
  opacity: 0.6;
  cursor: default;
}

.confirm-btn.is-solid {
  border-color: transparent;
  color: var(--on-accent);
}
.confirm-btn.is-primary {
  background: var(--accent);
}
.confirm-btn.is-primary:not(:disabled):hover {
  background: var(--accent-hover);
}
.confirm-btn.is-danger {
  background: var(--danger);
}
.confirm-btn.is-danger:not(:disabled):hover {
  background: var(--danger-hover);
}

.confirm-spin {
  animation: confirm-rotate 0.9s linear infinite;
}
@keyframes confirm-rotate {
  to {
    transform: rotate(360deg);
  }
}

.confirm-fade-enter-active,
.confirm-fade-leave-active {
  transition: opacity var(--dur) var(--ease);
}
.confirm-fade-enter-active .confirm-panel {
  transition: transform var(--dur) var(--ease-spring);
}
.confirm-fade-enter-from,
.confirm-fade-leave-to {
  opacity: 0;
}
.confirm-fade-enter-from .confirm-panel {
  transform: scale(0.97);
}

@media (prefers-reduced-motion: reduce) {
  .confirm-spin {
    animation: none;
  }
  .confirm-fade-enter-active,
  .confirm-fade-leave-active,
  .confirm-fade-enter-active .confirm-panel {
    transition: none;
  }
}
</style>
