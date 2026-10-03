import { defineStore } from 'pinia'
import type { Lang } from '../i18n'
import type { DatasourceInfo } from '../api/types'
import { apiGet } from '../api/http'
import { fetchTopics, type TopicInfo } from '../api/topics'

// Legacy localStorage keys kept so the vanilla-UI migration is seamless.
const LANG_KEY = 'trove_ui_lang'
const SIDEBAR_KEY = 'trove_ui_sidebar'
const ANALYSIS_KEY = 'trove_ui_analysis'
const DATASOURCE_KEY = 'trove_ui_datasource'
const SESSION_DS_KEY = (sid: string) => `trove_ui_ds_${sid}`
const TOPIC_KEY = 'trove_ui_topic'
const SESSION_TOPIC_KEY = (sid: string) => `trove_ui_topic_${sid}`

export const useUiStore = defineStore('ui', {
  state: () => ({
    lang: (localStorage.getItem(LANG_KEY) as Lang) || 'zh',
    sidebarOpen: localStorage.getItem(SIDEBAR_KEY) !== '0',
    analysisOpen: localStorage.getItem(ANALYSIS_KEY) !== '0',
    datasource: localStorage.getItem(DATASOURCE_KEY) || '',
    datasourceList: [] as DatasourceInfo[],
    datasourcesLoaded: false,
    /** 当前选中的主题域('' = 不限定)。 */
    topic: localStorage.getItem(TOPIC_KEY) || '',
    /** 已拉到的域清单(属于 topicsFor 那个数据源)。 */
    topicList: [] as TopicInfo[],
    /** topicList 对应的数据源('' = 未加载/无源);与当前源不符时清单视为空。 */
    topicsFor: '',
    topicsLoaded: false,
    /** 请求序号:慢响应回来时若已有更新的请求发出,直接丢弃。 */
    topicsSeq: 0,
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
    /** 当前源的主题域清单;清单与源不符(切源过渡态/未加载)时视为空。 */
    activeTopics(state): TopicInfo[] {
      return state.topicsFor && state.topicsFor === this.activeDatasource
        ? state.topicList
        : []
    },
    /** 当前生效的主题域:必须存在于当前清单且 status=ok,否则 ''(不限定)。
     *  置灰挡的是手滑;这道门挡的是历史值——换源后的残留名、后来过期的域,
     *  一律不随请求带出去。 */
    activeTopic(state): string {
      if (!state.topic) return ''
      const found = this.activeTopics.find((t) => t.name === state.topic)
      return found && found.status === 'ok' ? state.topic : ''
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
      // 页面语言跟着走（D21）：读屏器与拼写检查读的是 <html lang>，
      // 只切 UI 文案不同步它会按旧语言发音。局部切换，不整页 reload。
      if (typeof document !== 'undefined') {
        document.documentElement.lang = lang
      }
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
      // 换源即重置主题域:旧域的 scope 是旧源的数据集,在新源上不成立。
      // (会话恢复路径 setDatasource → restoreSessionTopic 的顺序保证
      // 该会话自己的选择随后被放回。)
      this.setTopic('')
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
    setTopic(name: string) {
      this.topic = name
      localStorage.setItem(TOPIC_KEY, name)
    },
    /** 拉某个源的主题域清单。404(该源无语义模型)与网络错误同待遇:
     *  没有域可选 —— 选择器据此隐藏,「这个源没有域」不是故障。 */
    async loadTopics(ds: string) {
      if (!ds) {
        this.topicList = []
        this.topicsFor = ''
        this.topicsLoaded = true
        this.pruneTopic()
        return
      }
      if (this.topicsFor === ds && this.topicsLoaded) {
        // 同一源的清单已就绪(会话切换回到同源):不重复拉,只校验选择。
        this.pruneTopic()
        return
      }
      const seq = ++this.topicsSeq
      let list: TopicInfo[]
      try {
        const body = await fetchTopics(ds)
        list = body.topics ?? []
      } catch {
        list = []
      }
      if (seq !== this.topicsSeq) return // 已有更新的请求在飞,丢弃这份
      this.topicList = list
      this.topicsFor = ds
      this.topicsLoaded = true
      this.pruneTopic()
    },
    /** 选中的域不在当前清单里(或已失效)就清掉 —— 状态自愈,不靠调用方。 */
    pruneTopic() {
      if (!this.topic) return
      const ok = this.activeTopics.some(
        (t) => t.name === this.topic && t.status === 'ok',
      )
      if (!ok) this.setTopic('')
    },
    /** 与数据源同口径:按会话记住本次选择。 */
    rememberSessionTopic(sid: string) {
      if (!sid) return
      localStorage.setItem(SESSION_TOPIC_KEY(sid), this.topic || '')
    },
    /** 会话级记忆:有存值就恢复(能否生效由 activeTopic 的门决定,
     *  清单到位后 pruneTopic 再校正),没有则显式清空 —— 不能让上一个
     *  会话的选择漏进这个会话。 */
    restoreSessionTopic(sid: string) {
      if (!sid) return
      const stored = localStorage.getItem(SESSION_TOPIC_KEY(sid)) || ''
      this.setTopic(stored)
      // 清单未就绪时不能 prune:activeTopics 为空会把有效值误清
      // (冷启动恢复会话正是这条路径),等 loadTopics 完成后它再校正一次。
      if (this.topicsFor && this.topicsFor === this.activeDatasource) {
        this.pruneTopic()
      }
    },
  },
})
