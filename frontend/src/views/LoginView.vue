<!--
  登录页（P6 §2.4 B 案）：左品牌区 + 右表单卡。

  产品是「对话 + 管理」双面，登录页先说自己是谁，再让人进门 —— 品牌区
  给出主张、三条卖点与版本行；表单卡只做一件事：把失败说清楚。

  八态（§2.4 状态矩阵）逐态落地：
    空闲      用户名自动聚焦，无错误块
    提交中    按钮 loading + aria-busy，两输入 readonly，连点只发一次
    凭据错误  401 → role="alert"「用户名或密码错误 · 请检查后重试」+ 焦点回密码框并选中
    被节流    429 → 读 Retry-After 秒数 → 文案含秒数 + 按钮倒计时禁用
    服务不可用 5xx → StatePanel error 态 + 重试
    网络失败  fetch reject → StatePanel error 态 + 重试
    会话过期  ?reason=expired → 顶部一次性信息条（展示后从 URL 抹掉）
    已登录    路由守卫照旧回对话页（router/index.ts，未改）

  文案遵循「发生了什么 + 怎么办」：错误按原因分流，绝不拿「密码错」一概而论。
-->
<template>
  <div class="login-view">
    <div class="login-shell">
      <!-- 左：品牌区 -->
      <section class="login-brand">
        <div class="lb-head">
          <span class="lb-mark"><BrandMark :size="26" /></span>
          <span class="lb-word">{{ t('brand', ui.lang) }}</span>
        </div>
        <h1 class="lb-claim">{{ t('loginClaim', ui.lang) }}</h1>
        <p class="lb-sub">{{ t('loginClaimSub', ui.lang) }}</p>
        <ul class="lb-points">
          <li v-for="key in POINTS" :key="key">
            <Check :size="15" aria-hidden="true" />
            <span>{{ t(key, ui.lang) }}</span>
          </li>
        </ul>
        <p class="lb-version" :title="versionTitle">{{ versionLine }}</p>
      </section>

      <!-- 右：表单卡 -->
      <section class="login-card">
        <h2 class="login-title">{{ t('loginFormTitle', ui.lang) }}</h2>
        <p class="login-subtitle">{{ t('loginFormSub', ui.lang) }}</p>

        <p v-if="expiredNotice" class="login-notice" role="status">
          <Info :size="15" aria-hidden="true" />
          <span>{{ t('loginExpired', ui.lang) }}</span>
        </p>

        <el-form @submit.prevent="submit">
          <div class="login-field">
            <span class="login-field-icon"><UserCircle :size="17" /></span>
            <el-input
              ref="usernameEl"
              v-model="username"
              name="username"
              autocomplete="username"
              :placeholder="t('loginUser', ui.lang)"
              autofocus
              :readonly="loading"
              @keyup.enter="submit"
            />
          </div>
          <div class="login-field">
            <span class="login-field-icon"><Lock :size="17" /></span>
            <el-input
              ref="passwordEl"
              v-model="password"
              name="password"
              type="password"
              autocomplete="current-password"
              :placeholder="t('loginPass', ui.lang)"
              show-password
              :readonly="loading"
              @keyup.enter="submit"
            />
          </div>

          <transition name="fade">
            <div v-if="inlineError" class="login-error" role="alert">
              <AlertCircle :size="15" aria-hidden="true" />
              <span>{{ inlineError }}</span>
            </div>
          </transition>

          <StatePanel
            v-if="panelError"
            class="login-state"
            mode="error"
            :title="panelError"
            :detail="errorDetail"
            :retry-text="t('loginRetry', ui.lang)"
            @retry="submit"
          />

          <el-button
            type="primary"
            class="login-btn"
            :loading="loading"
            :aria-busy="loading ? 'true' : undefined"
            :disabled="retryIn > 0"
            @click="submit"
          >
            {{ retryIn > 0 ? t('loginRetryIn', ui.lang, retryIn) : t('loginBtn', ui.lang) }}
          </el-button>
        </el-form>

        <div class="login-footer">
          <span class="login-hint">{{ t('loginLegal', ui.lang) }}</span>
          <button class="login-lang" type="button" @click="toggleLang">
            <Languages :size="13" />
            {{ ui.lang === 'zh' ? 'English' : '中文' }}
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlertCircle,
  Check,
  Info,
  Languages,
  Lock,
  UserCircle,
} from 'lucide-vue-next'
import { ElInput } from 'element-plus'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import BrandMark from '../components/brand/BrandMark.vue'
import StatePanel from '../components/base/StatePanel.vue'
import { t } from '../i18n'
import { version } from '../../package.json'

// 版本与构建信息（P6 §4.5）：vite.config.ts 的 define 注入。测试与工具链里
// 没有 define（vitest.config.ts 不带 define），那两处退回 package.json 的
// version、构建行整个不显示 —— 宁可少一行，也不显示半截假信息。
declare const __APP_VERSION__: string
declare const __BUILD_ID__: string

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()
const route = useRoute()

const POINTS = ['loginPoint1', 'loginPoint2', 'loginPoint3'] as const

const username = ref('')
const password = ref('')
const loading = ref(false)

/** 四类失败各自成态：凭据 / 节流 / 服务端 / 网络。绝不合并成一句「密码错」。 */
type ErrorKind = '' | 'credentials' | 'rateLimited' | 'server' | 'network'
const errorKind = ref<ErrorKind>('')
/** StatePanel 的 detail：原始技术信息（HTTP 状态 + 后端 detail），排障用。 */
const errorDetail = ref('')
/** 429 倒计时剩余秒数；> 0 期间提交按钮禁用。 */
const retryIn = ref(0)
let countdown: ReturnType<typeof setInterval> | undefined

const usernameEl = ref<InstanceType<typeof ElInput> | null>(null)
const passwordEl = ref<InstanceType<typeof ElInput> | null>(null)

// ── 版本行 ────────────────────────────────────────────────────────────
const appVersion =
  typeof __APP_VERSION__ === 'string' && __APP_VERSION__ ? __APP_VERSION__ : version
const buildId = typeof __BUILD_ID__ === 'string' ? __BUILD_ID__ : ''

const versionLine = buildId
  ? `v${appVersion} · build ${buildId.slice(0, 10)}`
  : `v${appVersion}`
const versionTitle = computed(
  () =>
    `${t('loginVersionTitle', ui.lang)}: v${appVersion}${buildId ? ` (${buildId})` : ''}`,
)

// ── 一次性信息条：被踢出过来说明来路 ──────────────────────────────────
const expiredNotice = ref(route.query.reason === 'expired')

onMounted(() => {
  usernameEl.value?.focus()
  if (!expiredNotice.value) return
  // 「一次性」= 展示完即从 URL 抹掉：刷新 / 前进后退都不会再看一遍。
  const query: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (key !== 'reason' && typeof value === 'string') query[key] = value
  }
  void router.replace({ query })
})

// ── 错误文案 ──────────────────────────────────────────────────────────
const inlineError = computed(() => {
  if (errorKind.value === 'credentials') return t('loginErrCredentials', ui.lang)
  if (errorKind.value === 'rateLimited') {
    return retryIn.value > 0
      ? t('loginErrRateLimited', ui.lang, retryIn.value)
      : t('loginErrRateLimitedGeneric', ui.lang)
  }
  return ''
})

const panelError = computed(() => {
  if (errorKind.value === 'server') return t('loginErrServer', ui.lang)
  if (errorKind.value === 'network') return t('loginErrNetwork', ui.lang)
  return ''
})

function clearError() {
  errorKind.value = ''
  errorDetail.value = ''
}

function stopCountdown() {
  if (countdown !== undefined) {
    clearInterval(countdown)
    countdown = undefined
  }
}

function startCountdown(seconds?: number) {
  stopCountdown()
  if (!seconds || seconds <= 0) {
    retryIn.value = 0
    return
  }
  retryIn.value = Math.ceil(seconds)
  countdown = setInterval(() => {
    retryIn.value -= 1
    if (retryIn.value <= 0) {
      retryIn.value = 0
      stopCountdown()
      // 等待期结束 → 收掉这条错误：留下的「请 0 秒后重试」只会让人困惑。
      if (errorKind.value === 'rateLimited') clearError()
    }
  }, 1000)
}

onBeforeUnmount(stopCountdown)

/** 改输入即撤掉凭据错误（429 的等待期除外：那条提示随倒计时自己走完）。 */
watch([username, password], () => {
  if (errorKind.value === 'credentials') clearError()
})

/** 把异常翻译成四态之一。状态码用鸭子类型读 —— 测试会以模块 mock 替换
 *  http.ts，instanceof 在那种替身下不成立，状态才是稳定契约。 */
function classify(err: unknown) {
  const status =
    typeof (err as { status?: unknown } | null)?.status === 'number'
      ? (err as { status: number }).status
      : 0
  const message = err instanceof Error ? err.message : String(err ?? '')

  if (status === 401) {
    errorKind.value = 'credentials'
    // 焦点回密码框并选中内容：直接重输，不用先摸鼠标。
    passwordEl.value?.focus()
    passwordEl.value?.select()
    return
  }
  if (status === 429) {
    errorKind.value = 'rateLimited'
    startCountdown((err as { retryAfter?: number } | null)?.retryAfter)
    return
  }
  if (status > 0) {
    // 后端在，但这个请求没成（5xx / 其它 4xx）：原始信息进 detail，别吞。
    errorKind.value = 'server'
    errorDetail.value = `HTTP ${status}${message ? ` · ${message}` : ''}`
    return
  }
  // fetch 直接 reject：根本没连上服务器（断网 / DNS / 被拦）。
  errorKind.value = 'network'
  errorDetail.value = message
}

function toggleLang() {
  ui.setLang(ui.lang === 'zh' ? 'en' : 'zh')
}

async function submit() {
  // 防重复提交：连点 / 连按 Enter 只发一次 —— loading 在首个 await 前同步置位。
  if (loading.value || retryIn.value > 0) return
  if (!username.value || !password.value) return
  loading.value = true
  clearError()
  try {
    await auth.login(username.value, password.value)
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    await router.push(next)
  } catch (err) {
    classify(err)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* 版式：左品牌 + 右表单。窄屏堆叠，品牌区收敛为页头。
   base.css 的 .login-view 是 overflow:hidden 的居中容器（单卡时代）——
   两栏 + 窄屏堆叠都可能高于视口，这里换成可滚动 + safe 居中（拦不住
   内容时不裁顶；老浏览器忽略 safe，退回原来的 center）。 */
.login-view {
  padding: var(--sp-6);
  overflow: auto;
  align-items: safe center;
}

.login-shell {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 400px);
  align-items: center;
  gap: var(--sp-12);
  width: 100%;
  max-width: 1000px;
}

.login-brand {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.lb-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.lb-mark {
  display: inline-grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: var(--r-md);
  background: var(--accent-soft);
  color: var(--accent);
  animation: brand-in 0.25s var(--ease);
}

.lb-word {
  font-size: var(--fs-xl);
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text-primary);
}

.lb-claim {
  margin: var(--sp-5) 0 0;
  max-width: 22ch;
  font-size: var(--fs-2xl);
  line-height: var(--lh-tight);
  letter-spacing: -0.02em;
  color: var(--text-primary);
}

.lb-sub {
  margin: var(--sp-3) 0 0;
  max-width: 44ch;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--text-secondary);
}

.lb-points {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin: var(--sp-5) 0 0;
  padding: 0;
  list-style: none;
}

.lb-points li {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}

.lb-points svg {
  flex: none;
  margin-top: 2px;
  color: var(--ok);
}

.lb-version {
  margin: var(--sp-6) 0 0;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

/* 表单卡：卡片外形沿用 base.css 的 .login-card，这里只调标题与内嵌状态。 */
.login-card {
  width: 100%;
}

.login-title {
  margin: 0 0 var(--sp-1);
  font-size: var(--fs-lg);
  letter-spacing: -0.01em;
}

.login-subtitle {
  margin: 0 0 var(--sp-5);
}

.login-notice {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-4);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  background: var(--accent-soft);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}

.login-notice svg {
  flex: none;
  color: var(--accent);
}

/* 块级错误（5xx / 网络）：StatePanel 是仓库里唯一的状态承载件 ——
   只把它的留白收进卡片尺度，文案与 role / aria-live 全部由它自己管。 */
.login-state {
  min-height: 0;
  margin-bottom: var(--sp-4);
  padding: var(--sp-4) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
}

.login-btn {
  margin-top: var(--sp-1);
}

@media (max-width: 900px) {
  .login-shell {
    grid-template-columns: minmax(0, 1fr);
    max-width: 440px;
    gap: var(--sp-6);
  }
  .lb-claim {
    max-width: none;
    margin-top: var(--sp-4);
    font-size: var(--fs-xl);
  }
  .lb-version {
    margin-top: var(--sp-3);
  }
}

@media (max-width: 640px) {
  .login-view {
    padding: var(--sp-4);
  }
  .lb-sub,
  .lb-points {
    display: none;
  }
}
</style>
