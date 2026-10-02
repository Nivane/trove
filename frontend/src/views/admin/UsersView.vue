<!--
  UsersView — the P6/P7 vertical slice: console shell + server-side list.

  Rules this page encodes (they carry to every other /admin page):
    · filters, sort and page live in the URL (useListQuery) — shareable,
      refresh-proof, back-button-undoable;
    · KPI tiles are filters ("the numbers are clickable");
    · the list is fetched from the server (q/role/status/sort/order/limit/
      offset) — no full-table pull, no per-user grant requests;
    · writes are deliberate: the drawer stages profile + grant edits behind
      an explicit Save, the create dialog validates per field, deletes go
      through a ConfirmDialog that states the blast radius.
-->
<template>
  <div class="admin-view users-page">
    <PageHeader
      :title="t('users', ui.lang)"
      :description="t('usersPageDesc', ui.lang)"
      :breadcrumbs="crumbs"
    >
      <template #actions>
        <el-button type="primary" @click="openCreate">
          <UserPlus :size="15" class="btn-icon" />
          {{ t('createUser', ui.lang) }}
        </el-button>
      </template>
    </PageHeader>

    <!-- KPI row — every number is a filter. -->
    <div class="kpi-row">
      <KpiTile
        v-for="tile in kpiTiles"
        :key="tile.key"
        :label="tile.label"
        :value="tile.value"
        :sub="tile.sub"
        :active="tile.active"
        @click="applyKpi(tile.key)"
      />
    </div>

    <div class="list-toolbar">
      <el-input
        v-model="values.q"
        class="toolbar-search"
        :prefix-icon="Search"
        :placeholder="t('searchUsers', ui.lang)"
        :aria-label="t('usersSearchAria', ui.lang)"
        clearable
      />
      <el-select v-model="values.role" class="filter-select">
        <el-option :label="t('usersFilterRole', ui.lang)" value="" />
        <el-option
          v-for="r in ROLES"
          :key="r"
          :label="roleLabel(r)"
          :value="r"
        />
      </el-select>
      <el-select v-model="values.status" class="filter-select">
        <el-option :label="t('usersFilterStatus', ui.lang)" value="" />
        <el-option
          v-for="s in STATUSES"
          :key="s"
          :label="statusLabel(s)"
          :value="s"
        />
      </el-select>
      <span class="spacer" />
      <span class="view-count">{{ t('usersTotal', ui.lang, total) }}</span>
    </div>

    <div v-if="selected.length" class="bulk-bar" role="status">
      <span class="bulk-count">
        {{ t('usersSelectedCount', ui.lang, selected.length) }}
      </span>
      <el-button size="small" :loading="bulkBusy" @click="bulkSetDisabled(false)">
        {{ t('enable', ui.lang) }}
      </el-button>
      <el-button size="small" :loading="bulkBusy" @click="bulkSetDisabled(true)">
        {{ t('disable', ui.lang) }}
      </el-button>
      <span class="spacer" />
      <el-button size="small" type="danger" plain @click="askDeleteSelected">
        {{ t('delete', ui.lang) }}
      </el-button>
      <el-button size="small" text @click="clearSelection">
        {{ t('usersClearSelection', ui.lang) }}
      </el-button>
    </div>

    <!-- error state: the list failed and there is nothing to show -->
    <StatePanel
      v-if="listError && !users.length"
      mode="error"
      :title="t('usersErrorTitle', ui.lang)"
      :description="t('usersErrorDesc', ui.lang)"
      :detail="listError"
      :retry-text="t('retry', ui.lang)"
      @retry="reload"
    />

    <DataTable
      v-else
      :columns="columns"
      :rows="users"
      row-key="id"
      selectable
      row-clickable
      :selected="selected"
      :sort="tableSort"
      :loading="loading"
      :skeleton-rows="6"
      :select-all-label="t('usersSelectAll', ui.lang)"
      :select-row-label="t('usersSelectRow', ui.lang)"
      @update:selected="selected = $event"
      @update:sort="onSort"
      @row-click="onRowClick"
    >
      <template #cell-username="{ row, index }">
        <div class="u-cell">
          <span class="avatar" :class="avatarClass(u(row), index)">
            {{ avatarChar(u(row)) }}
          </span>
          <div class="u-meta">
            <span class="u-name">{{ u(row).username }}</span>
            <span v-if="u(row).display_name" class="u-sub">
              {{ u(row).display_name }}
            </span>
          </div>
        </div>
      </template>

      <template #cell-role="{ row }">
        <span class="tag-pill" :class="`is-${u(row).role}`">
          {{ roleLabel(u(row).role) }}
        </span>
      </template>

      <template #cell-disabled="{ row }">
        <span class="tag-pill" :class="u(row).disabled ? 'is-off' : 'is-ok'">
          <span class="tag-dot" />
          {{ u(row).disabled ? t('statusDisabled', ui.lang) : t('statusActive', ui.lang) }}
        </span>
      </template>

      <template #cell-created_at="{ row }">
        <span class="cell-time" :title="fmtDateTime(u(row).created_at)">
          {{ fmtDateTime(u(row).created_at) || '—' }}
        </span>
      </template>

      <template #cell-grants="{ row }">
        <span
          class="grant-chip"
          :class="{ 'is-none': !(u(row).datasources ?? []).length }"
        >
          {{ grantChip(u(row)) }}
        </span>
      </template>

      <template #cell-actions="{ row }">
        <div class="row-actions">
          <button
            type="button"
            class="icon-btn"
            :aria-label="t('edit', ui.lang)"
            :title="t('edit', ui.lang)"
            @click.stop="openDrawer(row)"
          >
            <Pencil :size="14" />
          </button>
          <button
            type="button"
            class="icon-btn"
            :aria-label="t('apiTokens', ui.lang)"
            :title="t('apiTokens', ui.lang)"
            @click.stop="openDrawer(row, 'tokens')"
          >
            <KeyRound :size="14" />
          </button>
          <button
            type="button"
            class="icon-btn is-danger"
            :aria-label="t('deleteUser', ui.lang)"
            :title="t('deleteUser', ui.lang)"
            @click.stop="askDelete([u(row)])"
          >
            <Trash2 :size="14" />
          </button>
        </div>
      </template>

      <template #empty>
        <StatePanel
          v-if="isFiltered"
          mode="empty"
          :title="t('usersEmptyFilteredTitle', ui.lang)"
          :description="t('usersEmptyFilteredDesc', ui.lang)"
        >
          <template #action>
            <el-button size="small" @click="clearFilters">
              {{ t('usersClearFilters', ui.lang) }}
            </el-button>
          </template>
        </StatePanel>
        <StatePanel
          v-else
          mode="empty"
          :title="t('usersEmptyFirstTitle', ui.lang)"
          :description="t('usersEmptyFirstDesc', ui.lang)"
        >
          <template #action>
            <el-button size="small" type="primary" @click="openCreate">
              {{ t('usersCreateFirst', ui.lang) }}
            </el-button>
          </template>
        </StatePanel>
      </template>
    </DataTable>

    <div v-if="!listError || users.length" class="list-footer">
      <span class="view-count">
        {{ t('usersTotal', ui.lang, total) }}
        <template v-if="isFiltered">· {{ t('usersFiltered', ui.lang) }}</template>
      </span>
      <span class="spacer" />
      <span class="pager-size-label">
        {{ t('usersPageSize', ui.lang) }}
        <el-select v-model="pageSize" size="small" class="pager-size">
          <el-option v-for="n in PAGE_SIZES" :key="n" :label="String(n)" :value="n" />
        </el-select>
      </span>
      <button
        type="button"
        class="icon-btn"
        :aria-label="t('usersPrevPage', ui.lang)"
        :disabled="page <= 1"
        @click="setPage(page - 1)"
      >
        <ChevronLeft :size="15" />
      </button>
      <span class="pager-page">{{ page }} / {{ pageCount }}</span>
      <button
        type="button"
        class="icon-btn"
        :aria-label="t('usersNextPage', ui.lang)"
        :disabled="page >= pageCount"
        @click="setPage(page + 1)"
      >
        <ChevronRight :size="15" />
      </button>
    </div>

    <!-- ── detail drawer: profile + grants (staged) + tokens + activity ── -->
    <DetailDrawer
      v-model="drawerOpen"
      width="460px"
      :aria-label="t('usersDrawerAria', ui.lang)"
      :close-label="t('close', ui.lang)"
      :before-close="beforeDrawerClose"
    >
      <template #header>
        <div class="drawer-head">
          <span class="avatar" :class="avatarClass(drawerUser, 0)">
            {{ drawerUser ? avatarChar(drawerUser) : '?' }}
          </span>
          <div class="who">
            <div class="name">
              {{ drawerUser?.display_name || drawerUser?.username }}
              <span class="handle">@{{ drawerUser?.username }}</span>
            </div>
            <div class="sub">
              <span class="tag-pill" :class="`is-${draft.role}`">
                {{ roleLabel(draft.role) }}
              </span>
              <span class="tag-pill" :class="draft.disabled ? 'is-off' : 'is-ok'">
                <span class="tag-dot" />
                {{ draft.disabled ? t('statusDisabled', ui.lang) : t('statusActive', ui.lang) }}
              </span>
            </div>
          </div>
        </div>
      </template>

      <!-- profile -->
      <section class="dsec">
        <h4>
          {{ t('usersDrawerProfile', ui.lang) }}
          <span v-if="dirty" class="dirty-badge">{{ t('usersDirty', ui.lang) }}</span>
        </h4>
        <el-form label-position="top" class="profile-form" @submit.prevent>
          <el-form-item :label="t('displayName', ui.lang)">
            <el-input v-model="draft.display_name" :placeholder="t('displayNameOptional', ui.lang)" />
          </el-form-item>
          <el-form-item :label="t('role', ui.lang)">
            <el-select v-model="draft.role" class="profile-select">
              <el-option :label="t('userRole', ui.lang)" value="user" />
              <el-option :label="t('analystRole', ui.lang)" value="analyst" />
              <el-option :label="t('adminRole', ui.lang)" value="admin" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('status', ui.lang)">
            <el-switch
              v-model="draft.disabled"
              :active-text="t('statusDisabled', ui.lang)"
              :inactive-text="t('statusActive', ui.lang)"
            />
          </el-form-item>
          <el-form-item :label="t('usersNewPassword', ui.lang)">
            <el-input
              v-model="draft.password"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="t('passwordKeep', ui.lang)"
            />
          </el-form-item>
        </el-form>
      </section>

      <!-- datasource grants (staged — nothing is written until Save) -->
      <section class="dsec">
        <h4>
          {{ t('usersDrawerGrants', ui.lang) }}
          <span v-if="dirty" class="dirty-badge">{{ t('usersDirty', ui.lang) }}</span>
        </h4>
        <div class="ds-checks">
          <label v-for="d in grantOptions" :key="d" class="ds-check">
            <input
              class="ds-check-input"
              type="checkbox"
              :checked="draft.datasources.includes(d)"
              :aria-label="d"
              @change="toggleGrant(d)"
            >
            <span>{{ d }}</span>
          </label>
        </div>
        <p class="dsec-note">{{ t('usersGrantsNote', ui.lang) }}</p>
      </section>

      <!-- api tokens -->
      <section ref="tokensSectionEl" class="dsec">
        <h4>{{ t('apiTokens', ui.lang) }}</h4>
        <div v-if="tokenRaw" class="token-reveal">
          <div class="token-reveal-title">{{ t('tokenRevealTitle', ui.lang) }}</div>
          <div class="token-reveal-row">
            <code class="token-reveal-code">{{ tokenRaw }}</code>
            <button
              type="button"
              class="icon-btn"
              :aria-label="t('copy', ui.lang)"
              :title="t('copy', ui.lang)"
              @click="copyTokenRaw"
            >
              <Check v-if="tokenCopied" :size="14" />
              <Copy v-else :size="14" />
            </button>
          </div>
        </div>
        <div class="token-create">
          <el-input v-model="tokenForm.label" size="small" :placeholder="t('tokenLabel', ui.lang)" />
          <el-input-number v-model="tokenForm.ttl" :min="0" size="small" :placeholder="t('tokenTtl', ui.lang)" />
          <el-input v-model="tokenForm.scopes" size="small" :placeholder="t('tokenScopes', ui.lang)" />
          <el-button size="small" type="primary" :loading="tokenBusy" @click="createToken">
            {{ t('createToken', ui.lang) }}
          </el-button>
        </div>
        <p class="dsec-note">{{ t('tokenTtlHint', ui.lang) }} · {{ t('tokenScopesHint', ui.lang) }}</p>
        <div class="token-list">
          <p v-if="!tokens.length && !tokenBusy" class="dsec-note">{{ t('noTokens', ui.lang) }}</p>
          <div v-for="tk in tokens" :key="tk.id" class="token-row">
            <span class="token-dot" :class="tk.revoked ? 'is-off' : 'is-on'" />
            <div class="token-meta">
              <span class="token-label">{{ tk.label || '—' }}</span>
              <span class="token-sub">
                {{ tk.revoked ? t('revoked', ui.lang) : t('created', ui.lang) }}
                {{ fmtDateTime(tk.created_at) }}
                <template v-if="tk.expires_at">
                  · {{ t('expiresAt', ui.lang) }} {{ fmtDateTime(tk.expires_at) }}
                </template>
              </span>
            </div>
            <el-button
              v-if="!tk.revoked"
              size="small"
              text
              type="danger"
              @click="revokeToken(tk)"
            >
              {{ t('revoke', ui.lang) }}
            </el-button>
          </div>
        </div>
      </section>

      <!-- recent activity (audit, read-only) -->
      <section class="dsec">
        <h4>
          {{ t('usersDrawerActivity', ui.lang) }}
          <span class="h4-hint">· {{ t('usersDrawerActivityFrom', ui.lang) }}</span>
          <span class="spacer" />
          <RouterLink
            v-if="drawerUser"
            class="dsec-link"
            :to="{ path: '/admin/audit', query: { user_id: String(drawerUser.id) } }"
          >
            {{ t('usersDrawerViewAll', ui.lang) }} →
          </RouterLink>
        </h4>
        <p v-if="!activity.length" class="dsec-note">{{ t('usersDrawerNoActivity', ui.lang) }}</p>
        <div v-for="a in activity" :key="a.id" class="act-row">
          <span class="act-time">{{ fmtDateTime(a.ts) }}</span>
          <span class="act-action">{{ a.action }}</span>
        </div>
      </section>

      <!-- danger zone -->
      <section class="dsec">
        <h4>{{ t('usersDangerZone', ui.lang) }}</h4>
        <div class="danger-zone">
          <div class="dz-title">{{ t('usersDeleteAccount', ui.lang) }}</div>
          <div class="dz-desc">{{ t('usersDeleteAccountDesc', ui.lang) }}</div>
          <el-button size="small" type="danger" plain @click="askDeleteDrawerUser">
            {{ t('deleteUser', ui.lang) }}
          </el-button>
        </div>
      </section>

      <template #footer>
        <el-button :disabled="!dirty || saving" @click="discardDraft">
          {{ t('usersDiscard', ui.lang) }}
        </el-button>
        <el-button type="primary" :disabled="!dirty" :loading="saving" @click="saveDraft">
          {{ t('usersSaveChanges', ui.lang) }}
        </el-button>
      </template>
    </DetailDrawer>

    <!-- ── create user ── -->
    <el-dialog
      v-model="createOpen"
      :title="t('createUser', ui.lang)"
      width="440"
      class="admin-dialog"
      :close-on-click-modal="false"
      :before-close="beforeCreateClose"
    >
      <el-form
        :model="createForm"
        label-position="top"
        @submit.prevent
      >
        <el-form-item :label="t('username', ui.lang)" prop="username" :error="createErrors.username">
          <el-input
            v-model="createForm.username"
            :placeholder="t('usersUsernameHint', ui.lang)"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item :label="t('displayName', ui.lang)" prop="display_name">
          <el-input v-model="createForm.display_name" :placeholder="t('displayNameOptional', ui.lang)" />
        </el-form-item>
        <el-form-item :label="t('loginPass', ui.lang)" prop="password" :error="createErrors.password">
          <el-input
            v-model="createForm.password"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="t('usersPasswordHint', ui.lang)"
          />
        </el-form-item>
        <el-form-item :label="t('role', ui.lang)">
          <el-select v-model="createForm.role" class="profile-select">
            <el-option :label="t('userRole', ui.lang)" value="user" />
            <el-option :label="t('analystRole', ui.lang)" value="analyst" />
            <el-option :label="t('adminRole', ui.lang)" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="requestCreateClose">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">
          {{ t('createUser', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── delete confirmation (blast radius, not a one-liner) ── -->
    <ConfirmDialog
      v-model="deleteOpen"
      :title="deleteTitle"
      :confirm-text="deleteTitle"
      :cancel-text="t('cancel', ui.lang)"
      danger
      :loading="deleting"
      @confirm="doDelete"
    >
      <p class="confirm-names">{{ deleteNames }}</p>
      <template #impact>
        <ul class="impact-list">
          <li v-if="deleteTokenCount !== null">
            {{ t('usersImpactTokens', ui.lang, deleteTokenCount) }}
          </li>
          <li v-else>{{ t('usersImpactUnknown', ui.lang) }}</li>
          <li>{{ t('usersImpactGrants', ui.lang, deleteGrantCount) }}</li>
          <li>{{ t('usersImpactKeep', ui.lang) }}</li>
          <li>{{ t('usersImpactLogout', ui.lang) }}</li>
        </ul>
      </template>
    </ConfirmDialog>

    <!-- ── unsaved-changes guard (drawer + create dialog share it) ── -->
    <ConfirmDialog
      v-model="discardOpen"
      :title="t('usersDiscardTitle', ui.lang)"
      :confirm-text="t('usersDiscard', ui.lang)"
      :cancel-text="t('usersKeepEditing', ui.lang)"
      danger
      @confirm="confirmDiscard"
    >
      <p>{{ t('usersDiscardDesc', ui.lang) }}</p>
    </ConfirmDialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  KeyRound,
  Pencil,
  Search,
  Trash2,
  UserPlus,
} from 'lucide-vue-next'
import { apiDelete, apiGet, apiPatch, apiPost, apiPut, ApiError } from '../../api/http'
import type { AdminUser } from '../../api/types'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, toastError } from '../../utils/notify'
import { copyText, fmtDateTime } from '../../utils/format'
import { useListQuery } from '../../composables/useListQuery'
import PageHeader from '../../components/base/PageHeader.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import DataTable, { type DataTableColumn, type DataTableSort } from '../../components/base/DataTable.vue'
import DetailDrawer from '../../components/base/DetailDrawer.vue'
import ConfirmDialog from '../../components/base/ConfirmDialog.vue'
import StatePanel from '../../components/base/StatePanel.vue'

interface TokenRow {
  id: number
  label?: string
  revoked?: number | boolean
  created_at?: string
  expires_at?: string | null
  scopes?: string[]
}

interface ActivityRow {
  id: number
  ts?: string
  action?: string
  username?: string
}

const ROLES = ['admin', 'analyst', 'user'] as const
const STATUSES = ['active', 'disabled', 'nogrant'] as const
const PAGE_SIZES = [20, 50, 100]
type KpiKey = 'all' | 'active' | 'admin' | 'disabled' | 'nogrant'

const ui = useUiStore()

/* ── list state: filters/sort/page live in the URL, never in the session ── */
const { values, isActive: isFiltered, reset: resetFilters } = useListQuery({
  q: '',
  role: '',
  status: '',
  sort: 'created_at',
  order: 'desc',
  page: '1',
})

const users = ref<AdminUser[]>([])
const total = ref(0)
const loading = ref(false)
const listError = ref('')
const selected = ref<(string | number)[]>([])
const bulkBusy = ref(false)
const pageSize = ref(20)
const kpis = ref<Record<KpiKey, number | null>>({
  all: null,
  active: null,
  admin: null,
  disabled: null,
  nogrant: null,
})
const knownDatasources = ref<string[]>([])

const page = computed(() => Math.max(1, Number.parseInt(values.page, 10) || 1))
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const tableSort = computed<DataTableSort>(() => ({
  key: values.sort,
  dir: values.order === 'asc' ? 'asc' : 'desc',
}))

const columns = computed<DataTableColumn[]>(() => [
  { key: 'username', label: t('username', ui.lang), sortable: true },
  { key: 'role', label: t('role', ui.lang), sortable: true, width: 110 },
  { key: 'disabled', label: t('status', ui.lang), sortable: true, width: 116 },
  {
    key: 'created_at',
    label: t('createdAt', ui.lang),
    sortable: true,
    width: 150,
    defaultDir: 'desc',
  },
  { key: 'grants', label: t('usersColGrants', ui.lang), width: 150 },
  { key: 'actions', label: t('actions', ui.lang), width: 108 },
])

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('users', ui.lang) },
])

const kpiTiles = computed(() => [
  {
    key: 'all' as KpiKey,
    label: t('usersKpiAll', ui.lang),
    value: kpiValue('all'),
    sub: t('usersKpiAllSub', ui.lang),
    active: values.role === '' && values.status === '',
  },
  {
    key: 'active' as KpiKey,
    label: t('usersKpiActive', ui.lang),
    value: kpiValue('active'),
    sub: t('usersKpiActiveSub', ui.lang),
    active: values.status === 'active',
  },
  {
    key: 'admin' as KpiKey,
    label: t('adminRole', ui.lang),
    value: kpiValue('admin'),
    sub: t('usersKpiAdminsSub', ui.lang),
    active: values.role === 'admin',
  },
  {
    key: 'disabled' as KpiKey,
    label: t('statusDisabled', ui.lang),
    value: kpiValue('disabled'),
    sub: t('usersKpiDisabledSub', ui.lang),
    active: values.status === 'disabled',
  },
  {
    key: 'nogrant' as KpiKey,
    label: t('usersKpiNogrant', ui.lang),
    value: kpiValue('nogrant'),
    sub: t('usersKpiNograntSub', ui.lang),
    active: values.status === 'nogrant',
  },
])

/* ── fetching ───────────────────────────────────────────────────────────── */

function listPath(extra: Record<string, string> = {}): string {
  const params = new URLSearchParams()
  if (values.q) params.set('q', values.q)
  if (values.role) params.set('role', values.role)
  if (values.status) params.set('status', values.status)
  params.set('sort', values.sort)
  params.set('order', values.order)
  for (const [k, v] of Object.entries(extra)) params.set(k, v)
  return `/v1/admin/users?${params.toString()}`
}

async function load() {
  loading.value = true
  listError.value = ''
  try {
    const body = await apiGet(
      listPath({
        limit: String(pageSize.value),
        offset: String((page.value - 1) * pageSize.value),
      }),
    )
    users.value = (body.users ?? []) as AdminUser[]
    total.value = Number(body.total ?? users.value.length)
  } catch (e) {
    listError.value = e instanceof Error ? e.message : String(e)
    // A transient failure keeps the last good page on screen; the loud
    // surface is the StatePanel above (error + rows would double-report).
    if (users.value.length) toastError(e)
  } finally {
    loading.value = false
  }
}

/** KPI counts come from the same endpoint (limit=1, read `total`) — no
 *  second contract, and each facet counts against its own filter. */
async function loadKpis() {
  const facets: [KpiKey, Record<string, string>][] = [
    ['all', {}],
    ['active', { status: 'active' }],
    ['admin', { role: 'admin' }],
    ['disabled', { status: 'disabled' }],
    ['nogrant', { status: 'nogrant' }],
  ]
  const results = await Promise.all(
    facets.map(async ([key, filter]) => {
      const params = new URLSearchParams({ ...filter, limit: '1' })
      try {
        const body = await apiGet(`/v1/admin/users?${params.toString()}`)
        return [key, Number(body.total ?? 0)] as const
      } catch {
        return [key, null] as const
      }
    }),
  )
  const next = { ...kpis.value }
  for (const [key, value] of results) next[key] = value
  kpis.value = next
}

async function loadDatasources() {
  try {
    const body = await apiGet('/v1/catalog/datasources')
    knownDatasources.value = (body.datasources ?? []).map((d: { name: string }) => d.name)
  } catch {
    knownDatasources.value = []
  }
}

async function reload() {
  await Promise.all([load(), loadKpis()])
}

onMounted(() => {
  void reload()
  void loadDatasources()
})

// A page-size change renumbers the pages under the user's feet: go back to
// page 1 first (this watcher is created before the fetch watcher below, so
// the reset lands before the fetch runs).
watch(pageSize, () => {
  if (values.page !== '1') values.page = '1'
})

// Filter changes refetch — and restart at page 1: page 7 of the old result
// set is meaningless. When the page does change, the fetch watcher below is
// queued by it instead of a second load firing here, and Vue batches a KPI
// click (role + status together) into one run.
watch(
  [() => values.q, () => values.role, () => values.status],
  () => {
    if (values.page !== '1') values.page = '1'
    else void load()
  },
)
watch([() => values.sort, () => values.order, () => values.page, pageSize], () => void load())

/* ── filters ────────────────────────────────────────────────────────────── */

function applyKpi(key: KpiKey) {
  if (key === 'all') {
    values.role = ''
    values.status = ''
  } else if (key === 'admin') {
    values.role = values.role === 'admin' ? '' : 'admin'
    values.status = ''
  } else {
    values.status = values.status === key ? '' : key
  }
}

function clearFilters() {
  resetFilters()
}

function onSort(next: DataTableSort) {
  values.sort = next.key
  values.order = next.dir
}

function setPage(next: number) {
  values.page = String(Math.min(Math.max(1, next), pageCount.value))
}

/* ── KPI / row formatting ───────────────────────────────────────────────── */

function kpiValue(key: KpiKey): string {
  const v = kpis.value[key]
  return v === null ? '—' : String(v)
}

function roleLabel(role: string): string {
  if (role === 'admin') return t('adminRole', ui.lang)
  if (role === 'analyst') return t('analystRole', ui.lang)
  return t('userRole', ui.lang)
}

function statusLabel(status: string): string {
  if (status === 'active') return t('statusActive', ui.lang)
  if (status === 'disabled') return t('statusDisabled', ui.lang)
  return t('usersKpiNogrant', ui.lang)
}

function u(row: unknown): AdminUser {
  return row as AdminUser
}

function avatarChar(user: AdminUser): string {
  return ((user.display_name || user.username || '?').trim()[0] || '?').toUpperCase()
}

function avatarClass(user: AdminUser | null, index = 0): string {
  if (!user || user.disabled) return 'is-muted'
  return `av-${(index % 3) + 1}`
}

function grantChip(user: AdminUser): string {
  const grants = user.datasources ?? []
  if (!grants.length) return t('usersKpiNogrant', ui.lang)
  if (grants.length === 1) return grants[0]
  return t('usersGrantsCount', ui.lang, grants.length)
}

/* ── selection + bulk ───────────────────────────────────────────────────── */

function clearSelection() {
  selected.value = []
}

async function bulkSetDisabled(disabled: boolean) {
  const ids = selected.value.map(Number)
  if (!ids.length) return
  bulkBusy.value = true
  try {
    await Promise.all(
      ids.map((id) => apiPatch(`/v1/admin/users/${id}`, { disabled })),
    )
    notifySuccess(t('usersBulkUpdated', ui.lang, ids.length))
    clearSelection()
    await reload()
  } catch (e) {
    toastError(e)
  } finally {
    bulkBusy.value = false
  }
}

/* ── delete (single row, drawer, or bulk — one confirm, one copy) ───────── */

const deleteOpen = ref(false)
const deleting = ref(false)
const deleteTargets = ref<AdminUser[]>([])
const deleteTokenCount = ref<number | null>(null)

const deleteTitle = computed(() =>
  deleteTargets.value.length === 1
    ? t('usersDeleteTitleOne', ui.lang)
    : t('usersDeleteTitle', ui.lang, deleteTargets.value.length),
)
const deleteNames = computed(() =>
  deleteTargets.value.map((uu) => uu.username).join(', '),
)
const deleteGrantCount = computed(() =>
  deleteTargets.value.reduce((n, uu) => n + (uu.datasources ?? []).length, 0),
)

/** Exact impact for a couple of users: one tokens request each, charged only
 *  when a destructive confirmation is actually opened. Only *active* tokens
 *  count — a revoked one is already inert, so counting it would overstate
 *  what the admin is about to break. Unknown counts fall back to wording
 *  without numbers rather than to a made-up number. */
async function loadTokenCounts(targets: AdminUser[]) {
  const counts = await Promise.all(
    targets.map(async (uu) => {
      const cached = tokenCountCache.get(uu.id)
      if (cached !== undefined) return cached
      try {
        const body = await apiGet(`/v1/admin/users/${uu.id}/tokens`)
        const n = ((body.tokens ?? []) as TokenRow[]).filter((tk) => !tk.revoked).length
        tokenCountCache.set(uu.id, n)
        return n
      } catch {
        return null
      }
    }),
  )
  return counts.some((c) => c === null)
    ? null
    : counts.reduce<number>((a, c) => a + (c ?? 0), 0)
}

async function askDelete(targets: AdminUser[]) {
  if (!targets.length) return
  deleteTargets.value = targets
  deleteTokenCount.value = null
  deleteOpen.value = true
  deleteTokenCount.value = await loadTokenCounts(targets)
}

function askDeleteSelected() {
  const ids = new Set(selected.value.map(Number))
  void askDelete(users.value.filter((uu) => ids.has(uu.id)))
}

function askDeleteDrawerUser() {
  if (drawerUser.value) void askDelete([drawerUser.value])
}

async function doDelete() {
  const targets = deleteTargets.value
  if (!targets.length) return
  deleting.value = true
  try {
    await Promise.all(targets.map((uu) => apiDelete(`/v1/admin/users/${uu.id}`)))
    notifySuccess(
      targets.length === 1
        ? t('userDeletedOk', ui.lang)
        : t('usersDeletedCount', ui.lang, targets.length),
    )
    const removed = new Set(targets.map((uu) => uu.id))
    selected.value = selected.value.filter((k) => !removed.has(Number(k)))
    for (const id of removed) tokenCountCache.delete(id)
    deleteOpen.value = false
    if (drawerUser.value && removed.has(drawerUser.value.id)) closeDrawerNow()
    await reload()
  } catch (e) {
    toastError(e)
  } finally {
    deleting.value = false
  }
}

/* ── create user (field-level validation) ───────────────────────────────── */

const createOpen = ref(false)
const creating = ref(false)
const createForm = reactive({
  username: '',
  display_name: '',
  password: '',
  role: 'user',
})
const createErrors = reactive<{ username?: string; password?: string }>({})

const createDirty = computed(() =>
  Boolean(createForm.username || createForm.display_name || createForm.password),
)

/**
 * Field-level validation, owned by the page.
 *
 * Deliberately not the framework's `rules` + `form.validate()`: the page's
 * rules are two string checks, and a deterministic function is both easier to
 * test and immune to async-validator promise semantics. The messages still
 * render through the form item's `:error`, so the UI is unchanged.
 */
function validateCreate(): boolean {
  createErrors.username = undefined
  createErrors.password = undefined
  if (!createForm.username.trim()) createErrors.username = t('errUserRequired', ui.lang)
  else if (/\s/.test(createForm.username)) createErrors.username = t('usersErrUsernameChars', ui.lang)
  if (!createForm.password) createErrors.password = t('errPassRequired', ui.lang)
  else if (createForm.password.length < 8) createErrors.password = t('usersErrPassLen', ui.lang)
  return !createErrors.username && !createErrors.password
}

// An error clears as soon as its field is being fixed.
watch(
  () => createForm.username,
  () => {
    createErrors.username = undefined
  },
)
watch(
  () => createForm.password,
  () => {
    createErrors.password = undefined
  },
)

function openCreate() {
  resetCreateForm()
  createOpen.value = true
}

function resetCreateForm() {
  createForm.username = ''
  createForm.display_name = ''
  createForm.password = ''
  createForm.role = 'user'
  createErrors.username = undefined
  createErrors.password = undefined
}

async function submitCreate() {
  if (!validateCreate()) return
  creating.value = true
  try {
    await apiPost('/v1/admin/users', {
      username: createForm.username.trim(),
      password: createForm.password,
      display_name: createForm.display_name,
      role: createForm.role,
    })
    notifySuccess(t('userCreatedOk', ui.lang))
    createOpen.value = false
    resetCreateForm()
    await reload()
  } catch (e) {
    // A duplicate is a username problem — it belongs under the field, not in
    // a floating toast that has nothing to point at.
    if (e instanceof ApiError && e.status === 400 && /exist/i.test(e.message)) {
      createErrors.username = e.message
    } else {
      toastError(e)
    }
  } finally {
    creating.value = false
  }
}

/* ── one unsaved-changes guard for both the drawer and the create dialog ── */

const discardOpen = ref(false)
let discardTarget: 'drawer' | 'create' | null = null

function askDiscard(target: 'drawer' | 'create') {
  discardTarget = target
  discardOpen.value = true
}

function confirmDiscard() {
  discardOpen.value = false
  if (discardTarget === 'drawer') closeDrawerNow()
  else if (discardTarget === 'create') {
    createOpen.value = false
    resetCreateForm()
  }
  discardTarget = null
}

function beforeCreateClose(done: () => void) {
  if (!createDirty.value) return done()
  askDiscard('create')
}

function requestCreateClose() {
  beforeCreateClose(() => {
    createOpen.value = false
    resetCreateForm()
  })
}

/* ── detail drawer: staged profile + grants ─────────────────────────────── */

const drawerOpen = ref(false)
const drawerUser = ref<AdminUser | null>(null)
const saving = ref(false)
const tokensSectionEl = ref<HTMLElement | null>(null)
const draft = reactive({
  display_name: '',
  role: 'user',
  disabled: false,
  password: '',
  datasources: [] as string[],
})
const original = ref({
  display_name: '',
  role: 'user',
  disabled: false,
  datasources: [] as string[],
})

const grantOptions = computed(() => {
  const set = new Set([...knownDatasources.value, ...draft.datasources])
  return Array.from(set).sort()
})

const dirty = computed(() => {
  const base = original.value
  return (
    draft.display_name !== base.display_name ||
    draft.role !== base.role ||
    draft.disabled !== base.disabled ||
    draft.password !== '' ||
    !sameSet(draft.datasources, base.datasources)
  )
})

function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const set = new Set(b)
  return a.every((x) => set.has(x))
}

/** DataTable's row-click carries (row, index); the drawer only wants the row. */
function onRowClick(row: unknown) {
  openDrawer(row)
}

function openDrawer(row: unknown, focus: 'top' | 'tokens' = 'top') {
  const user = row as AdminUser
  drawerUser.value = user
  const grants = [...(user.datasources ?? [])]
  draft.display_name = user.display_name ?? ''
  draft.role = user.role || 'user'
  draft.disabled = Boolean(user.disabled)
  draft.password = ''
  draft.datasources = [...grants]
  original.value = {
    display_name: draft.display_name,
    role: draft.role,
    disabled: draft.disabled,
    datasources: [...grants],
  }
  tokenRaw.value = ''
  tokens.value = []
  activity.value = []
  Object.assign(tokenForm, { label: '', ttl: 0, scopes: '' })
  drawerOpen.value = true
  void loadTokens(user.id)
  void loadActivity(user.id)
  if (focus === 'tokens') {
    void Promise.resolve().then(() =>
      tokensSectionEl.value?.scrollIntoView?.({ block: 'start' }),
    )
  }
}

function closeDrawerNow() {
  drawerOpen.value = false
  drawerUser.value = null
}

function beforeDrawerClose(): boolean {
  if (!dirty.value) return true
  askDiscard('drawer')
  return false
}

function toggleGrant(name: string) {
  const idx = draft.datasources.indexOf(name)
  if (idx >= 0) draft.datasources.splice(idx, 1)
  else draft.datasources.push(name)
}

function discardDraft() {
  const base = original.value
  draft.display_name = base.display_name
  draft.role = base.role
  draft.disabled = base.disabled
  draft.password = ''
  draft.datasources = [...base.datasources]
}

async function saveDraft() {
  const user = drawerUser.value
  if (!user || !dirty.value) return
  const base = original.value
  const profile: Record<string, unknown> = {}
  if (draft.display_name !== base.display_name) profile.display_name = draft.display_name
  if (draft.role !== base.role) profile.role = draft.role
  if (draft.disabled !== base.disabled) profile.disabled = draft.disabled
  if (draft.password) profile.password = draft.password
  const grantsChanged = !sameSet(draft.datasources, base.datasources)

  saving.value = true
  try {
    if (Object.keys(profile).length) {
      await apiPatch(`/v1/admin/users/${user.id}`, profile)
    }
    if (grantsChanged) {
      await apiPut(`/v1/admin/users/${user.id}/datasources`, {
        datasources: draft.datasources,
      })
    }
    notifySuccess(t('userUpdatedOk', ui.lang))
    // The draft is now the truth — re-anchor so `dirty` goes clean.
    original.value = {
      display_name: draft.display_name,
      role: draft.role,
      disabled: draft.disabled,
      datasources: [...draft.datasources],
    }
    draft.password = ''
    await reload()
    const fresh = users.value.find((uu) => uu.id === user.id)
    if (fresh) {
      drawerUser.value = fresh
      original.value.datasources = [...(fresh.datasources ?? [])]
      draft.datasources = [...(fresh.datasources ?? [])]
    }
  } catch (e) {
    toastError(e)
  } finally {
    saving.value = false
  }
}

/* ── api tokens ─────────────────────────────────────────────────────────── */

const tokens = ref<TokenRow[]>([])
const tokenBusy = ref(false)
const tokenRaw = ref('')
const tokenCopied = ref(false)
const tokenForm = reactive({ label: '', ttl: 0, scopes: '' })
const tokenCountCache = new Map<number, number>()

async function loadTokens(userId: number) {
  tokenBusy.value = true
  try {
    const body = await apiGet(`/v1/admin/users/${userId}/tokens`)
    tokens.value = (body.tokens ?? []) as TokenRow[]
    // Cached as the *active* count: this cache feeds the delete confirmation,
    // whose wording promises tokens that stop working.
    tokenCountCache.set(userId, tokens.value.filter((tk) => !tk.revoked).length)
  } catch (e) {
    toastError(e)
  } finally {
    tokenBusy.value = false
  }
}

async function createToken() {
  const user = drawerUser.value
  if (!user) return
  tokenBusy.value = true
  tokenRaw.value = ''
  try {
    const body = await apiPost(`/v1/admin/users/${user.id}/tokens`, {
      label: tokenForm.label,
      ttl_hours: tokenForm.ttl > 0 ? tokenForm.ttl : undefined,
      scopes: tokenForm.scopes
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    })
    tokenRaw.value = body.token as string
    Object.assign(tokenForm, { label: '', ttl: 0, scopes: '' })
    notifySuccess(t('tokenCreatedOk', ui.lang))
    await loadTokens(user.id)
  } catch (e) {
    toastError(e)
  } finally {
    tokenBusy.value = false
  }
}

async function revokeToken(tk: TokenRow) {
  try {
    await apiDelete(`/v1/admin/tokens/${tk.id}`)
    notifySuccess(t('tokenRevokedOk', ui.lang))
    if (drawerUser.value) await loadTokens(drawerUser.value.id)
  } catch (e) {
    toastError(e)
  }
}

async function copyTokenRaw() {
  if (!tokenRaw.value) return
  const ok = await copyText(tokenRaw.value)
  if (!ok) {
    notifySuccess(t('copyFailed', ui.lang))
    return
  }
  tokenCopied.value = true
  window.setTimeout(() => (tokenCopied.value = false), 1600)
}

/* ── recent activity (audit, read-only, best-effort) ────────────────────── */

const activity = ref<ActivityRow[]>([])

async function loadActivity(userId: number) {
  try {
    const body = await apiGet(`/v1/admin/audit?user_id=${userId}&limit=5`)
    activity.value = (body.audit ?? []) as ActivityRow[]
  } catch {
    activity.value = []
  }
}
</script>

<style scoped>
.users-page {
  min-width: 0;
}

/* ── KPI row ── */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--sp-2);
}
@media (max-width: 900px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* ── toolbar ── */
.list-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.toolbar-search {
  width: 240px;
}
.filter-select {
  width: 150px;
}
.list-toolbar .spacer {
  flex: 1;
}

.bulk-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--accent);
  border-radius: var(--r-md);
  background: var(--accent-soft);
  color: var(--accent-active);
  font-size: var(--fs-xs);
}
.bulk-bar .spacer {
  flex: 1;
}
.bulk-count {
  font-variant-numeric: tabular-nums;
}

/* ── table cells ── */
.u-cell {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}
.u-meta {
  min-width: 0;
}
.u-name {
  display: block;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.u-sub {
  display: block;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  font-weight: 600;
}
.avatar.av-1 {
  background: var(--indigo-100);
  color: var(--accent-active);
}
.avatar.av-2 {
  background: var(--ok-bg);
  color: var(--ok);
}
.avatar.av-3 {
  background: var(--warn-bg);
  color: var(--warn);
}
.avatar.is-muted {
  background: var(--surface-muted);
  color: var(--text-secondary);
}

.tag-pill {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  border-radius: var(--r-full);
  padding: 1px 9px;
  font-size: var(--fs-2xs);
  white-space: nowrap;
  background: var(--surface-muted);
  color: var(--text-secondary);
}
.tag-pill.is-admin {
  background: var(--accent-soft);
  color: var(--accent-active);
}
.tag-pill.is-analyst {
  background: #e0f2fe;
  color: #0369a1;
}
.tag-pill.is-ok {
  background: var(--ok-bg);
  color: var(--ok);
}
.tag-pill.is-off {
  background: var(--surface-muted);
  color: var(--text-tertiary);
}
.tag-dot {
  width: 5px;
  height: 5px;
  border-radius: var(--r-full);
  background: currentColor;
}

.cell-time {
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.grant-chip {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  padding: 1px 9px;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.grant-chip.is-none {
  border-color: var(--warn-bg);
  background: var(--warn-bg);
  color: var(--warn);
}

/* Row actions stay out of the way until the row is under the pointer —
   but a keyboard user gets them on focus, and they are always in the
   accessibility tree (aria-label, not title-only). */
.row-actions {
  display: flex;
  gap: var(--sp-1);
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease);
}
:deep(.dt-row:hover) .row-actions,
:deep(.dt-row:focus-within) .row-actions,
.row-actions:focus-within {
  opacity: 1;
}
@media (hover: none) {
  .row-actions {
    opacity: 1;
  }
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: var(--r-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-secondary);
}
.icon-btn:not(:disabled):hover {
  background: var(--surface-hover);
  border-color: var(--border-subtle);
  color: var(--text-primary);
}
.icon-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}
.icon-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.icon-btn.is-danger {
  color: var(--danger);
}
.icon-btn.is-danger:not(:disabled):hover {
  background: var(--danger-bg);
  border-color: var(--danger-bg);
}

/* ── footer / pager ── */
.list-footer {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.list-footer .spacer {
  flex: 1;
}
.pager-size-label {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}
.pager-size {
  width: 76px;
}
.pager-page {
  min-width: 52px;
  text-align: center;
}

/* ── drawer ── */
.drawer-head {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  min-width: 0;
  flex: 1;
}
.drawer-head .avatar {
  width: 36px;
  height: 36px;
  font-size: var(--fs-xs);
}
.drawer-head .who {
  min-width: 0;
}
.drawer-head .name {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.drawer-head .handle {
  font-weight: 400;
  color: var(--text-tertiary);
}
.drawer-head .sub {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  margin-top: 3px;
  flex-wrap: wrap;
}

.dsec {
  padding: var(--sp-4) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.dsec:last-child {
  border-bottom: none;
}
.dsec h4 {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-2xs);
  font-weight: 500;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}
.dsec h4 .spacer {
  flex: 1;
}
.dsec h4 .h4-hint {
  text-transform: none;
  letter-spacing: 0;
  font-family: var(--font-sans);
}
.dirty-badge {
  text-transform: none;
  letter-spacing: 0;
  font-family: var(--font-sans);
  background: var(--warn-bg);
  color: var(--warn);
  border-radius: var(--r-full);
  padding: 0 8px;
  font-size: var(--fs-2xs);
}
.dsec-note {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
  line-height: var(--lh-relaxed);
}
.dsec-link {
  text-transform: none;
  letter-spacing: 0;
  font-family: var(--font-sans);
  font-size: var(--fs-2xs);
  color: var(--accent-active);
  text-decoration: none;
}
.dsec-link:hover {
  text-decoration: underline;
}
.dsec-link:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.profile-form :deep(.el-form-item) {
  margin-bottom: var(--sp-3);
}
.profile-select {
  width: 100%;
}

.ds-checks {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.ds-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  border: 1px solid var(--border-default);
  border-radius: var(--r-full);
  padding: 3px 11px;
  font-size: var(--fs-2xs);
  color: var(--text-primary);
  cursor: pointer;
}
.ds-check:hover {
  border-color: var(--accent);
}
.ds-check-input {
  width: 13px;
  height: 13px;
  accent-color: var(--accent);
  margin: 0;
}

.token-reveal {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-3);
}
.token-reveal-title {
  font-size: var(--fs-2xs);
  color: var(--warn);
  margin-bottom: var(--sp-1);
}
.token-reveal-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.token-reveal-code {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  overflow-wrap: anywhere;
}

.token-create {
  display: grid;
  grid-template-columns: 1.2fr 0.7fr 1fr auto;
  gap: var(--sp-2);
  align-items: center;
}

.token-list {
  margin-top: var(--sp-3);
}
.token-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) 0;
  border-bottom: 1px dashed var(--border-subtle);
}
.token-row:last-child {
  border-bottom: none;
}
.token-dot {
  width: 6px;
  height: 6px;
  border-radius: var(--r-full);
  flex: none;
}
.token-dot.is-on {
  background: var(--ok);
}
.token-dot.is-off {
  background: var(--text-tertiary);
}
.token-meta {
  flex: 1;
  min-width: 0;
}
.token-label {
  display: block;
  color: var(--text-primary);
  font-size: var(--fs-xs);
}
.token-sub {
  display: block;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}

.act-row {
  display: flex;
  gap: var(--sp-2);
  align-items: baseline;
  padding: 3px 0;
  font-size: var(--fs-xs);
}
.act-time {
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  font-size: var(--fs-2xs);
}
.act-action {
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.danger-zone {
  border: 1px solid var(--danger-bg);
  border-radius: var(--r-md);
  padding: var(--sp-3);
}
.dz-title {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--danger);
}
.dz-desc {
  margin: var(--sp-1) 0 var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  line-height: var(--lh-relaxed);
}

.confirm-names {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.impact-list {
  margin: 0;
  padding-left: 1.2em;
}
.impact-list li {
  margin-bottom: 3px;
}
</style>
