<!--
  ConsoleShell — 控制台外壳：侧栏（四组 15 项）+ 顶栏 48px + 主区（设计稿 P6 §2.1 AFTER）。

  · 11 个既有管理页零改动：主区仍是 .admin-main（同样的滚动容器、同样的
    背景与 Element 变量），页面自带的 .admin-view / .view-header 原样生效。
  · 顶栏下沿依次是：路由进度条 / 全局条带（离线·恢复）。
  · 壳级数据只有一处读取（useNavBadges → 一次 overview）：侧栏角标 +
    顶栏健康点；取不到就都不显示。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import NavSidebar from './NavSidebar.vue'
import TopBar from './TopBar.vue'
import GlobalBanner from './GlobalBanner.vue'
import RouteProgress from './RouteProgress.vue'
import CommandPalette from './CommandPalette.vue'
import { useNavBadges } from '../../composables/useNavBadges'
import { useNetworkStatus } from '../../composables/useNetworkStatus'

const ui = useUiStore()
const { badges, health, load } = useNavBadges()
const net = useNetworkStatus()

const paletteOpen = ref(false)
const bannerDismissed = ref(false)

// 离线条：网络状态一变就重新给一次机会（关掉的是「这一条」，不是这类状态）。
watch(
  () => net.online.value,
  () => {
    bannerDismissed.value = false
  },
)

const bannerKind = computed<'offline' | 'online' | null>(() => {
  if (bannerDismissed.value) return null
  if (!net.online.value) return 'offline'
  if (net.justRecovered.value) return 'online'
  return null
})

// <html lang> 跟语言走（D21）：首屏（localStorage 里是 en）也要同步一次。
watch(
  () => ui.lang,
  (lang) => {
    document.documentElement.lang = lang
  },
  { immediate: true },
)

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="admin-shell console-shell">
    <!-- K1：Tab 第一站 —— 跳过导航直达主区。 -->
    <a class="skip-link" href="#main-content">{{
      t('skipToContent', ui.lang)
    }}</a>

    <NavSidebar :collapsed="!ui.sidebarOpen" :badges="badges" />

    <div class="console-col">
      <TopBar :health="health" @open-palette="paletteOpen = true" />
      <GlobalBanner
        v-if="bannerKind"
        :kind="bannerKind"
        @dismiss="bannerDismissed = true"
      />
      <main id="main-content" class="admin-main" tabindex="-1">
        <router-view />
      </main>
      <RouteProgress />
    </div>

    <CommandPalette v-model:open="paletteOpen" />
  </div>
</template>

<style scoped>
/* 侧栏尺寸按设计稿「壳结构对照」：248px；折叠 64px（base.css 的 272/52 被覆盖）。 */
.console-shell :deep(.admin-sidebar) {
  width: 248px;
}
.console-shell :deep(.admin-sidebar.rail) {
  width: 64px !important;
}

.console-col {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  --console-topbar-h: 48px;
}

/* 主区在列布局里要能收缩，滚动仍留在 .admin-main 内（页面零改动）。 */
.console-shell .admin-main {
  min-height: 0;
}

.skip-link {
  position: fixed;
  top: -100px;
  left: var(--sp-3);
  z-index: 1300;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-default);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  color: var(--text-primary);
  font-size: var(--fs-sm);
  text-decoration: none;
  box-shadow: var(--shadow-md);
  transition: top var(--dur-fast) var(--ease);
}
.skip-link:focus {
  top: var(--sp-3);
}
@media (prefers-reduced-motion: reduce) {
  .skip-link {
    transition: none;
  }
}
</style>
