<!--
  KpiTile — a KPI that is also a filter. Rendered as a real <button>
  (Enter/Space work for free) with aria-pressed reflecting `active`.
  Copy comes from props / slots; nothing is hardcoded here.
-->
<script setup lang="ts">
withDefaults(
  defineProps<{
    label: string
    value: string | number
    /** Context line under the value, e.g. "1 user without any datasource". */
    sub?: string
    /** Pressed state — the tile currently drives the list filter. */
    active?: boolean
    /** W5：出口对该角色不可达（如 analyst 的只读面外页面）——置灰并说明，
     *  不做「点了没反应」的静默死点击。 */
    disabled?: boolean
    /** 置灰原因（原生 title，悬停可见）。 */
    title?: string
  }>(),
  { sub: '', active: false, disabled: false, title: '' },
)

const emit = defineEmits<{ (e: 'click', event: MouseEvent): void }>()
</script>

<template>
  <button
    type="button"
    class="kpi-tile"
    :class="{ 'is-active': active }"
    :aria-pressed="active"
    :disabled="disabled"
    :title="title"
    @click="!disabled && emit('click', $event)"
  >
    <span class="kpi-label">{{ label }}</span>
    <span class="kpi-value"><slot name="value">{{ value }}</slot></span>
    <span v-if="sub || $slots.sub" class="kpi-sub"><slot name="sub">{{ sub }}</slot></span>
  </button>
</template>

<style scoped>
.kpi-tile {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  width: 100%;
  min-width: 0;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  text-align: left;
}
.kpi-tile:not(:disabled):hover {
  background: var(--surface-hover);
  border-color: var(--border-default);
}
.kpi-tile:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.kpi-tile.is-active {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.kpi-tile.is-active:not(:disabled):hover {
  background: var(--accent-soft-hover);
}

.kpi-label {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}
.kpi-value {
  font-size: var(--fs-md);
  font-weight: 600;
  line-height: var(--lh-tight);
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.kpi-sub {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.kpi-tile.is-active .kpi-label,
.kpi-tile.is-active .kpi-value,
.kpi-tile.is-active .kpi-sub {
  color: var(--accent-active);
}
</style>
