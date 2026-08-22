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
})
