<!--
  NavItem — 侧栏单项（设计稿 P6 §3 / K2 / K3 / K6）。
  · 真 <RouterLink>（渲染成 <a href>）：中键 / ⌘+点击能新开标签、可复制链接；
  · 高亮用前缀匹配：子路由保父项高亮，/admin 精确匹配（防全亮）；
  · 当前项 aria-current="page"；折叠态图标项有 aria-label 与 title 悬浮标签。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { BADGE_TITLE_KEYS, isPathActive, type NavItem } from './navModel'

const props = defineProps<{
  item: NavItem
  /** 折叠（rail）态：只留图标 + aria-label + 悬浮标签。 */
  collapsed?: boolean
  /** 已格式化的角标文案（null = 不显示）。 */
  badge?: string | null
}>()

const ui = useUiStore()
const route = useRoute()

const label = computed(() => t(props.item.labelKey, ui.lang))
const active = computed(() => isPathActive(route.path, props.item.path))
const badgeTitle = computed(() =>
  props.item.badgeKey ? t(BADGE_TITLE_KEYS[props.item.badgeKey], ui.lang) : '',
)
/** 折叠态没有可见文本，角标并入可访问名（K6）。 */
const ariaLabel = computed(() =>
  props.collapsed
    ? props.badge
      ? `${label.value} (${props.badge})`
      : label.value
    : undefined,
)
</script>

<template>
  <RouterLink
    class="admin-nav-item"
    :class="{ active, rail: collapsed }"
    :to="item.path"
    :aria-current="active ? 'page' : undefined"
    :aria-label="ariaLabel"
    :title="collapsed ? label : undefined"
  >
    <component
      :is="item.icon"
      :size="collapsed ? 18 : 17"
      class="admin-nav-icon"
      aria-hidden="true"
    />
    <span v-if="!collapsed" class="admin-nav-text">{{ label }}</span>
    <span
      v-if="badge && !collapsed"
      class="admin-nav-badge"
      :title="badgeTitle"
      >{{ badge }}</span
    >
    <span
      v-else-if="badge && collapsed"
      class="admin-nav-badge-dot"
      :title="badgeTitle"
      aria-hidden="true"
    />
  </RouterLink>
</template>

<style scoped>
/* <a> 复位：其余外观沿用 admin.css 的 .admin-nav-item。 */
.admin-nav-item {
  text-decoration: none;
}
.admin-nav-item.active {
  font-weight: 600;
}
.admin-nav-item.rail {
  position: relative;
  width: 40px;
  min-height: 40px;
  padding: 0;
  justify-content: center;
}
.admin-nav-badge-dot {
  position: absolute;
  top: 6px;
  right: 8px;
  width: 6px;
  height: 6px;
  border-radius: var(--r-full);
  background: var(--accent);
}
</style>
