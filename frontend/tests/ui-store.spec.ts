import { describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../src/api/http', () => ({ apiGet: vi.fn() }))
import { apiGet } from '../src/api/http'
import { useUiStore } from '../src/stores/ui'

describe('ui store datasource list', () => {
  it('loadDatasources stores the list', async () => {
    setActivePinia(createPinia())
    ;(apiGet as any).mockResolvedValue({
      datasources: [{ name: 'financial', kb_initialized: true }],
    })
    const ui = useUiStore()
    await ui.loadDatasources()
    expect(ui.datasourceList.map((d: any) => d.name)).toEqual(['financial'])
  })
  it('hasDatasource reflects a selected existing source', async () => {
    setActivePinia(createPinia())
    ;(apiGet as any).mockResolvedValue({ datasources: [{ name: 'financial' }] })
    const ui = useUiStore()
    await ui.loadDatasources()
    expect(ui.hasDatasource).toBe(false)
    ui.setDatasource('financial')
    expect(ui.hasDatasource).toBe(true)
  })
  it('loadDatasources clears a stale selection not in the list', async () => {
    setActivePinia(createPinia())
    ;(apiGet as any).mockResolvedValue({ datasources: [{ name: 'financial' }] })
    const ui = useUiStore()
    ui.datasource = 'old' // 上次会话记住的源已不在列表（被删/未 init）
    await ui.loadDatasources()
    expect(ui.datasource).toBe('')
    expect(ui.hasDatasource).toBe(false)
  })
  it('loadDatasources failure leaves an empty list', async () => {
    setActivePinia(createPinia())
    ;(apiGet as any).mockRejectedValue(new Error('network down'))
    const ui = useUiStore()
    await ui.loadDatasources()
    expect(ui.datasourceList).toEqual([])
    expect(ui.hasDatasource).toBe(false)
  })
  it('datasourcesLoaded flips to true after load', async () => {
    setActivePinia(createPinia())
    const ui = useUiStore()
    expect(ui.datasourcesLoaded).toBe(false)
    ;(apiGet as any).mockResolvedValue({ datasources: [{ name: 'financial' }] })
    await ui.loadDatasources()
    expect(ui.datasourcesLoaded).toBe(true)
  })
})
