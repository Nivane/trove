<!--
  NavSidebar — 四组 15 项 IA 的渲染层（设计稿 P6 §2.2 / §3）。
  展开与折叠两态共用一份模板（消灭旧壳两份拷贝）；分组标签不可聚焦（K1），
  项是真链接（K2）。角标取不到 / 为 0 时不渲染。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useAuthStore } from '../../stores/auth'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import BrandMark from '../brand/BrandMark.vue'
import NavItem from './NavItem.vue'
import UserMenu from './UserMenu.vue'
import { navGroupsFor, type BadgeKey } from './navModel'
import { formatBadge, type BadgeCount } from '../../composables/useNavBadges'

const props = defineProps<{
  collapsed: boolean
  badges: Partial<Record<BadgeKey, BadgeCount>>
}>()

const auth = useAuthStore()
const ui = useUiStore()

const groups = computed(() => navGroupsFor(auth.user?.role))
</script>

<template>
  <aside class="sidebar admin-sidebar" :class="{ rail: props.collapsed }">
    <div class="brand" :class="{ 'brand-rail': props.collapsed }">
      <span class="brand-mark"
        ><BrandMark :size="props.collapsed ? 20 : 24"
      /></span>
    </div>

    <nav class="admin-nav" :aria-label="t('adminNavLabel', ui.lang)">
      <div
        v-for="entry in groups"
        :key="entry.group.key"
        class="admin-nav-section"
      >
        <!-- 组标签是标签不是控件：不可聚焦、不进 Tab 序（K1）。 -->
        <div v-if="!props.collapsed" class="admin-nav-label">
          {{ t(entry.group.labelKey, ui.lang) }}
        </div>
        <div v-else class="nav-rail-sep" aria-hidden="true"></div>
        <NavItem
          v-for="item in entry.items"
          :key="item.key"
          :item="item"
          :collapsed="props.collapsed"
          :badge="
            item.badgeKey ? formatBadge(props.badges[item.badgeKey]) : null
          "
        />
      </div>
    </nav>

    <div class="sidebar-profile">
      <UserMenu :rail="props.collapsed" />
    </div>
  </aside>
</template>

<style scoped>
.brand {
  padding-bottom: var(--sp-2);
}
.brand.brand-rail {
  justify-content: center;
  padding: var(--sp-1) 0 var(--sp-2);
}
/* 折叠态的分组分隔线：代替不可见的组标签，保持四组的分段感。 */
.nav-rail-sep {
  height: 1px;
  width: 24px;
  margin: var(--sp-2) auto;
  background: var(--border-subtle);
}
</style>
