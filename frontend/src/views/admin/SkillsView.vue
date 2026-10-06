<template>
  <div class="admin-view">
    <PageHeader
      :title="t('skills', ui.lang)"
      :description="t('skillsPageDesc', ui.lang)"
      :breadcrumbs="crumbs"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <RefreshCw :size="14" />
        </el-button>
        <el-button type="primary" @click="openDraft(false)">
          <Pencil :size="14" />
          {{ t('skillsDraft', ui.lang) }}
        </el-button>
        <el-button type="primary" plain @click="openDraft(true)">
          <Sparkles :size="14" />
          {{ t('skillsLlmDraft', ui.lang) }}
        </el-button>
      </template>
    </PageHeader>

    <div class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('skills', ui.lang) }}</div>
        <div class="card-actions">
          <el-select v-model="values.status" class="filter-select" :aria-label="t('skillsFilterStatus', ui.lang)">
            <el-option :label="t('skillsFilterStatus', ui.lang)" value="" />
            <el-option :label="t('skillsStatusPending', ui.lang)" value="pending" />
            <el-option :label="t('skillsStatusConfirmed', ui.lang)" value="confirmed" />
            <el-option :label="t('skillsStatusDisabled', ui.lang)" value="disabled" />
            <el-option :label="t('skillsStatusRejected', ui.lang)" value="rejected" />
          </el-select>
          <el-select v-model="values.tier" class="filter-select" :aria-label="t('skillsFilterTier', ui.lang)">
            <el-option :label="t('skillsFilterTier', ui.lang)" value="" />
            <el-option :label="t('skillsRequired', ui.lang)" value="required" />
            <el-option :label="t('skillsAvailable', ui.lang)" value="available" />
            <el-option :label="t('skillsValidator', ui.lang)" value="validator" />
            <el-option :label="t('skillsGuard', ui.lang)" value="guard" />
          </el-select>
          <span class="pill pill-neutral">{{ filtered.length }} {{ t('skillsCount', ui.lang) }}</span>
        </div>
      </div>
      <el-table
        v-loading="loading"
        :data="filtered"
        class="admin-table"
        max-height="var(--table-max-h)"
      >
        <template #empty>
          <TableEmpty>{{ t('skillsEmpty', ui.lang) }}</TableEmpty>
        </template>
        <el-table-column :label="t('skillsName', ui.lang)" width="190">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.name }}</span>
            <span v-if="row.source === 'code'" class="pill pill-code">{{ t('skillsSourceCode', ui.lang) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('skillsDescription', ui.lang)" min-width="260">
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('skillsNode', ui.lang)" width="150">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.triggers?.node || t('skillsGlobal', ui.lang) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('skillsTier', ui.lang)" width="110">
          <template #default="{ row }">
            <!-- title 挂在包一层的 span 上:el-tag 不把未知 attr 透传到根元素,
                 直接写在它上面等于没写(实测 attributes() 里没有 title)。 -->
            <span v-if="row.source !== 'code'"
                  :title="tierHint(row.tier)"
            >
              <el-tag size="small" :type="tierTagType(row.tier)" effect="plain">
                {{ tierLabel(row.tier) }}
              </el-tag>
            </span>
            <span v-else class="pill pill-neutral">{{ t('skillsRequired', ui.lang) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('skillsStatus', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="pill" :class="statusClass(row.status)">{{ statusLabel(row.status) }}</span>
          </template>
        </el-table-column>
        <!-- 290px:最宽一行(en「Disable + Set available + Preview」)实测约 263px
             (12px SF 字宽 + small 按钮 22px 内边距 + 12px 间距 + 单元格 24px 内边距),
             250 会折行。加动作时同步重算。 -->
        <el-table-column label="" width="290" fixed="right">
          <template #default="{ row }">
            <template v-if="row.source !== 'code'">
              <el-button v-if="row.status === 'pending'" size="small" type="primary" @click="confirm(row)">
                {{ t('skillsConfirm', ui.lang) }}
              </el-button>
              <!-- reject 收窄到 pending:后端 reject 只查目录存在、不查状态,
                   对 disabled 行调用会直接删掉资产 —— 这条路径不能出现在 UI 里。 -->
              <el-button v-if="row.status === 'pending'" size="small" type="danger" plain @click="reject(row)">
                {{ t('skillsReject', ui.lang) }}
              </el-button>
              <el-button
                v-if="row.status === 'confirmed'"
                size="small"
                plain
                :title="t('skillsDisableHint', ui.lang)"
                @click="disable(row)"
              >
                {{ t('skillsDisable', ui.lang) }}
              </el-button>
              <el-button v-if="row.status === 'disabled'" size="small" type="primary" plain @click="enable(row)">
                {{ t('skillsEnable', ui.lang) }}
              </el-button>
              <!-- 档位切换只给 required/available:validator 与 guard 的
                   四字段按当前 tier 投影,两个方向都 400,按钮恒失败不如不给。 -->
              <el-button
                v-if="row.status === 'confirmed' && (row.tier === 'required' || row.tier === 'available')"
                size="small"
                plain
                @click="toggleTier(row)"
              >
                {{ row.tier === 'required' ? t('skillsMakeAvailable', ui.lang) : t('skillsMakeRequired', ui.lang) }}
              </el-button>
            </template>
            <el-button size="small" plain @click="preview(row)">
              {{ t('skillsPreview', ui.lang) }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="editorOpen"
      :title="llmMode ? t('skillsLlmDraft', ui.lang) : t('skillsDraft', ui.lang)"
      width="680px"
    >
      <p class="view-desc">{{ t('skillsEditorHint', ui.lang) }}</p>
      <el-form label-position="top">
        <el-form-item :label="t('skillsName', ui.lang)">
          <el-input v-model="form.name" placeholder="loan-caliber" spellcheck="false" />
        </el-form-item>
        <el-form-item :label="t('skillsDescription', ui.lang)">
          <el-input v-model="form.description" :placeholder="t('skillsDescriptionPh', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('skillsNode', ui.lang)">
          <el-select v-model="form.node" allow-create filterable clearable :placeholder="t('skillsGlobal', ui.lang)">
            <el-option label="query_sketch" value="query_sketch" />
            <el-option label="analyze_error" value="analyze_error" />
            <el-option label="gen_sql" value="gen_sql" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="llmMode" :label="t('skillsPurpose', ui.lang)">
          <el-input v-model="form.purpose" type="textarea" :rows="3" :placeholder="t('skillsPurposePh', ui.lang)" />
        </el-form-item>
        <el-form-item v-else :label="t('skillsBody', ui.lang)">
          <el-input v-model="form.body" type="textarea" :rows="8" :placeholder="t('skillsBodyPh', ui.lang)" />
        </el-form-item>
        <el-form-item v-if="!llmMode" :label="t('skillsTier', ui.lang)">
          <el-radio-group v-model="form.tier">
            <el-radio value="available">{{ t('skillsAvailable', ui.lang) }}</el-radio>
            <el-radio value="required">{{ t('skillsRequired', ui.lang) }}</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <div v-if="saveError" class="decisions-error cell-mono">{{ saveError }}</div>
      <template #footer>
        <el-button @click="editorOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ t('skillsDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="previewOpen" :title="previewName" size="520px">
      <pre class="cell-mono skill-preview">{{ previewBody }}</pre>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Pencil, RefreshCw, Sparkles } from 'lucide-vue-next'
import { apiGet, apiPost } from '../../api/http'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, toastError } from '../../utils/notify'
import { useListQuery } from '../../composables/useListQuery'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import PageHeader from '../../components/base/PageHeader.vue'

interface SkillRow {
  name: string
  description: string
  source: string
  status: string
  tier: string
  triggers: { node?: string }
  body?: string
  [k: string]: unknown
}

const ui = useUiStore()
const skills = ref<SkillRow[]>([])
const loading = ref(false)
const saving = ref(false)
const editorOpen = ref(false)
const llmMode = ref(false)
const saveError = ref('')
const previewOpen = ref(false)
const previewName = ref('')
const previewBody = ref('')

const form = ref({
  name: '',
  description: '',
  node: '',
  purpose: '',
  body: '',
  tier: 'available',
})

/* ── URL state (P6 §4.3) ──────────────────────────────────────────────────
   status / tier are the keys this page acknowledges. The list endpoint
   returns every source (code + org, all states), so both filters run
   client-side and /admin/skills?status=pending lands on the review queue. */
const { values } = useListQuery({ status: '', tier: '' })

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('skills', ui.lang) },
])

const filtered = computed(() =>
  skills.value.filter((row) => {
    if (values.status && row.status !== values.status) return false
    if (values.tier && row.tier !== values.tier) return false
    return true
  }),
)

function statusClass(s: string): string {
  if (s === 'confirmed') return 'pill-ok'
  if (s === 'pending') return 'pill-warn'
  if (s === 'disabled') return 'pill-disabled'
  return 'pill-neutral'
}

function statusLabel(s: string): string {
  if (s === 'confirmed') return t('skillsStatusConfirmed', ui.lang)
  if (s === 'pending') return t('skillsStatusPending', ui.lang)
  if (s === 'disabled') return t('skillsStatusDisabled', ui.lang)
  return t('skillsStatusRejected', ui.lang)
}

// validator / guard 是**非提示词档**,不是"没选 required 的 available" ——
// validator 按结果断言运行、guard 在 execute_sql 前拦截,正文从不投给模型。
// 落到 available 分支上,显示的就是别人的名字。
function tierLabel(tier: string): string {
  if (tier === 'required') return t('skillsRequired', ui.lang)
  if (tier === 'validator') return t('skillsValidator', ui.lang)
  if (tier === 'guard') return t('skillsGuard', ui.lang)
  return t('skillsAvailable', ui.lang)
}

function tierTagType(tier: string): string {
  if (tier === 'required') return 'warning'
  if (tier === 'validator' || tier === 'guard') return 'danger'
  return 'info'
}

// 手写档(validator / guard)的提示:说清"SKILL.md 里手写"、别让人去找
// 一个不存在的按钮。title 挂在外层 span(el-tag 双根条件渲染,不吃 attr)。
function tierHint(tier: string): string {
  if (tier === 'validator') return t('skillsValidatorHint', ui.lang)
  if (tier === 'guard') return t('skillsGuardHint', ui.lang)
  return ''
}

async function load() {
  loading.value = true
  try {
    const body = await apiGet('/v1/admin/skills')
    skills.value = (body.skills ?? []) as SkillRow[]
  } catch (e) {
    skills.value = []
    toastError(e)
  } finally {
    loading.value = false
  }
}

function openDraft(llm: boolean) {
  llmMode.value = llm
  saveError.value = ''
  form.value = { name: '', description: '', node: '', purpose: '', body: '', tier: 'available' }
  editorOpen.value = true
}

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    if (llmMode.value) {
      await apiPost('/v1/admin/skills/llm-draft', {
        name: form.value.name,
        description: form.value.description,
        node: form.value.node,
        purpose: form.value.purpose,
        lang: ui.lang === 'zh' ? 'zh' : 'en',
      })
    } else {
      await apiPost('/v1/admin/skills/draft', {
        name: form.value.name,
        description: form.value.description,
        triggers: form.value.node ? { node: form.value.node } : {},
        tier: form.value.tier,
        lang: ui.lang === 'zh' ? 'zh' : 'en',
        body: form.value.body,
      })
    }
    notifySuccess(t('skillsDrafted', ui.lang))
    editorOpen.value = false
    await load()
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function confirm(row: SkillRow) {
  try {
    await apiPost(`/v1/admin/skills/${row.name}/confirm`)
    notifySuccess(t('skillsConfirmed', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function reject(row: SkillRow) {
  try {
    await apiPost(`/v1/admin/skills/${row.name}/reject`)
    notifySuccess(t('skillsRejected', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

// 颗粒停用/启用。后端严格非幂等(disable 仅 confirmed→disabled、
// enable 仅 disabled→confirmed,非法流转 400)—— 按钮的 v-if 已保证
// UI 只给合法转移,这里不做"兜底重试",失败就是失败,照原样报出来。
async function disable(row: SkillRow) {
  try {
    await apiPost(`/v1/admin/skills/${row.name}/disable`)
    notifySuccess(t('skillsDisabled', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function enable(row: SkillRow) {
  try {
    await apiPost(`/v1/admin/skills/${row.name}/enable`)
    notifySuccess(t('skillsEnabled', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function toggleTier(row: SkillRow) {
  const tier = row.tier === 'required' ? 'available' : 'required'
  try {
    await apiPost(`/v1/admin/skills/${row.name}/tier`, { tier })
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function preview(row: SkillRow) {
  previewName.value = row.name
  try {
    const body = await apiGet(`/v1/admin/skills/${row.name}/body`)
    previewBody.value = (body.body ?? '') as string
    previewOpen.value = true
  } catch (e) {
    toastError(e)
  }
}

onMounted(load)
</script>

<style scoped>
.skill-preview {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 70vh;
  overflow: auto;
}
.pill-code {
  margin-left: 6px;
}
</style>
