import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { h } from 'vue'
import DataTable from '../src/components/base/DataTable.vue'
import type { DataTableColumn } from '../src/components/base/DataTable.vue'

const columns: DataTableColumn[] = [
  { key: 'username', label: 'Username', sortable: true },
  { key: 'role', label: 'Role', sortable: true, defaultDir: 'desc' },
  { key: 'created', label: 'Created', width: 120 },
]

const rows = [
  { id: 1, username: 'admin', role: 'admin', created: '2026-03-02' },
  { id: 2, username: 'lin.wang', role: 'analyst', created: '2026-04-18' },
  { id: 3, username: 'zhangsan', role: 'user', created: '2026-05-30' },
]

function mountTable(props: Record<string, unknown> = {}, slots: Record<string, unknown> = {}) {
  return mount(DataTable, {
    props: { columns, rows, rowKey: 'id', ...props },
    slots,
  })
}

describe('DataTable', () => {
  it('renders a row per record with the configured columns', () => {
    const w = mountTable()
    expect(w.findAll('.dt-th')).toHaveLength(columns.length)
    expect(w.findAll('.dt-row')).toHaveLength(rows.length)
    expect(w.findAll('.dt-row')[0].text()).toContain('admin')
  })

  it('renders a cell slot per column key with row/value/index', () => {
    const w = mountTable({}, {
      'cell-role': (props: Record<string, unknown>) =>
        h('span', { class: 'role-chip' }, `role:${props.value}`),
    })
    expect(w.findAll('.role-chip')).toHaveLength(3)
    expect(w.findAll('.role-chip')[0].text()).toBe('role:admin')
  })

  it('renders bare cell values without stray whitespace', () => {
    const w = mountTable()
    const cell = w.findAll('.dt-row')[0].findAll('.dt-td')[0].element
    expect(cell.textContent).toBe('admin')
  })

  it('renders null/undefined cells as an em dash', () => {
    const w = mountTable({ rows: [{ id: 9, username: null }] })
    expect(w.find('.dt-row').text()).toContain('—')
  })

  /* ── data typography (P7-W4 §6.1): identifier mono + numeric tabular ── */

  it('marks identifier columns mono on header and body cells', () => {
    const w = mountTable({
      columns: [
        { key: 'id', label: 'ID', mono: true },
        columns[0],
      ],
    })
    expect(w.findAll('.dt-th')[0].classes()).toContain('is-mono')
    const cells = w.findAll('.dt-row').map((r) => r.findAll('.dt-td')[0])
    expect(cells).toHaveLength(3)
    for (const c of cells) expect(c.classes()).toContain('is-mono')
    // the plain column stays untouched
    expect(w.findAll('.dt-th')[1].classes()).not.toContain('is-mono')
  })

  it('marks numeric columns right-aligned tabular', () => {
    const w = mountTable({
      columns: [{ key: 'visits', label: 'Visits', numeric: true }, columns[0]],
    })
    expect(w.findAll('.dt-th')[0].classes()).toContain('is-num')
    const cells = w.findAll('.dt-row').map((r) => r.findAll('.dt-td')[0])
    for (const c of cells) expect(c.classes()).toContain('is-num')
    expect(w.findAll('.dt-td')[1].classes()).not.toContain('is-num')
  })

  /* ── sorting ── */

  it('emits update:sort ascending when a sortable header is clicked', async () => {
    const w = mountTable()
    await w.findAll('.dt-sort')[0].trigger('click')
    expect(w.emitted('update:sort')![0]).toEqual([{ key: 'username', dir: 'asc' }])
  })

  it('toggles direction when the active column is clicked again', async () => {
    const w = mountTable({ sort: { key: 'username', dir: 'asc' } })
    await w.findAll('.dt-sort')[0].trigger('click')
    expect(w.emitted('update:sort')![0]).toEqual([{ key: 'username', dir: 'desc' }])
  })

  it('honours a column defaultDir when switching columns', async () => {
    const w = mountTable({ sort: { key: 'username', dir: 'asc' } })
    await w.findAll('.dt-sort')[1].trigger('click')
    expect(w.emitted('update:sort')![0]).toEqual([{ key: 'role', dir: 'desc' }])
  })

  it('exposes aria-sort on sortable headers only', () => {
    const w = mountTable({ sort: { key: 'username', dir: 'desc' } })
    const headers = w.findAll('.dt-th')
    expect(headers[0].attributes('aria-sort')).toBe('descending')
    expect(headers[1].attributes('aria-sort')).toBe('none')
    expect(headers[2].attributes('aria-sort')).toBeUndefined()
  })

  /* ── selection ── */

  it('emits the full key set from the header checkbox', async () => {
    const w = mountTable({ selectable: true, selected: [] })
    await w.findAll('.dt-check')[0].trigger('click')
    expect(w.emitted('update:selected')![0]).toEqual([[1, 2, 3]])
  })

  it('clears visible keys while keeping selection made on other pages', async () => {
    const w = mountTable({ selectable: true, selected: [1, 2, 3, 42] })
    await w.findAll('.dt-check')[0].trigger('click')
    expect(w.emitted('update:selected')![0]).toEqual([[42]])
  })

  it('reports a mixed header state for a partial selection', () => {
    const w = mountTable({ selectable: true, selected: [2] })
    const header = w.findAll('.dt-check')[0]
    expect(header.attributes('aria-checked')).toBe('mixed')
    expect(header.classes()).toContain('is-mixed')
  })

  it('toggles a single row without emitting a row click', async () => {
    const w = mountTable({ selectable: true, selected: [] })
    await w.findAll('.dt-check')[1].trigger('click')
    expect(w.emitted('update:selected')![0]).toEqual([[1]])
    expect(w.emitted('row-click')).toBeUndefined()
  })

  /* ── row click ── */

  it('emits row-click with the row and its index', async () => {
    const w = mountTable()
    await w.findAll('.dt-row')[1].trigger('click')
    expect(w.emitted('row-click')![0]).toEqual([rows[1], 1])
  })

  it('makes rows keyboard-activatable when rowClickable is set', async () => {
    const w = mountTable({ rowClickable: true })
    const row = w.findAll('.dt-row')[0]
    expect(row.attributes('tabindex')).toBe('0')
    await row.trigger('keydown.enter')
    expect(w.emitted('row-click')![0]).toEqual([rows[0], 0])
  })

  /* ── states ── */

  it('shows skeleton rows while the first page loads', () => {
    const w = mountTable({ rows: [], loading: true, skeletonRows: 4 })
    expect(w.findAll('.dt-skel-row')).toHaveLength(4)
    expect(w.find('.dt-row').exists()).toBe(false)
    expect(w.attributes('aria-busy')).toBe('true')
  })

  it('keeps existing rows and shows the progress bar on refresh', () => {
    const w = mountTable({ loading: true })
    expect(w.findAll('.dt-row')).toHaveLength(3)
    expect(w.find('.dt-progress').exists()).toBe(true)
    expect(w.find('.dt-skel-row').exists()).toBe(false)
  })

  it('renders the empty slot content when there are no rows', () => {
    const w = mountTable({ rows: [] }, { empty: '<div class="my-empty">nothing here</div>' })
    expect(w.find('.my-empty').text()).toBe('nothing here')
    expect(w.find('.dt-empty-cell').attributes('colspan')).toBe('3')
    expect(w.find('.dt-row').exists()).toBe(false)
  })

  it('falls back to a built-in empty panel', () => {
    const w = mountTable({ rows: [], emptyText: 'No users yet' })
    expect(w.find('.state-panel').exists()).toBe(true)
    expect(w.text()).toContain('No users yet')
  })
})
