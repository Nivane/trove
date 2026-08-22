import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'

import App from './App.vue'
import { router } from './router'
import { useUiStore } from './stores/ui'
import './assets/styles/tokens.css'
import './assets/styles/base.css'
import './assets/styles/element.css'
import './assets/styles/admin.css'
import 'highlight.js/styles/atom-one-light.css'

// Stable marker pinned by tests/api/test_ui.py on the served bundle.
;(window as any).__TROVE_UI__ = true

const app = createApp(App)
app.use(createPinia())
app.use(router)

const ui = useUiStore()
app.use(ElementPlus, { locale: ui.lang === 'zh' ? zhCn : en })

ui.applyTheme()

app.mount('#app')
