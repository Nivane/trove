import { defineStore } from 'pinia'
import type { Lang } from '../i18n'
import type { DatasourceInfo } from '../api/types'
import { apiGet } from '../api/http'
import { clampSidebarWidth } from '../utils/sidebar'

// Legacy localStorage keys kept so the vanilla-UI migration is seamless.
const THEME_KEY = 'trove_ui_theme'
const LANG_KEY = 'trove_ui_lang'
const SIDEBAR_KEY = 'trove_ui_sidebar'
const SIDEBAR_WIDTH_KEY = 'trove_ui_sidebar_width'
const ANALYSIS_KEY = 'trove_ui_analysis'
const DATASOURCE_KEY = 'trove_ui_datasource'

export type Theme = 'light' | 'dark'

export const useUiStore = defineStore('ui', {
  state: () => ({
    theme: (localStorage.getItem(THEME_KEY) as Theme) || 'light',
    lang: (localStorage.getItem(LANG_KEY) as Lang) || 'zh',
    sidebarOpen: localStorage.getItem(SIDEBAR_KEY) !== '0',
    sidebarWidth: clampSidebarWidth(
      Number(localStorage.getItem(SIDEBAR_WIDTH_KEY)) || 260,
    ),
    analysisOpen: localStorage.getItem(ANALYSIS_KEY) !== '0',
    datasource: localStorage.getItem(DATASOURCE_KEY) || '',
    datasourceList: [] as DatasourceInfo[],
    datasourcesLoaded: false,
  }),
  getters: {
    hasDatasource: (state) =>
      state.datasourceList.some((d) => d.name === state.datasource),
  },
  actions: {
    applyTheme() {
      document.documentElement.dataset.theme = this.theme
      document.documentElement.classList.toggle('dark', this.theme === 'dark')
    },
    cycleTheme() {
      this.theme = this.theme === 'light' ? 'dark' : 'light'
      localStorage.setItem(THEME_KEY, this.theme)
      this.applyTheme()
    },
    setLang(lang: Lang) {
      this.lang = lang
      localStorage.setItem(LANG_KEY, lang)
    },
    toggleSidebar() {
      this.sidebarOpen = !this.sidebarOpen
      localStorage.setItem(SIDEBAR_KEY, this.sidebarOpen ? '1' : '0')
    },
    setSidebarWidth(w: number) {
      this.sidebarWidth = clampSidebarWidth(w)
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(this.sidebarWidth))
    },
    toggleAnalysis() {
      this.analysisOpen = !this.analysisOpen
      localStorage.setItem(ANALYSIS_KEY, this.analysisOpen ? '1' : '0')
    },
    setDatasource(name: string) {
      this.datasource = name
      localStorage.setItem(DATASOURCE_KEY, name)
    },
    async loadDatasources() {
      try {
        const body = await apiGet('/v1/catalog/datasources')
        this.datasourceList = body.datasources ?? []
        if (
          this.datasource &&
          !this.datasourceList.some((d) => d.name === this.datasource)
        ) {
          this.datasource = ''
        }
      } catch {
        this.datasourceList = []
      } finally {
        this.datasourcesLoaded = true
      }
    },
  },
})
