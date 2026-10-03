<!--
  StatePanel — the P7 state matrix for a content area: loading / empty / error.
  Copy is never baked in: every visible string (and the retry button itself)
  comes from the caller, so zh/en stay in the page's i18n table.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { Inbox, LoaderCircle, TriangleAlert } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    mode?: 'loading' | 'empty' | 'error'
    /** Heading, e.g. "No matching users". */
    title?: string
    /** One line of guidance: what happened + what to do. */
    description?: string
    /** Raw technical detail (error text, request id) — rendered as code. */
    detail?: string
    /**
     * Label for the built-in retry button (error mode). The button only
     * renders when this is provided; the #action slot takes precedence.
     */
    retryText?: string
  }>(),
  { mode: 'empty', title: '', description: '', detail: '', retryText: '' },
)

const emit = defineEmits<{ (e: 'retry'): void }>()

const role = computed(() => (props.mode === 'error' ? 'alert' : 'status'))
const live = computed(() =>
  props.mode === 'loading' ? 'polite' : props.mode === 'error' ? 'assertive' : 'off',
)
</script>

<template>
  <div
    class="state-panel"
    :class="`is-${mode}`"
    :role="role"
    :aria-live="live"
    :aria-busy="mode === 'loading' || undefined"
  >
    <span class="sp-icon" aria-hidden="true">
      <slot name="icon">
        <LoaderCircle v-if="mode === 'loading'" :size="20" class="sp-spin" />
        <TriangleAlert v-else-if="mode === 'error'" :size="20" />
        <Inbox v-else :size="20" />
      </slot>
    </span>
    <h3 v-if="title" class="sp-title">{{ title }}</h3>
    <p v-if="description" class="sp-desc">{{ description }}</p>
    <pre v-if="detail" class="sp-detail">{{ detail }}</pre>
    <div v-if="$slots.action || (mode === 'error' && retryText)" class="sp-actions">
      <slot name="action">
        <button v-if="mode === 'error' && retryText" type="button" class="sp-btn" @click="emit('retry')">
          {{ retryText }}
        </button>
      </slot>
    </div>
  </div>
</template>

<style scoped>
.state-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-1);
  padding: var(--sp-8) var(--sp-6);
  min-height: 180px;
  text-align: center;
  color: var(--text-secondary);
}

.sp-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  margin-bottom: var(--sp-2);
  border-radius: var(--r-lg);
  background: var(--surface-muted);
  color: var(--text-tertiary);
}
.is-error .sp-icon {
  background: var(--danger-bg);
  color: var(--danger);
}
.is-loading .sp-icon {
  background: var(--accent-soft);
  color: var(--accent-active);
}

.sp-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}

.sp-desc {
  margin: 0;
  max-width: 46ch;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  line-height: var(--lh-relaxed);
}

.sp-detail {
  margin: var(--sp-2) 0 0;
  max-width: 100%;
  overflow-x: auto;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-on-muted); /* gray-500 is 4.40:1 on the muted surface */
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  text-align: left;
  white-space: pre-wrap;
  word-break: break-word;
}

.sp-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}

.sp-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: var(--density-control-h, 30px); /* density register (P7) */
  padding: 0 var(--sp-4);
  border: 1px solid var(--accent);
  border-radius: var(--r-sm);
  background: var(--accent);
  color: var(--on-accent);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.sp-btn:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.sp-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.sp-spin {
  animation: sp-rotate 0.9s linear infinite;
}
@keyframes sp-rotate {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sp-spin {
    animation: none;
  }
}
</style>
