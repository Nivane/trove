<template>
  <!-- 一键下一步(拒绝出口 / 错误出口共用):后端给全 label 与 href —
       这里只渲染 + 按登录角色过 admin_only 闸,绝不自己分类或编链接。
       payload(蓝本预填等)只做机械转运:逐键以 bp_ 前缀并入 query,
       含义由目标页(SemanticView)解释——这里不认识任何业务键。 -->
  <div v-if="visible.length" class="next-actions">
    <span class="next-actions-label">{{ t('nextActions', ui.lang) }}</span>
    <RouterLink
      v-for="a in visible"
      :key="a.id ?? a.href"
      class="next-action-btn"
      :to="toFor(a)"
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

function toFor(a: NextActionInfo): string {
  const href = a.href ?? ''
  const payload = a.payload
  if (!payload || !Object.keys(payload).length) return href
  const [path, search] = href.split('?')
  const params = new URLSearchParams(search ?? '')
  for (const [k, v] of Object.entries(payload)) {
    if (v === null || v === undefined || v === '') continue
    params.set(`bp_${k}`, Array.isArray(v) ? v.join(',') : String(v))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}
</script>
