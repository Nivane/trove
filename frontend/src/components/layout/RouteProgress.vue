<!--
  RouteProgress — 顶栏下沿 2px 路由进度条（设计稿 P6 §2.5）。
  自研、零依赖；装饰性元素（aria-hidden），不抢焦点、不吃点击。
  prefers-reduced-motion 下不做宽度动画：直接满格显示/隐藏（K8）。
-->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()

const active = ref(false)
const pct = ref(0)
let trickle: ReturnType<typeof setInterval> | null = null
let hideTimer: ReturnType<typeof setTimeout> | null = null

function reducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function')
    return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function clearTimers() {
  if (trickle) {
    clearInterval(trickle)
    trickle = null
  }
  if (hideTimer) {
    clearTimeout(hideTimer)
    hideTimer = null
  }
}

function start() {
  clearTimers()
  active.value = true
  if (reducedMotion()) {
    // 不做动效：静默满格，路由落地即消失。
    pct.value = 100
    return
  }
  pct.value = 8
  trickle = setInterval(() => {
    // 逼近但不越过 90%：完成由 finish() 收口，不做假进度。
    pct.value = Math.min(90, pct.value + Math.max(1, (90 - pct.value) * 0.12))
  }, 160)
}

function finish() {
  clearTimers()
  if (!active.value) return
  pct.value = 100
  const settle = reducedMotion() ? 0 : 220
  hideTimer = setTimeout(() => {
    active.value = false
    pct.value = 0
    hideTimer = null
  }, settle)
}

let unregister: (() => void)[] = []

onMounted(() => {
  unregister = [
    router.beforeEach(() => {
      start()
      return true
    }),
    router.afterEach(() => finish()),
    router.onError(() => finish()),
  ]
})

onBeforeUnmount(() => {
  unregister.forEach((off) => off())
  clearTimers()
})
</script>

<template>
  <div
    class="route-progress"
    :class="{ 'is-active': active }"
    aria-hidden="true"
  >
    <div class="route-progress-bar" :style="{ width: `${pct}%` }" />
  </div>
</template>

<style scoped>
.route-progress {
  position: absolute;
  top: var(--console-topbar-h, 48px);
  left: 0;
  right: 0;
  height: 2px;
  pointer-events: none;
  z-index: 40;
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}
.route-progress.is-active {
  opacity: 1;
}
.route-progress-bar {
  height: 100%;
  width: 0;
  background: var(--accent);
  transition: width 160ms var(--ease);
}
@media (prefers-reduced-motion: reduce) {
  .route-progress,
  .route-progress-bar {
    transition: none;
  }
}
</style>
