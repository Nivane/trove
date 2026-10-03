/**
 * useNetworkStatus — navigator.onLine + online/offline 事件（P6 §2.5 离线态）。
 *
 * 模块级单例：多个组件共享一份监听与一份状态。恢复在线时广播一次
 * retry 事件 —— 页面订阅后可重放当前请求（W3 接各页；W0 只把出口留好）。
 * 状态只报事实（浏览器给的 onLine），不推断后端可用性。
 */
import { ref, type Ref } from 'vue'

export interface UseNetworkStatus {
  /** navigator.onLine 的响应式镜像。 */
  online: Ref<boolean>
  /** 刚刚恢复在线的一段窗口（用于「网络已恢复」的短暂提示）。 */
  justRecovered: Ref<boolean>
  /** 订阅「恢复在线」广播；返回退订函数。 */
  onRetry: (cb: () => void) => () => void
}

const RECOVERED_MS = 4000

const online = ref(typeof navigator === 'undefined' ? true : navigator.onLine)
const justRecovered = ref(false)
const listeners = new Set<() => void>()
let bound = false
let recoveredTimer: ReturnType<typeof setTimeout> | null = null

function handleOffline() {
  online.value = false
  justRecovered.value = false
  if (recoveredTimer) {
    clearTimeout(recoveredTimer)
    recoveredTimer = null
  }
}

function handleOnline() {
  online.value = true
  justRecovered.value = true
  for (const cb of listeners) {
    try {
      cb()
    } catch {
      // 订阅者自己的错误不拖垮广播
    }
  }
  if (recoveredTimer) clearTimeout(recoveredTimer)
  recoveredTimer = setTimeout(() => {
    justRecovered.value = false
    recoveredTimer = null
  }, RECOVERED_MS)
}

function bind() {
  if (bound || typeof window === 'undefined') return
  bound = true
  window.addEventListener('offline', handleOffline)
  window.addEventListener('online', handleOnline)
}

export function useNetworkStatus(): UseNetworkStatus {
  bind()
  return {
    online,
    justRecovered,
    onRetry(cb: () => void) {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
  }
}

/** 测试用：清空监听与计时器，把状态还原为初始值（生产代码不调用）。 */
export function resetNetworkStatusForTests() {
  listeners.clear()
  if (recoveredTimer) {
    clearTimeout(recoveredTimer)
    recoveredTimer = null
  }
  online.value = typeof navigator === 'undefined' ? true : navigator.onLine
  justRecovered.value = false
}
