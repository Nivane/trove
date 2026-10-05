<template>
  <!-- 一键下一步(拒绝出口 / 错误出口共用):后端给全 label 与 href —
       这里只渲染 + 按登录角色过 admin_only 闸,绝不自己分类或编链接。 -->
  <div v-if="visible.length" class="next-actions">
    <span class="next-actions-label">{{ t('nextActions', ui.lang) }}</span>
    <RouterLink
      v-for="a in visible"
      :key="a.id ?? a.href"
      class="next-action-btn"
      :to="a.href!"
    >
      <ArrowRight :size="13" />
      {{ a.label }}
    </RouterLink>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { ArrowRight } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { useAuthStore } from '../../stores/auth'
import { t } from '../../i18n'
import type { NextActionInfo } from '../../api/types'

const props = defineProps<{ actions?: NextActionInfo[] | null }>()

const ui = useUiStore()
const auth = useAuthStore()

const visible = computed(() =>
  (props.actions ?? []).filter(
    (a) => !!a.href && !!a.label && (!a.admin_only || auth.isAdmin),
  ),
)
</script>
