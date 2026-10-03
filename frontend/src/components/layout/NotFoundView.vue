<!--
  NotFoundView — 404 兜底（设计稿 P6 §4.2 / D12）。
  两种落点共用一份：壳内（/admin/* 未知路径，渲染在 ConsoleShell 的主区）
  与非 /admin 的未知路径（独立页）。给出路，不给死胡同。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'

const ui = useUiStore()
const route = useRoute()

const inConsole = computed(
  () => route.path === '/admin' || route.path.startsWith('/admin/'),
)
</script>

<template>
  <div class="notfound" :class="{ 'in-console': inConsole }">
    <div class="nf-code" aria-hidden="true">404</div>
    <h1 class="nf-title">{{ t('notFoundTitle', ui.lang) }}</h1>
    <p class="nf-desc">{{ t('notFoundDesc', ui.lang) }}</p>
    <RouterLink class="nf-action" :to="inConsole ? '/admin' : '/'">
      {{
        inConsole
          ? t('notFoundBackConsole', ui.lang)
          : t('notFoundBackChat', ui.lang)
      }}
    </RouterLink>
  </div>
</template>

<style scoped>
.notfound {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  min-height: 60vh;
  padding: var(--sp-8) var(--sp-4);
  text-align: center;
}
/* 壳内落点沿用页面内边距（与 .admin-view 同宽），不再自带外层容器。 */
.notfound.in-console {
  max-width: 1240px;
  margin: 0 auto;
  min-height: 50vh;
}
.nf-code {
  font-size: var(--fs-2xl);
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.nf-title {
  margin: 0;
  font-size: var(--fs-xl);
  letter-spacing: -0.02em;
}
.nf-desc {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.nf-action {
  margin-top: var(--sp-3);
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 var(--sp-4);
  border-radius: var(--r-md);
  background: var(--accent);
  color: var(--on-accent);
  font-size: var(--fs-sm);
  font-weight: 500;
  text-decoration: none;
}
.nf-action:hover {
  background: var(--accent-hover);
}
</style>
