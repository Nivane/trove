<!--
  OntologyView — 管理台「本体」页（E6/O2，前端方案 §3）。

  本体 = 语义模型的**只读投影**：对象（数据集）+ 链接（关系）+ 层级，
  全部由 semantics.yml 派生，页面本身不建模、不编辑（唯一的动作是
  深链回语义台）。词汇按 2026-10-11 拍板直呼「本体」。

  三态纪律（§3.6，NF13「读不到 ≠ 空」）：
  · 无模型 / 零数据集 → 引导卡 +「去语义台声明」按钮（合法空态）；
  · 有数据集但零声明 → 图照常渲染 + 弱化引导卡（**不是**空态——
    未声明的对象也是投影的一部分，藏起整张图会成为「本体不存在」的假象）；
  · 读不到 / 响应缺 objects|links 键 → 显式错误卡 + 重试（形状守卫先于
    渲染：绝不把形状异常渲染成一张假空图）。
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import type { DatasourceInfo, ObjectGraphInfo } from '../../api/types'
import { apiGet } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { useListQuery } from '../../composables/useListQuery'
import PageHeader from '../../components/base/PageHeader.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import OntologyGraph from '../../components/ontology/OntologyGraph.vue'
import ObjectPanel from '../../components/ontology/ObjectPanel.vue'
import {
  graphScaleHintNeeded,
  isObjectGraph,
  type OntologySelection,
} from '../../utils/ontologyChart'

const ui = useUiStore()
const router = useRouter()

// ?ds= 与语义台同一条 URL 纪律：可分享、可刷新、back 可退。
const { values } = useListQuery({ ds: '' })

const datasources = ref<DatasourceInfo[]>([])
const graph = ref<ObjectGraphInfo | null>(null)
const loading = ref(false)
const loadError = ref('')
const selection = ref<OntologySelection | null>(null)
const panelEl = ref<InstanceType<typeof ObjectPanel> | null>(null)

const connected = computed(() => datasources.value.filter((d) => d.status === 'connected'))
const objects = computed(() => graph.value?.objects ?? [])
const links = computed(() => graph.value?.links ?? [])
const hierarchies = computed(() => graph.value?.hierarchies ?? [])
const declaredCount = computed(() => objects.value.filter((o) => o.declared).length)

/** 纯派生 KPI：没有任何第二个计数来源。 */
const coverageText = computed(() => {
  const total = objects.value.length
  if (!total) return '—'
  return `${Math.round((declaredCount.value / total) * 100)}%`
})

const scaleHint = computed(() => graphScaleHintNeeded(objects.value.length))
/** 有对象但零声明：图照常渲染，引导卡弱化出现（不是空态）。 */
const showWeakGuide = computed(() => objects.value.length > 0 && declaredCount.value === 0)
const crumbTitle = computed(() =>
  values.ds ? `${t('ontology', ui.lang)} · ${values.ds}` : t('ontology', ui.lang),
)
const crumbs = computed(() => [
  { label: t('semCrumbManage', ui.lang), to: { path: '/admin' } },
  { label: t('ontology', ui.lang) },
])

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) return String((e as { message: unknown }).message)
  return String(e ?? '')
}

async function loadDatasources() {
  try {
    const body = await apiGet<{ datasources: DatasourceInfo[] }>('/v1/admin/datasources')
    datasources.value = body.datasources ?? []
    if (!values.ds && connected.value.length) {
      const dflt = connected.value.find((d) => d.default)
      values.ds = dflt ? dflt.name : connected.value[0].name
    }
  } catch (e) {
    // 深链带了 ?ds= 时仍按该源取图；列表读不到只在没有可查源时才是错误。
    if (!values.ds) loadError.value = errMsg(e)
  }
}

async function loadGraph() {
  const ds = values.ds
  if (!ds) return
  loading.value = true
  loadError.value = ''
  try {
    const body = await apiGet<unknown>(`/v1/admin/semantic/${encodeURIComponent(ds)}/object-graph`)
    if (values.ds !== ds) return // raced with a datasource switch
    if (!isObjectGraph(body)) {
      // 形状守卫：缺 objects/links = 旧后端/代理形状 → 错误态，不是空图。
      // detail 是诊断信息（无服务端报文可原文照展），不进 i18n 文案面。
      graph.value = null
      loadError.value = 'shape: objects/links missing'
      return
    }
    graph.value = body
  } catch (e) {
    if (values.ds !== ds) return
    graph.value = null
    loadError.value = errMsg(e)
  } finally {
    if (values.ds === ds) loading.value = false
  }
}

function switchDs(name: string) {
  if (values.ds !== name) values.ds = name
}

/** 窄屏：侧栏折为图下方卡片 —— 选中后滚到侧栏；宽屏并排时不动视口。 */
function onSelect(sel: OntologySelection) {
  selection.value = sel
  const narrow =
    typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 1080px)').matches
  if (!narrow) return
  void nextTick(() => {
    panelEl.value?.$el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  })
}

function goSemantic() {
  void router.push({ name: 'admin-semantic', query: values.ds ? { ds: values.ds } : {} })
}

onMounted(async () => {
  if (values.ds) await loadGraph()
  await loadDatasources()
})

watch(
  () => values.ds,
  async () => {
    // 切换数据源 = 换一张图：选中态是上一张图的残值，必须清掉。
    selection.value = null
    await loadGraph()
  },
)
</script>

<template>
  <div class="admin-view onto-page">
    <PageHeader :title="crumbTitle" :description="t('ontologySubtitle', ui.lang)" :breadcrumbs="crumbs">
      <template #actions>
        <div class="onto-ds-switch" role="group" :aria-label="t('semSelectDs', ui.lang)">
          <button
            v-for="d in connected"
            :key="d.name"
            type="button"
            class="onto-ds-seg"
            :class="{ 'is-active': d.name === values.ds }"
            :aria-pressed="d.name === values.ds ? 'true' : 'false'"
            @click="switchDs(d.name)"
          >
            {{ d.default ? `${d.name} · default` : d.name }}
          </button>
        </div>
      </template>
    </PageHeader>

    <!-- 读不到 / 形状异常：显式错误卡 + 重试（出错即清空 graph，绝不渲染假空图） -->
    <StatePanel
      v-if="loadError && !graph"
      mode="error"
      :title="t('ontologyErrorTitle', ui.lang)"
      :description="t('ontologyErrorBody', ui.lang)"
      :detail="loadError"
      :retry-text="t('ontologyErrorRetry', ui.lang)"
      @retry="loadGraph"
    />

    <!-- first paint -->
    <StatePanel v-else-if="loading && !graph" mode="loading" :title="t('ontology', ui.lang)" />

    <!-- 合法空态：零数据集（本体没有可投影的对象） -->
    <StatePanel
      v-else-if="graph && objects.length === 0"
      mode="empty"
      :title="t('ontologyEmptyTitle', ui.lang)"
      :description="t('ontologyEmptyBody', ui.lang)"
    >
      <template #action>
        <el-button type="primary" @click="goSemantic">
          {{ t('ontologyEmptyAction', ui.lang) }}
        </el-button>
      </template>
    </StatePanel>

    <template v-else-if="graph">
      <!-- KPI 行：全部纯派生自投影，不做第二次计数 -->
      <div class="onto-kpi-row">
        <KpiTile :label="t('ontologyKpiObjects', ui.lang)" :value="objects.length" />
        <KpiTile :label="t('ontologyKpiLinks', ui.lang)" :value="links.length" />
        <KpiTile :label="t('ontologyKpiHierarchies', ui.lang)" :value="hierarchies.length" />
        <KpiTile :label="t('ontologyKpiCoverage', ui.lang)" :value="coverageText" />
      </div>

      <!-- 有对象零声明：图照常渲染 + 弱化引导（不是空态） -->
      <div v-if="showWeakGuide" class="onto-guide is-weak">
        <div class="onto-guide-text">
          <h3>{{ t('ontologyEmptyTitle', ui.lang) }}</h3>
          <p>{{ t('ontologyEmptyBody', ui.lang) }}</p>
        </div>
        <el-button @click="goSemantic">{{ t('ontologyEmptyAction', ui.lang) }}</el-button>
      </div>

      <div v-if="scaleHint" class="onto-scale-hint" role="status">
        {{ t('ontologyScaleHint', ui.lang) }}
      </div>

      <div class="onto-main">
        <OntologyGraph
          :objects="objects"
          :links="links"
          :selected="selection"
          @select="onSelect"
        />
        <ObjectPanel
          ref="panelEl"
          class="onto-panel-col"
          :ds="values.ds"
          :selection="selection"
          :objects="objects"
          :links="links"
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.onto-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.onto-ds-switch {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  flex-wrap: wrap;
}

.onto-ds-seg {
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  padding: 2px var(--sp-2);
  cursor: pointer;
}

.onto-ds-seg:hover {
  color: var(--text-primary);
}

.onto-ds-seg.is-active {
  background: var(--surface-raised);
  border-color: var(--border-subtle);
  color: var(--text-primary);
}

.onto-ds-seg:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.onto-kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--sp-2);
}

@media (max-width: 900px) {
  .onto-kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.onto-guide {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}

/* 弱化版：图还在，引导只是注记（不抢图的视觉层级）。 */
.onto-guide.is-weak {
  border-style: dashed;
  background: var(--surface-muted);
}

.onto-guide-text {
  min-width: 0;
}

.onto-guide h3 {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-primary);
}

.onto-guide p {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}

.onto-scale-hint {
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--r-sm);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-sm);
}

/* 宽屏：图 + 侧栏并排（侧栏固定 320px）；窄屏：侧栏折为图下方卡片。 */
.onto-main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: var(--sp-3);
  align-items: start;
}

@media (max-width: 1080px) {
  .onto-main {
    grid-template-columns: minmax(0, 1fr);
  }
}

.onto-panel-col {
  position: sticky;
  top: var(--sp-3);
}
</style>
