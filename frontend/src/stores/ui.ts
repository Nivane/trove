import { defineStore } from 'pinia'
import type { Lang } from '../i18n'
import type { DatasourceInfo } from '../api/types'
import { apiGet } from '../api/http'

// Legacy localStorage keys kept so the vanilla-UI migration is seamless.
const LANG_KEY = 'trove_ui_lang'
const SIDEBAR_KEY = 'trove_ui_sidebar'
const ANALYSIS_KEY = 'trove_ui_analysis'
const DATASOURCE_KEY = 'trove_ui_datasource'
const SESSION_DS_KEY = (sid: string) => `trove_ui_ds_${sid}`

export const useUiStore = defineStore('ui', {
  state: () => ({
    lang: (localStorage.getItem(LANG_KEY) as Lang) || 'zh',
    sidebarOpen: localStorage.getItem(SIDEBAR_KEY) !== '0',
    analysisOpen: localStorage.getItem(ANALYSIS_KEY) !== '0',
    datasource: localStorage.getItem(DATASOURCE_KEY) || '',
    datasourceList: [] as DatasourceInfo[],
    datasourcesLoaded: false,
  }),
  getters: {
    hasDatasource: (state) =>
      state.datasourceList.some((d) => d.name === state.datasource),
    /** 当前生效数据源:显式选择优先,否则回退到默认/首个可用数据源。 */
    activeDatasource(state): string {
      if (
        state.datasource &&
        state.datasourceList.some((d) => d.name === state.datasource)
      ) {
        return state.datasource
      }
      const def = state.datasourceList.find((d) => d.default)
      return def?.name || state.datasourceList[0]?.name || ''
    },
  },
  actions: {
    applyTheme() {
      // light-only theme: nothing to toggle
      document.documentElement.dataset.theme = 'light'
      document.documentElement.classList.remove('dark')
    },
    setLang(lang: Lang) {
      this.lang = lang
      localStorage.setItem(LANG_KEY, lang)
    },
    toggleSidebar() {
      this.sidebarOpen = !this.sidebarOpen
      localStorage.setItem(SIDEBAR_KEY, this.sidebarOpen ? '1' : '0')
    },
    toggleAnalysis() {
      this.analysisOpen = !this.analysisOpen
      localStorage.setItem(ANALYSIS_KEY, this.analysisOpen ? '1' : '0')
    },
    setDatasource(name: string) {
      this.datasource = name
      localStorage.setItem(DATASOURCE_KEY, name)
    },
    /** 按会话记住本次选择:后续切回该会话时恢复。 */
    rememberSessionDatasource(sid: string) {
      if (!sid) return
      localStorage.setItem(SESSION_DS_KEY(sid), this.datasource || '')
    },
    /** 会话级记忆:存在且仍可用时恢复为该会话上次的选择。 */
    restoreSessionDatasource(sid: string) {
      if (!sid) return
      const stored = localStorage.getItem(SESSION_DS_KEY(sid))
      if (stored && this.datasourceList.some((d) => d.name === stored)) {
        this.datasource = stored
        localStorage.setItem(DATASOURCE_KEY, stored)
      }
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
