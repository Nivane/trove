<!--
  AssetDrawer — detail panel for one KB asset file (format / generator /
  edit state / adoption), closing with the three dispositions.

  Purely presentational: the parent computes the field rows and supplies all
  copy (zh/en live in the page's i18n table). Disposition clicks are emitted,
  never executed here — merge/overwrite must go through the page's
  blast-radius ConfirmDialog first.
-->
<script setup lang="ts">
import DetailDrawer from '../base/DetailDrawer.vue'
import { RefreshCw, Recycle, Trash2, TriangleAlert } from 'lucide-vue-next'

withDefaults(
  defineProps<{
    modelValue: boolean
    title: string
    closeLabel: string
    /** aria-label fallback for when no `title` is rendered. */
    ariaLabel?: string
    fields?: { label: string; value: string }[]
    /** Warning blocks (refusal reason / format note): label + wrapped text. */
    notes?: { label: string; text: string; danger?: boolean }[]
    /** Section heading above the disposition buttons. */
    dispositionLabel?: string
    dispositions?: { id: string; label: string; desc: string; danger?: boolean }[]
  }>(),
  {
    fields: () => [],
    notes: () => [],
    ariaLabel: '',
    dispositionLabel: '',
    dispositions: () => [],
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'close'): void
  (e: 'disposition', id: string): void
}>()

function iconFor(id: string) {
  if (id === 'overwrite' || id === 'delete') return Trash2
  if (id === 'merge') return Recycle
  return RefreshCw
}
</script>

<template>
  <DetailDrawer
    :model-value="modelValue"
    :title="title"
    :close-label="closeLabel"
    :aria-label="ariaLabel"
    width="480px"
    @update:model-value="emit('update:modelValue', $event)"
    @close="emit('close')"
  >
    <dl v-if="fields.length" class="ad-fields">
      <div v-for="f in fields" :key="f.label" class="ad-field">
        <dt>{{ f.label }}</dt>
        <dd>{{ f.value }}</dd>
      </div>
    </dl>

    <div
      v-for="(n, i) in notes"
      :key="i"
      class="ad-note"
      :class="{ 'is-danger': n.danger }"
      role="note"
    >
      <TriangleAlert :size="14" aria-hidden="true" />
      <div class="ad-note-body">
        <strong class="ad-note-label">{{ n.label }}</strong>
        <p class="ad-note-text">{{ n.text }}</p>
      </div>
    </div>

    <section v-if="dispositions.length" class="ad-zone">
      <h3 class="ad-zone-title">{{ dispositionLabel }}</h3>
      <button
        v-for="d in dispositions"
        :key="d.id"
        type="button"
        class="ad-action"
        :class="{ 'is-danger': d.danger }"
        @click="emit('disposition', d.id)"
      >
        <component :is="iconFor(d.id)" :size="15" aria-hidden="true" />
        <span class="ad-action-body">
          <span class="ad-action-label">{{ d.label }}</span>
          <span class="ad-action-desc">{{ d.desc }}</span>
        </span>
      </button>
    </section>
  </DetailDrawer>
</template>

<style scoped>
.ad-fields {
  margin: 0 0 var(--sp-4);
  padding: 0;
}

.ad-field {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.ad-field dt {
  flex: none;
  width: 110px;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.ad-field dd {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.ad-note {
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-3);
  margin-bottom: var(--sp-4);
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-secondary);
}
.ad-note.is-danger {
  border-color: var(--danger);
  background: var(--danger-bg);
  color: var(--danger);
}
.ad-note-body {
  min-width: 0;
}
.ad-note-label {
  display: block;
  font-size: var(--fs-2xs);
  font-weight: 600;
}
.ad-note-text {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.ad-zone {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.ad-zone-title {
  margin: 0;
  font-size: var(--fs-2xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}

.ad-action {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: var(--text-primary);
  text-align: left;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.ad-action:hover {
  border-color: var(--border-strong);
  background: var(--surface-hover);
}
.ad-action:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.ad-action.is-danger {
  color: var(--danger);
}
.ad-action.is-danger:hover {
  border-color: var(--danger);
  background: var(--danger-bg);
}

.ad-action-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.ad-action-label {
  font-size: var(--fs-xs);
  font-weight: 600;
}
.ad-action-desc {
  font-size: var(--fs-2xs);
  line-height: var(--lh-normal);
  color: var(--text-secondary);
}
</style>
