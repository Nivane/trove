<!--
  DiffCard — one pending-queue item as a decision card (P6).

  The point of the card shape over a table row: the evidence and the blast
  radius sit on the same screen as the decision, so approving stops being a
  guess. Copy comes from props/slots — nothing is hardcoded here, zh/en live
  in the page's i18n table.

  The checkbox is a plain input so keyboard/AT behavior is the browser's;
  it only requests a toggle, the parent owns the selection set.
-->
<script setup lang="ts">
import { Check, LoaderCircle, PenLine, X } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    /** Type badge text, e.g. "Lesson" / "Example". */
    kind: string
    /** Card heading — the lesson pattern / example question. */
    title: string
    /** Context chips (source / time / votes / confidence …). */
    meta?: { label: string; value: string }[]
    selected?: boolean
    /** Work in flight for this card: actions disabled, spinner shown. */
    busy?: boolean
    /** Non-empty when a confirm was refused by the certification gate. */
    refusedReason?: string
    confirmLabel: string
    rejectLabel: string
    /** Empty string hides the "edit then confirm" action. */
    editLabel?: string
    selectLabel?: string
    refusedLabel?: string
  }>(),
  {
    meta: () => [],
    selected: false,
    busy: false,
    refusedReason: '',
    editLabel: '',
    selectLabel: '',
    refusedLabel: '',
  },
)

const emit = defineEmits<{
  (e: 'toggle'): void
  (e: 'confirm'): void
  (e: 'reject'): void
  (e: 'edit-confirm'): void
}>()
</script>

<template>
  <article class="diff-card" :class="{ 'is-refused': !!refusedReason, 'is-busy': busy }">
    <header class="dc-head">
      <input
        class="dc-check"
        type="checkbox"
        :checked="selected"
        :disabled="busy"
        :aria-label="selectLabel"
        @change="emit('toggle')"
      >
      <span class="dc-kind">{{ kind }}</span>
      <h3 class="dc-title">{{ title }}</h3>
    </header>

    <ul v-if="meta.length" class="dc-meta">
      <li v-for="m in meta" :key="m.label" class="dc-chip">
        <span class="dc-chip-label">{{ m.label }}</span>
        <span class="dc-chip-value">{{ m.value }}</span>
      </li>
    </ul>

    <div v-if="$slots.evidence" class="dc-evidence">
      <slot name="evidence" />
    </div>

    <p v-if="refusedReason" class="dc-refused" role="alert">
      <span class="dc-refused-label">{{ refusedLabel }}</span>
      <span class="dc-refused-text">{{ refusedReason }}</span>
    </p>

    <div v-if="$slots.impact" class="dc-impact">
      <slot name="impact" />
    </div>

    <footer class="dc-actions">
      <button type="button" class="dc-btn is-primary" :disabled="busy" @click="emit('confirm')">
        <LoaderCircle v-if="busy" :size="13" class="dc-spin" aria-hidden="true" />
        <Check v-else :size="13" aria-hidden="true" />
        {{ confirmLabel }}
      </button>
      <button
        v-if="editLabel"
        type="button"
        class="dc-btn"
        :disabled="busy"
        @click="emit('edit-confirm')"
      >
        <PenLine :size="13" aria-hidden="true" />
        {{ editLabel }}
      </button>
      <button type="button" class="dc-btn is-danger" :disabled="busy" @click="emit('reject')">
        <X :size="13" aria-hidden="true" />
        {{ rejectLabel }}
      </button>
    </footer>
  </article>
</template>

<style scoped>
.diff-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}
.diff-card.is-refused {
  border-color: var(--danger);
  background: var(--danger-bg);
}
.diff-card.is-busy {
  opacity: 0.75;
}

.dc-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}

.dc-check {
  flex: none;
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
}
.dc-check:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.dc-kind {
  flex: none;
  padding: 1px var(--sp-2);
  border-radius: var(--r-full);
  background: var(--accent-soft);
  color: var(--accent-active);
  font-size: var(--fs-2xs);
  font-weight: 600;
}

.dc-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  line-height: var(--lh-tight);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.dc-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.dc-chip {
  display: inline-flex;
  align-items: baseline;
  gap: var(--sp-1);
  padding: 1px var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  background: var(--surface-muted);
  font-size: var(--fs-2xs);
  color: var(--text-primary);
}
.dc-chip-label {
  color: var(--text-secondary);
}

.dc-evidence {
  padding: var(--sp-2) var(--sp-3);
  border-left: 2px solid var(--border-default);
  background: var(--surface-muted);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.dc-refused {
  display: flex;
  gap: var(--sp-2);
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
}
.dc-refused-label {
  flex: none;
  font-weight: 600;
  color: var(--danger);
}
.dc-refused-text {
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.dc-impact {
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
  color: var(--text-secondary);
}

.dc-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.dc-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: 28px;
  padding: 0 var(--sp-3);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-2xs);
  font-weight: 500;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.dc-btn:not(:disabled):hover {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}
.dc-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.dc-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.dc-btn.is-primary {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--on-accent);
}
.dc-btn.is-primary:not(:disabled):hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.dc-btn.is-danger {
  color: var(--danger);
  border-color: var(--border-default);
}
.dc-btn.is-danger:not(:disabled):hover {
  border-color: var(--danger);
  background: var(--danger-bg);
}

.dc-spin {
  animation: dc-spin 0.9s linear infinite;
}
@keyframes dc-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .dc-spin {
    animation: none;
  }
}
</style>
