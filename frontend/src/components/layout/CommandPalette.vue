<!--
  CommandPalette — ⌘K / Ctrl+K 命令面板（设计稿 P6 §3 / K5）。
  W0 最小版：只跳路由 —— 读 navModel（按角色过滤），中英文页名均可匹配；
  ↑↓ 选择、Enter 打开、Esc 关闭并把焦点还给触发元素；无匹配给可读空态。
  实体搜索（数据源 / 用户 / 任务）留待各页 URL 状态就位后接入（W3+）。
-->
<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  useId,
  watch,
} from 'vue'
import { useRouter } from 'vue-router'
import { Search } from 'lucide-vue-next'
import { useAuthStore } from '../../stores/auth'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { filterNavItems, NAV_GROUPS, type NavItem } from './navModel'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const ui = useUiStore()
const auth = useAuthStore()
const router = useRouter()

const uid = useId()
const listId = `${uid}-list`
const optionId = (index: number) => `${uid}-opt-${index}`

const query = ref('')
const activeIndex = ref(0)
const inputEl = ref<HTMLInputElement | null>(null)
let restoreEl: HTMLElement | null = null

const results = computed(() => filterNavItems(query.value, auth.user?.role))

const groupLabelKey = (item: NavItem) =>
  NAV_GROUPS.find((g) => g.key === item.group)?.labelKey ?? 'admin'

watch(results, () => {
  activeIndex.value = 0
})

watch(
  () => props.open,
  async (open) => {
    if (open) {
      restoreEl = (document.activeElement as HTMLElement | null) ?? null
      query.value = ''
      activeIndex.value = 0
      await nextTick()
      inputEl.value?.focus()
    } else {
      // Esc / 选择后的焦点归还：触发按钮（K5）。
      restoreEl?.focus?.()
      restoreEl = null
    }
  },
)

function close(restoreFocus = true) {
  if (!restoreFocus) restoreEl = null
  emit('update:open', false)
}

function pick(item: NavItem | undefined) {
  if (!item) return
  close(false)
  void router.push(item.path)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    close(true)
    return
  }
  if (e.key === 'Tab') {
    // 面板里唯一可交互的是输入框：把 Tab 关在对话内。
    e.preventDefault()
    return
  }
  const total = results.value.length
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (total) activeIndex.value = (activeIndex.value + 1) % total
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (total) activeIndex.value = (activeIndex.value - 1 + total) % total
    return
  }
  if (e.key === 'Enter') {
    e.preventDefault()
    pick(results.value[activeIndex.value])
  }
}

/** 全局 ⌘K / Ctrl+K：打开 / 收起（在输入框里按也生效）。 */
function onGlobalKey(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    emit('update:open', !props.open)
  }
}

onMounted(() => document.addEventListener('keydown', onGlobalKey))
onBeforeUnmount(() => document.removeEventListener('keydown', onGlobalKey))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cp-overlay" @mousedown.self="close(true)">
      <div
        class="cp-panel"
        role="dialog"
        aria-modal="true"
        :aria-label="t('paletteTitle', ui.lang)"
      >
        <div class="cp-search">
          <Search :size="15" aria-hidden="true" />
          <input
            ref="inputEl"
            v-model="query"
            class="cp-input"
            type="text"
            role="combobox"
            aria-expanded="true"
            :aria-controls="listId"
            :aria-activedescendant="
              results.length ? optionId(activeIndex) : undefined
            "
            :placeholder="t('palettePlaceholder', ui.lang)"
            :aria-label="t('paletteTitle', ui.lang)"
            autocomplete="off"
            spellcheck="false"
            @keydown="onKeydown"
          />
          <kbd class="cp-kbd" aria-hidden="true">esc</kbd>
        </div>

        <div v-if="results.length" :id="listId" class="cp-list" role="listbox">
          <div
            v-for="(item, index) in results"
            :id="optionId(index)"
            :key="item.key"
            class="cp-item"
            :class="{ 'is-active': index === activeIndex }"
            role="option"
            :aria-selected="index === activeIndex"
            @mousemove="activeIndex = index"
            @click="pick(item)"
          >
            <component
              :is="item.icon"
              :size="15"
              class="cp-icon"
              aria-hidden="true"
            />
            <span class="cp-label">{{ t(item.labelKey, ui.lang) }}</span>
            <span class="cp-group">{{ t(groupLabelKey(item), ui.lang) }}</span>
          </div>
        </div>
        <div v-else class="cp-empty" role="status">
          {{ t('paletteEmpty', ui.lang) }}
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cp-overlay {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 12vh var(--sp-4) var(--sp-4);
  background: var(--overlay);
}
.cp-panel {
  width: 560px;
  max-width: 100%;
  max-height: 60vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: var(--r-lg);
  background: var(--surface-raised);
  box-shadow: var(--shadow-xl);
}
.cp-search {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
/* 输入框不留自己的轮廓，但聚焦指示不消失 —— 换成整行下边框（K7）。 */
.cp-search:focus-within {
  border-bottom-color: var(--accent);
}
.cp-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  font-size: var(--fs-sm);
}
.cp-kbd {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  padding: 0 4px;
  line-height: 16px;
}
.cp-list {
  overflow-y: auto;
  padding: var(--sp-1);
}
.cp-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-md);
  font-size: var(--fs-sm);
  color: var(--text-primary);
  cursor: pointer;
}
.cp-item.is-active {
  background: var(--surface-muted);
}
.cp-icon {
  flex-shrink: 0;
  color: var(--text-secondary);
}
.cp-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cp-group {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  flex-shrink: 0;
}
.cp-empty {
  padding: var(--sp-5);
  text-align: center;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
</style>
