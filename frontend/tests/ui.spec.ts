import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useUiStore } from '../src/stores/ui'
import { clampSidebarWidth } from '../src/utils/sidebar'

describe('ui store — sidebar width / analysis panel / datasource', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('clamps sidebar width into 220–400', () => {
    expect(clampSidebarWidth(999)).toBe(400)
    expect(clampSidebarWidth(50)).toBe(220)
    expect(clampSidebarWidth(300.4)).toBe(300)
  })

  it('setSidebarWidth persists the clamped value', () => {
    const ui = useUiStore()
    ui.setSidebarWidth(999)
    expect(ui.sidebarWidth).toBe(400)
    expect(localStorage.getItem('trove_ui_sidebar_width')).toBe('400')
  })

  it('sidebarWidth defaults to 260 and analysis panel defaults open', () => {
    const ui = useUiStore()
    expect(ui.sidebarWidth).toBe(260)
    expect(ui.analysisOpen).toBe(true)
  })

  it('toggleAnalysis persists closed state', () => {
    const ui = useUiStore()
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
