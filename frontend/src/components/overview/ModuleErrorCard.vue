<!--
  ModuleErrorCard — a degraded block stays a block (设计稿 §3:块内降级).

  The overview endpoint returns per-block failures instead of failing the
  page, so every block needs a designed error face: the block's own slot
  shows what failed (type name only), when it failed, and a retry — no
  full-page toast, no blanking of the neighbouring blocks.

  Presentational only: all copy (including the retry label) is the
  caller's, following the base-component rule.
-->
<script setup lang="ts">
import { TriangleAlert } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    /** Block name, e.g. "Usage" — the card says which block is dark. */
    title: string
    /** One line of machine fact: error type name (+ source when useful). */
    detail?: string
    /** Optional guidance line (what still works, where to look). */
    hint?: string
    /** Renders the retry button when set. */
    retryText?: string
  }>(),
  { detail: '', hint: '', retryText: '' },
)

const emit = defineEmits<{ (e: 'retry'): void }>()
</script>

<template>
  <div class="module-error" role="alert">
    <span class="me-icon" aria-hidden="true"><TriangleAlert :size="16" /></span>
    <div class="me-text">
      <h4 class="me-title">{{ title }}</h4>
      <p v-if="detail" class="me-detail">{{ detail }}</p>
      <p v-if="hint" class="me-hint">{{ hint }}</p>
    </div>
    <button v-if="retryText" type="button" class="me-retry" @click="emit('retry')">
      {{ retryText }}
    </button>
  </div>
</template>

<style scoped>
.module-error {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--danger);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}

.me-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: var(--r-sm);
  background: var(--danger-bg);
  color: var(--danger);
}

.me-text {
  min-width: 0;
  flex: 1;
}

.me-title {
  margin: 0;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
}

.me-detail {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  word-break: break-word;
}

.me-hint {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.me-retry {
  flex: none;
  height: 26px;
  padding: 0 var(--sp-3);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-2xs);
  font-weight: 500;
}
.me-retry:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}
.me-retry:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
</style>
