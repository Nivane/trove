<!--
  DataTable — the console list primitive (P6).

  Deliberately presentational: it renders rows and emits intent
  (sort / selection / row-click). Ordering, paging and fetching stay the
  caller's job, because the console standard is server-side lists whose
  state lives in the URL (see useListQuery). Slot contract per column:
  #cell-<key> receives { row, value, index }.
-->
<script lang="ts">
export interface DataTableColumn {
  /** Row field rendered in this column; also names the #cell-<key> slot. */
  key: string
  label: string
  sortable?: boolean
  /** First direction when the user sorts by this column (default 'asc'). */
  defaultDir?: 'asc' | 'desc'
  width?: string | number
  align?: 'left' | 'center' | 'right'
}

export interface DataTableSort {
  key: string
  dir: 'asc' | 'desc'
}
</script>

<script setup lang="ts">
import { computed } from 'vue'
import { Check, ChevronDown, ChevronUp, ChevronsUpDown, Minus } from 'lucide-vue-next'
import StatePanel from './StatePanel.vue'

const props = withDefaults(
  defineProps<{
    columns: DataTableColumn[]
    rows: readonly unknown[]
    /**
     * Stable row identity for selection and keys — a field name or a getter.
     * Defaults to the row index, which is only safe for small client-side lists.
     */
    rowKey?: string | ((row: unknown, index: number) => string | number)
    selectable?: boolean
    /** v-model:selected — row keys, kept across pages (server lists paginate). */
    selected?: readonly (string | number)[]
    /** v-model:sort — emitted on header click; the caller applies the ordering. */
    sort?: DataTableSort | null
    /** Skeleton rows while the first page loads; existing rows stay on refresh. */
    loading?: boolean
    skeletonRows?: number
    /** Rows become keyboard-activatable (tabindex + Enter) — use when row-click matters. */
    rowClickable?: boolean
    /** Accessible name for the select-all checkbox (i18n copy lives in the caller). */
    selectAllLabel?: string
    /** Accessible name prefix for row checkboxes; the row key is appended. */
    selectRowLabel?: string
    /** Fallback copy for the empty slot. */
    emptyText?: string
    emptyDescription?: string
  }>(),
  {
    selectable: false,
    selected: () => [],
    sort: null,
    loading: false,
    skeletonRows: 5,
    rowClickable: false,
    selectAllLabel: '',
    selectRowLabel: '',
    emptyText: '',
    emptyDescription: '',
  },
)

const emit = defineEmits<{
  (e: 'update:selected', keys: (string | number)[]): void
  (e: 'update:sort', sort: DataTableSort): void
  (e: 'row-click', row: unknown, index: number): void
}>()

/* ── row identity ─────────────────────────────────────────────── */

function keyOf(row: unknown, index: number): string | number {
  const rk = props.rowKey
  if (typeof rk === 'function') return rk(row, index)
  if (typeof rk === 'string' && row !== null && typeof row === 'object') {
    const v = (row as Record<string, unknown>)[rk]
    if (typeof v === 'string' || typeof v === 'number') return v
  }
  return index
}

function cellValue(row: unknown, key: string): unknown {
  if (row === null || typeof row !== 'object') return undefined
  return (row as Record<string, unknown>)[key]
}

function display(v: unknown): string {
  if (v === null || v === undefined) return '—'
  return String(v)
}

/* ── selection ────────────────────────────────────────────────── */

const selectedSet = computed(() => new Set(props.selected))

function isSelected(row: unknown, index: number): boolean {
  return selectedSet.value.has(keyOf(row, index))
}

const allSelected = computed(
  () => props.rows.length > 0 && props.rows.every((row, i) => isSelected(row, i)),
)
const someSelected = computed(
  () => props.rows.some((row, i) => isSelected(row, i)) && !allSelected.value,
)

function toggleRow(row: unknown, index: number) {
  const key = keyOf(row, index)
  const next = props.selected.filter((k) => k !== key)
  if (next.length === props.selected.length) next.push(key)
  emit('update:selected', [...next])
}

function toggleAll() {
  if (!props.rows.length) return
  if (allSelected.value) {
    const drop = new Set(props.rows.map((row, i) => keyOf(row, i)))
    emit(
      'update:selected',
      props.selected.filter((k) => !drop.has(k)),
    )
    return
  }
  const have = new Set(props.selected)
  const add = props.rows.map((row, i) => keyOf(row, i)).filter((k) => !have.has(k))
  emit('update:selected', [...props.selected, ...add])
}

/* ── sorting ──────────────────────────────────────────────────── */

function ariaSort(col: DataTableColumn): 'ascending' | 'descending' | 'none' | undefined {
  if (!col.sortable) return undefined
  if (props.sort?.key !== col.key) return 'none'
  return props.sort.dir === 'asc' ? 'ascending' : 'descending'
}

function isSorted(col: DataTableColumn, dir: 'asc' | 'desc'): boolean {
  return props.sort?.key === col.key && props.sort.dir === dir
}

function toggleSort(col: DataTableColumn) {
  if (!col.sortable) return
  const current = props.sort
  const next: DataTableSort =
    current && current.key === col.key
      ? { key: col.key, dir: current.dir === 'asc' ? 'desc' : 'asc' }
      : { key: col.key, dir: col.defaultDir ?? 'asc' }
  emit('update:sort', next)
}

/* ── layout ───────────────────────────────────────────────────── */

const colCount = computed(() => props.columns.length + (props.selectable ? 1 : 0))
const showSkeleton = computed(() => props.loading && props.rows.length === 0)
const showEmpty = computed(() => !props.loading && props.rows.length === 0)
/** Refresh with data on screen: keep the rows, show the top progress bar. */
const showProgress = computed(() => props.loading && props.rows.length > 0)

function sizeStyle(col: DataTableColumn): Record<string, string> | undefined {
  if (col.width === undefined) return undefined
  return { width: typeof col.width === 'number' ? `${col.width}px` : col.width }
}

function alignClass(col: DataTableColumn): string | undefined {
  return col.align && col.align !== 'left' ? `is-${col.align}` : undefined
}

function skeletonWidth(i: number): string {
  return `${55 + ((i * 17) % 35)}%`
}
</script>

<template>
  <div class="data-table" :aria-busy="loading || undefined">
    <div v-if="showProgress" class="dt-progress" aria-hidden="true">
      <span class="dt-progress-bar" />
    </div>

    <div class="dt-scroll">
      <table class="dt-table">
        <thead>
          <tr>
            <th v-if="selectable" class="dt-th is-check" scope="col">
              <button
                type="button"
                class="dt-check"
                role="checkbox"
                :class="{ 'is-on': allSelected, 'is-mixed': someSelected }"
                :aria-checked="allSelected ? 'true' : someSelected ? 'mixed' : 'false'"
                :aria-label="selectAllLabel"
                :disabled="!rows.length"
                @click="toggleAll"
              >
                <Check v-if="allSelected" :size="11" />
                <Minus v-else-if="someSelected" :size="11" />
              </button>
            </th>
            <th
              v-for="col in columns"
              :key="col.key"
              scope="col"
              class="dt-th"
              :class="[alignClass(col), { 'is-sortable': col.sortable }]"
              :style="sizeStyle(col)"
              :aria-sort="ariaSort(col)"
            >
              <button
                v-if="col.sortable"
                type="button"
                class="dt-sort"
                @click="toggleSort(col)"
              >
                <span class="dt-th-label">{{ col.label }}</span>
                <ChevronUp
                  v-if="isSorted(col, 'asc')"
                  :size="12"
                  class="dt-sort-ico"
                  aria-hidden="true"
                />
                <ChevronDown
                  v-else-if="isSorted(col, 'desc')"
                  :size="12"
                  class="dt-sort-ico"
                  aria-hidden="true"
                />
                <ChevronsUpDown
                  v-else
                  :size="12"
                  class="dt-sort-ico is-idle"
                  aria-hidden="true"
                />
              </button>
              <span v-else class="dt-th-label">{{ col.label }}</span>
            </th>
          </tr>
        </thead>

        <tbody>
          <template v-if="showSkeleton">
            <tr v-for="n in skeletonRows" :key="`sk-${n}`" class="dt-skel-row" aria-hidden="true">
              <td v-if="selectable" class="dt-td is-check">
                <span class="dt-skel is-box" />
              </td>
              <td
                v-for="(col, ci) in columns"
                :key="col.key"
                class="dt-td"
                :class="alignClass(col)"
              >
                <span class="dt-skel" :style="{ width: skeletonWidth(ci) }" />
              </td>
            </tr>
          </template>

          <template v-else-if="showEmpty">
            <tr class="dt-empty-row">
              <td class="dt-td dt-empty-cell" :colspan="colCount">
                <slot name="empty">
                  <StatePanel
                    mode="empty"
                    :title="emptyText"
                    :description="emptyDescription"
                  />
                </slot>
              </td>
            </tr>
          </template>

          <template v-else>
            <tr
              v-for="(row, i) in rows"
              :key="keyOf(row, i)"
              class="dt-row"
              :class="{ 'is-selected': selectable && isSelected(row, i), 'is-clickable': rowClickable }"
              :tabindex="rowClickable ? 0 : undefined"
              @click="emit('row-click', row, i)"
              @keydown.enter="emit('row-click', row, i)"
            >
              <td v-if="selectable" class="dt-td is-check">
                <button
                  type="button"
                  class="dt-check"
                  role="checkbox"
                  :class="{ 'is-on': isSelected(row, i) }"
                  :aria-checked="isSelected(row, i) ? 'true' : 'false'"
                  :aria-label="selectRowLabel ? `${selectRowLabel} ${keyOf(row, i)}` : undefined"
                  @click.stop="toggleRow(row, i)"
                >
                  <Check v-if="isSelected(row, i)" :size="11" />
                </button>
              </td>
              <td
                v-for="col in columns"
                :key="col.key"
                class="dt-td"
                :class="alignClass(col)"
              >
                <slot
                  :name="`cell-${col.key}`"
                  :row="row"
                  :value="cellValue(row, col.key)"
                  :index="i"
                >
                  {{ display(cellValue(row, col.key)) }}
                </slot>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.data-table {
  position: relative;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  overflow: hidden;
}

/* refresh with data on screen — thin indeterminate bar, rows stay put */
.dt-progress {
  position: relative;
  height: 2px;
  overflow: hidden;
  background: var(--accent-soft);
}
.dt-progress-bar {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 40%;
  background: var(--accent);
  animation: dt-slide 1.1s var(--ease) infinite;
}
@keyframes dt-slide {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(350%);
  }
}

.dt-scroll {
  overflow-x: auto;
}

.dt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}

.dt-th {
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-weight: 500;
  text-align: left;
  white-space: nowrap;
}
.dt-th.is-check {
  width: 34px;
  padding-right: 0;
}
.dt-th.is-center {
  text-align: center;
}
.dt-th.is-right {
  text-align: right;
}

.dt-th-label {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.dt-sort {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  margin: calc(-1 * var(--sp-1)) calc(-1 * var(--sp-1));
  padding: var(--sp-1);
  border-radius: var(--r-sm);
  color: inherit;
  font: inherit;
  font-weight: inherit;
}
.dt-sort:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.dt-sort:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.dt-sort-ico {
  flex: none;
  color: var(--accent);
}
.dt-sort-ico.is-idle {
  color: var(--text-tertiary);
}

.dt-row {
  transition: background var(--dur-fast) var(--ease);
}
.dt-row:hover {
  background: var(--surface-hover);
}
.dt-row.is-clickable {
  cursor: pointer;
}
.dt-row.is-selected,
.dt-row.is-selected:hover {
  background: var(--accent-soft);
}
.dt-row:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: -2px;
}

.dt-td {
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-primary);
  vertical-align: middle;
}
.dt-table tbody tr:last-child .dt-td {
  border-bottom: none;
}
.dt-td.is-check {
  width: 34px;
  padding-right: 0;
}
.dt-td.is-center {
  text-align: center;
}
.dt-td.is-right {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.dt-empty-cell {
  padding: 0;
}

/* checkboxes */
.dt-check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border: 1.5px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  color: transparent;
  flex: none;
  vertical-align: middle;
}
.dt-check:not(:disabled):hover {
  border-color: var(--accent);
}
.dt-check.is-on,
.dt-check.is-mixed {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--on-accent);
}
.dt-check:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.dt-check:disabled {
  opacity: 0.5;
  cursor: default;
}

/* skeleton */
.dt-skel {
  display: inline-block;
  height: 11px;
  max-width: 100%;
  border-radius: var(--r-sm);
  background: linear-gradient(
    90deg,
    var(--surface-muted) 25%,
    var(--border-subtle) 50%,
    var(--surface-muted) 75%
  );
  background-size: 200% 100%;
  animation: dt-shimmer 1.2s linear infinite;
}
.dt-skel.is-box {
  width: 15px;
  height: 15px;
}
@keyframes dt-shimmer {
  from {
    background-position: 200% 0;
  }
  to {
    background-position: -200% 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .dt-progress-bar,
  .dt-skel {
    animation: none;
  }
}
</style>
