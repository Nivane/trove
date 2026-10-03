<!--
  OpsView — 质量与成本运营台(/admin/ops,P4)。

  两个 Tab 一个页面:质量(评测产物 + 判定 + 失败清单 + KB 反馈)与
  成本(用量样本 + 缓存 + 预算/降级)。页面**只读**:不触发评测
  (eval_bird 是花钱的,永不从这里点火),不写任何 KB。

  三个 URL 键全部住在 query 里(useListQuery):tab / win / 失败清单的
  {q, verdict, path, page, sort, order}(过滤键在 QualityPanel 内声明)。
  win 与 /admin 同键名(§4.3 白名单):总览页的成本下钻链接
  /admin/ops?tab=usage&win=<w> 必须落在这个窗口上,不是默认窗口。
  /admin/usage 是兼容别名,重定向到这里并带上 tab=usage(路由表里声明)。

  诚实规则在本页的体现:
    · 数据没到 → StatePanel loading,不是空白页;
    · 该腿挂了 → 面板拿到 null,页内 DegradedNotice 明说原因;
    · 整页 503(内部存储探不通)→ payload 里的 degraded 才是主角,
      页面级 DegradedNotice(page-level)呈现,其余块照常渲染;
    · not_measured 是端点声明的「本轮不测」清单 —— 直接照抄,
      不给它编数字。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  fetchQualityOverview,
  fetchUsageOverview,
  OPS_FAILURES_LIMIT,
  OPS_WINDOWS,
  type QualityOverview,
  type UsageOverview,
} from '../../api/ops'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { useListQuery } from '../../composables/useListQuery'
import { fmtDateTime } from '../../utils/format'
import PageHeader from '../../components/base/PageHeader.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import DegradedNotice from '../../components/ops/DegradedNotice.vue'
import QualityPanel from '../../components/ops/QualityPanel.vue'
import CostPanel from '../../components/ops/CostPanel.vue'
import PerfPanel from '../../components/ops/PerfPanel.vue'

const ui = useUiStore()

const { values } = useListQuery({ tab: 'quality', win: '7d' })

/** URL 里的 tab 认不出时回落到质量(自愈,不报错)。 */
const tab = computed<'quality' | 'usage'>(() => (values.tab === 'usage' ? 'usage' : 'quality'))

// §2.3 统一页头:根面包屑=管理台(→ /admin),末项=本页。
const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('opsTitle', ui.lang) },
])

const quality = ref<QualityOverview | null>(null)
const usage = ref<UsageOverview | null>(null)
const loadingQuality = ref(false)
const loadingUsage = ref(false)
const pageError = ref('')

const activeLoading = computed(() =>
  tab.value === 'quality' ? loadingQuality.value : loadingUsage.value,
)

const degraded = computed(() =>
  tab.value === 'quality' ? quality.value?.degraded ?? [] : usage.value?.degraded ?? [],
)
const notMeasured = computed(() =>
  tab.value === 'quality' ? quality.value?.not_measured ?? [] : usage.value?.not_measured ?? [],
)
const asOf = computed(() => {
  const at = tab.value === 'quality' ? quality.value?.generated_at : usage.value?.generated_at
  return at ? `${t('opsAsOf', ui.lang)} ${fmtDateTime(at)}` : ''
})

/** 整页级失败(存储探不通)的判据:usage 503 payload —— available=false
    且带存储腿的 degraded;质量侧不可达就是普通 fetch 错误。 */
const pageLevelDegraded = computed(
  () =>
    tab.value === 'usage' &&
    usage.value !== null &&
    !usage.value.available &&
    usage.value.degraded.length > 0,
)

function describeError(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

async function loadQuality() {
  loadingQuality.value = true
  pageError.value = ''
  try {
    quality.value = await fetchQualityOverview(OPS_FAILURES_LIMIT)
  } catch (e) {
    pageError.value = describeError(e)
  } finally {
    loadingQuality.value = false
  }
}

async function loadUsage() {
  loadingUsage.value = true
  pageError.value = ''
  try {
    // 存储探不通时 503 仍带完整 payload —— resolve 后从这里渲染。
    usage.value = await fetchUsageOverview(values.win)
  } catch (e) {
    pageError.value = describeError(e)
  } finally {
    loadingUsage.value = false
  }
}

function reload() {
  if (tab.value === 'quality') void loadQuality()
  else void loadUsage()
}

function setTab(next: 'quality' | 'usage') {
  values.tab = next
}

onMounted(() => {
  if (tab.value === 'usage') void loadUsage()
  else void loadQuality()
})

watch(tab, (next) => {
  pageError.value = ''
  if (next === 'usage') {
    if (!usage.value) void loadUsage()
  } else if (!quality.value) {
    void loadQuality()
  }
})

watch(
  () => values.win,
  () => {
    if (tab.value !== 'usage' || loadingUsage.value) return
    void loadUsage()
  },
)
</script>

<template>
  <div class="admin-view ops-page" :aria-busy="activeLoading || undefined">
    <PageHeader :title="t('opsTitle', ui.lang)" :breadcrumbs="crumbs">
      <template #description>
        <span>{{ t('opsDesc', ui.lang) }}</span>
        <span v-if="asOf" class="ops-asof"> · {{ asOf }}</span>
      </template>
      <template #actions>
        <div class="tab-seg" role="tablist" :aria-label="t('opsTitle', ui.lang)">
          <button
            type="button"
            role="tab"
            class="seg-btn"
            :class="{ 'is-active': tab === 'quality' }"
            :aria-selected="tab === 'quality'"
            @click="setTab('quality')"
          >
            {{ t('opsTabQuality', ui.lang) }}
          </button>
          <button
            type="button"
            role="tab"
            class="seg-btn"
            :class="{ 'is-active': tab === 'usage' }"
            :aria-selected="tab === 'usage'"
            @click="setTab('usage')"
          >
            {{ t('opsTabUsage', ui.lang) }}
          </button>
        </div>
        <div
          v-if="tab === 'usage'"
          class="win-seg"
          role="group"
          :aria-label="t('opsUsageWindow', ui.lang)"
        >
          <button
            v-for="w in OPS_WINDOWS"
            :key="w"
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.win === w }"
            :aria-pressed="values.win === w"
            @click="values.win = w"
          >
            {{ w }}
          </button>
        </div>
        <el-button :loading="activeLoading" @click="reload">
          {{ t('refresh', ui.lang) }}
        </el-button>
      </template>
    </PageHeader>

    <StatePanel
      v-if="pageError"
      mode="error"
      :title="t('opsErrorTitle', ui.lang)"
      :detail="pageError"
      :retry-text="t('opsRetry', ui.lang)"
      @retry="reload"
    />

    <StatePanel
      v-else-if="activeLoading && !(tab === 'quality' ? quality : usage)"
      mode="loading"
      :title="t('opsLoading', ui.lang)"
    />

    <template v-else>
      <DegradedNotice :items="degraded" :page-level="pageLevelDegraded" />

      <QualityPanel
        v-if="tab === 'quality' && quality"
        :data="quality"
        :loading="loadingQuality"
        error=""
        @retry="loadQuality"
      />

      <template v-if="tab === 'usage' && usage">
        <CostPanel :cost="usage.cost" />
        <PerfPanel :latency="usage.latency" :cache="usage.cache" :budget="usage.budget" />
      </template>

      <div v-if="notMeasured.length" class="nm-strip" :title="t('opsNotMeasuredDesc', ui.lang)">
        <span class="nm-label">{{ t('opsNotMeasured', ui.lang) }}:</span>
        <code v-for="key in notMeasured" :key="key" class="nm-chip">{{ key }}</code>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ops-asof {
  color: var(--text-tertiary);
}
.tab-seg,
.win-seg {
  display: inline-flex;
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  overflow: hidden;
}
.tab-seg .seg-btn,
.win-seg .seg-btn {
  padding: 4px 12px;
  border: none;
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.tab-seg .seg-btn + .seg-btn,
.win-seg .seg-btn + .seg-btn {
  border-left: 1px solid var(--border-subtle);
}
.tab-seg .seg-btn.is-active,
.win-seg .seg-btn.is-active {
  background: var(--accent);
  color: var(--on-accent);
  font-weight: 600;
}
.nm-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.nm-label {
  font-weight: 600;
}
.nm-chip {
  padding: 1px 6px;
  border-radius: var(--r-sm);
  background: var(--surface-sunken);
  border: 1px solid var(--border-subtle);
}
</style>
