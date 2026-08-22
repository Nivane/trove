import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import AuditView from '../src/views/admin/AuditView.vue'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiPut: vi.fn(),
}))

import { apiGet } from '../src/api/http'
import { useAuthStore } from '../src/stores/auth'
import { useUiStore } from '../src/stores/ui'
import type { VueWrapper } from '@vue/test-utils'

function makeEntries(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    ts: `2026-08-${String((i % 28) + 1).padStart(2, '0')}T0${(i % 9) + 1}:00:00Z`,
    username: `user${i}`,
    action: 'admin.user.create',
    method: 'POST',
    path: '/v1/admin/users',
    status: 201,
  }))
}

const calls: string[] = []
let currentTotal = 45

let wrapper: VueWrapper | null = null

async function mountView() {
  wrapper = mount(AuditView, {
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  currentTotal = 45
  setActivePinia(createPinia())
  useAuthStore().user = { id: 1, username: 'admin', role: 'admin' }
  useUiStore().lang = 'en'
  vi.clearAllMocks()
  calls.length = 0
  document.body.innerHTML = ''
  ;(apiGet as any).mockImplementation(async (path: string) => {
    calls.push(path)
    const url = new URL(path, 'http://localhost')
    const limit = Number(url.searchParams.get('limit') ?? 20)
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const all = makeEntries(currentTotal)
    return { audit: all.slice(offset, offset + limit), total: all.length }
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('AuditView pagination', () => {
  it('loads page 2 with the right offset when a pager number is clicked', async () => {
    const view = await mountView()
    expect(calls[0]).toContain('offset=0')

    // click the "2" pager button
    const page2 = view
      .findAll('.el-pager li.number')
      .find((li) => li.text() === '2')
    expect(page2).toBeTruthy()
    await page2!.trigger('click')
    await flushPromises()

    const last = calls[calls.length - 1]
    expect(last).toContain('offset=20')
    const rows = view.findAll('.el-table__body tbody tr')
    expect(rows.length).toBe(20)
  })

  it('keeps the selected page across a refresh', async () => {
    const view = await mountView()
    const page2 = view
      .findAll('.el-pager li.number')
      .find((li) => li.text() === '2')
    await page2!.trigger('click')
    await flushPromises()
    expect(calls[calls.length - 1]).toContain('offset=20')

    // clicking refresh re-reads the same page, not a reset to page 1
    await view.find('.refresh-btn').trigger('click')
    await flushPromises()
    expect(calls[calls.length - 1]).toContain('offset=20')
  })

  it('walks pages forward one by one', async () => {
    const view = await mountView()
    for (const expected of ['offset=20', 'offset=40']) {
      await view.find('.el-pagination .btn-next').trigger('click')
      await flushPromises()
      expect(calls[calls.length - 1]).toContain(expected)
    }
  })

  it('clamps back to page 1 when the result set shrinks after a refresh', async () => {
    const view = await mountView()
    const page3 = view
      .findAll('.el-pager li.number')
      .find((li) => li.text() === '3')
    await page3!.trigger('click')
    await flushPromises()
    expect(calls[calls.length - 1]).toContain('offset=40')

    // the dataset shrinks to a single page → the pager must not stay on page 3
    currentTotal = 12
    await view.find('.refresh-btn').trigger('click')
    await flushPromises()
    expect(calls[calls.length - 1]).toContain('offset=0')
  })
})
