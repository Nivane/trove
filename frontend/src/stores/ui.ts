import { defineStore } from 'pinia'
import type { Lang } from '../i18n'

// Legacy localStorage keys kept so the vanilla-UI migration is seamless.
const THEME_KEY = 'trove_ui_theme'
const LANG_KEY = 'trove_ui_lang'
const SIDEBAR_KEY = 'trove_ui_sidebar'

export type Theme = 'light' | 'dark'

export const useUiStore = defineStore('ui', {
  state: () => ({
    theme: (localStorage.getItem(THEME_KEY) as Theme) || 'light',
    lang: (localStorage.getItem(LANG_KEY) as Lang) || 'zh',
    sidebarOpen: localStorage.getItem(SIDEBAR_KEY) !== '0',
  }),
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
  },
})
