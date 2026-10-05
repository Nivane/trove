import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useUiStore } from '../src/stores/ui'

describe('ui store — sidebar collapse / analysis panel / datasource', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('sidebarOpen defaults to expanded and persists closed state', () => {
    const ui = useUiStore()
    expect(ui.sidebarOpen).toBe(true)
    ui.toggleSidebar()
    expect(ui.sidebarOpen).toBe(false)
    expect(localStorage.getItem('trove_ui_sidebar')).toBe('0')
  })

  it('toggleAnalysis 默认收起（设计「主推」），点击展开并持久化', () => {
    const ui = useUiStore()
    expect(ui.analysisOpen).toBe(false)
    ui.toggleAnalysis()
    expect(ui.analysisOpen).toBe(true)
    expect(localStorage.getItem('trove_ui_analysis')).toBe('1')
    ui.toggleAnalysis()
    expect(ui.analysisOpen).toBe(false)
    expect(localStorage.getItem('trove_ui_analysis')).toBe('0')
  })

  it('setDatasource persists the selected datasource', () => {
    const ui = useUiStore()
    ui.setDatasource('financial')
    expect(ui.datasource).toBe('financial')
    expect(localStorage.getItem('trove_ui_datasource')).toBe('financial')
  })
})
