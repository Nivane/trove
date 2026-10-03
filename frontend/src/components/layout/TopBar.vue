<!--
  TopBar — 48px 固定条（设计稿 P6「壳结构对照」）。
  内容：侧栏折叠按钮 · 面包屑槽（窄屏才显示当前页名，宽屏唯一来源是
  PageHeader，避免双面包屑，§2.3）· ⌘K 入口 · 平台健康点 · 版本。
  健康点没有数据就不渲染 —— 顶栏不发明数据。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { PanelLeftClose, PanelLeftOpen, Search } from 'lucide-vue-next'
import { version } from '../../../package.json'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import type { MessageKey } from './navModel'
import type { ConsoleHealth } from '../../composables/useNavBadges'

const props = defineProps<{
  /** overview.health.status；null = 没取到，不显示。 */
  health: ConsoleHealth | null
}>()

defineEmits<{ (e: 'open-palette'): void }>()

const ui = useUiStore()
const route = useRoute()

const appVersion = `v${version}`

/** 窄屏顶栏显示的当前页名，来自 route.meta.titleKey（W2 页头统一后仍是它）。 */
const pageTitle = computed(() => {
  const key = route.meta.titleKey as MessageKey | undefined
  return key ? t(key, ui.lang) : ''
})

const HEALTH_KEYS: Record<ConsoleHealth, MessageKey> = {
  ok: 'healthOk',
  degraded: 'healthDegraded',
  unavailable: 'healthUnavailable',
}
const healthLabel = computed(() =>
  props.health ? t(HEALTH_KEYS[props.health], ui.lang) : '',
)
</script>

<template>
  <header class="console-topbar">
    <button
      class="topbar-btn"
      :aria-label="t('toggleSidebar', ui.lang)"
      @click="ui.toggleSidebar()"
    >
      <PanelLeftClose v-if="ui.sidebarOpen" :size="16" aria-hidden="true" />
      <PanelLeftOpen v-else :size="16" aria-hidden="true" />
    </button>

    <span v-if="pageTitle" class="topbar-crumb" :title="pageTitle">{{
      pageTitle
    }}</span>

    <span class="topbar-spacer" />

    <button
      class="topbar-search"
      type="button"
      :aria-label="t('paletteButton', ui.lang)"
      @click="$emit('open-palette')"
    >
      <Search :size="14" aria-hidden="true" />
      <span class="topbar-search-text">{{ t('paletteButton', ui.lang) }}</span>
      <kbd class="topbar-kbd" aria-hidden="true">⌘K</kbd>
    </button>

    <span
      v-if="health"
      class="topbar-health"
      :class="`is-${health}`"
      :title="healthLabel"
    >
      <span class="health-dot" aria-hidden="true" />
      <span class="health-text">{{ healthLabel }}</span>
    </span>

    <span
      class="topbar-version"
      :title="`${t('appVersionLabel', ui.lang)} ${appVersion}`"
    >
      {{ appVersion }}
    </span>
  </header>
</template>

<style scoped>
.console-topbar {
  height: var(--console-topbar-h, 48px);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 0 var(--sp-3);
  background: var(--surface-raised);
  border-bottom: 1px solid var(--border-subtle);
}
.topbar-btn {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: var(--r-md);
  color: var(--text-secondary);
  flex-shrink: 0;
}
.topbar-btn:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
/* 宽屏不重复面包屑（PageHeader 才是唯一来源）；窄屏给当前页名。 */
.topbar-crumb {
  display: none;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
  font-weight: 600;
}
@media (max-width: 960px) {
  .topbar-crumb {
    display: block;
  }
}
.topbar-spacer {
  flex: 1;
}
.topbar-search {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  height: 28px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  flex-shrink: 0;
}
.topbar-search:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.topbar-kbd {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  padding: 0 4px;
  line-height: 16px;
}
@media (max-width: 720px) {
  .topbar-search-text {
    display: none;
  }
}
.topbar-health {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  flex-shrink: 0;
}
.health-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--r-full);
  background: var(--ok);
}
.topbar-health.is-degraded .health-dot {
  background: var(--warn);
}
.topbar-health.is-unavailable .health-dot {
  background: var(--danger);
}
@media (max-width: 720px) {
  .health-text {
    display: none;
  }
}
.topbar-version {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
</style>
