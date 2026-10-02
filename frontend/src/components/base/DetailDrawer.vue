<!--
  DetailDrawer — right-hand detail panel that keeps the list in context.
  Closes on Esc / backdrop / the ✕ button, all routed through the optional
  `beforeClose` guard (return false — or a promise resolving false — to veto,
  e.g. while there are unsaved edits). Focus moves in on open, is trapped
  while open, and returns to the trigger on close.
-->
<script setup lang="ts">
import { computed, nextTick, ref, useId, watch } from 'vue'
import { X } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title?: string
    /** Veto hook for every close path, including Esc and the backdrop. */
    beforeClose?: () => boolean | Promise<boolean>
    /** Panel width; any CSS length. */
    width?: string
    /** Accessible name for the close button (i18n copy lives in the caller). */
    closeLabel?: string
    /** aria-label fallback for when no `title` is rendered. */
    ariaLabel?: string
  }>(),
  { title: '', width: '420px', closeLabel: '', ariaLabel: '' },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'close'): void
}>()

defineOptions({ inheritAttrs: false })

const uid = useId()
const titleId = `${uid}-title`
const panelEl = ref<HTMLElement | null>(null)
let restoreEl: HTMLElement | null = null
let guardBusy = false

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusables(): HTMLElement[] {
  const panel = panelEl.value
  if (!panel) return []
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
}

watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      restoreEl = (document.activeElement as HTMLElement | null) ?? null
      await nextTick()
      const target = focusables()[0] ?? panelEl.value
      target?.focus()
    } else {
      restoreEl?.focus?.()
      restoreEl = null
    }
  },
  { immediate: true },
)

/** A throwing guard must not close the drawer — deny, never guess. */
async function runGuard(guard: () => boolean | Promise<boolean>): Promise<boolean> {
  try {
    return await guard()
  } catch {
    return false
  }
}

async function requestClose() {
  if (guardBusy) return
  const guard = props.beforeClose
  if (guard) {
    guardBusy = true
    const ok = await runGuard(guard)
    guardBusy = false
    if (!ok) return
  }
  emit('update:modelValue', false)
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    void requestClose()
    return
  }
  if (e.key !== 'Tab') return
  const list = focusables()
  const panel = panelEl.value
  if (!panel) return
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

const open = computed(() => props.modelValue)
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div v-if="open" class="drawer-overlay" @click.self="requestClose()">
        <section
          ref="panelEl"
          class="drawer-panel"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="!$slots.header && title ? titleId : undefined"
          :aria-label="!$slots.header && title ? undefined : ariaLabel"
          :style="{ width }"
          tabindex="-1"
          v-bind="$attrs"
          @keydown="onKeydown"
        >
          <header class="drawer-head">
            <slot name="header">
              <h2 v-if="title" :id="titleId" class="drawer-title">{{ title }}</h2>
            </slot>
            <button
              type="button"
              class="drawer-close"
              :aria-label="closeLabel"
              @click="requestClose()"
            >
              <X :size="16" />
            </button>
          </header>

          <div class="drawer-body">
            <slot />
          </div>

          <footer v-if="$slots.footer" class="drawer-foot">
            <slot name="footer" />
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
  background: var(--overlay);
}

.drawer-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 92vw;
  background: var(--surface-raised);
  border-left: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-xl);
  outline: none;
}

.drawer-head {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}

.drawer-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
  line-height: var(--lh-tight);
  color: var(--text-primary);
}

.drawer-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: var(--r-sm);
  color: var(--text-secondary);
}
.drawer-close:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.drawer-close:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-5);
}

.drawer-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border-subtle);
}

.drawer-fade-enter-active,
.drawer-fade-leave-active {
  transition: opacity var(--dur) var(--ease);
}
.drawer-fade-enter-from,
.drawer-fade-leave-to {
  opacity: 0;
}
.drawer-fade-enter-active .drawer-panel {
  transition: transform var(--dur-slow) var(--ease);
}
.drawer-fade-enter-from .drawer-panel {
  transform: translateX(100%);
}

@media (prefers-reduced-motion: reduce) {
  .drawer-fade-enter-active,
  .drawer-fade-leave-active,
  .drawer-fade-enter-active .drawer-panel {
    transition: none;
  }
}
</style>
