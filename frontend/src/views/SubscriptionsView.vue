<!--
  我的订阅(用户面)—— 定时报告的订阅列表 + 投递历史。

  口径:订阅是管理员在任务上建的,这里只让订阅者**看和管理自己那一份**
  (启停 / 模式 / 退订 / 看投递)。用户面端点严格 own-only:别人的 id
  一律 404,所以这里不做存在性预判,把 404 如实呈现(见 api/subscriptions.ts)。
-->
<template>
  <div class="chat-shell">
    <Sidebar />
    <div class="chat-main">
      <div class="subs-main">
        <div class="subs-page">
          <header class="subs-head">
            <div class="subs-head-text">
              <h1 class="subs-title">{{ t('subsMy', ui.lang) }}</h1>
              <p class="subs-desc">{{ t('subsPageDesc', ui.lang) }}</p>
            </div>
            <el-button class="subs-back" @click="backToChat">
              <MessageSquare :size="15" class="btn-icon" />
              {{ t('subsBackChat', ui.lang) }}
            </el-button>
          </header>

          <div class="subs-card">
            <el-table
              v-loading="loading"
              :data="rows"
              class="admin-table"
              max-height="var(--table-max-h)"
            >
              <template #empty>
                <div class="subs-empty">{{ t('subsMyEmpty', ui.lang) }}</div>
              </template>
              <el-table-column :label="t('subsJob', ui.lang)" min-width="220">
                <template #default="{ row }">
                  <div class="subs-job">{{ row.job_name || row.job_id }}</div>
                  <div v-if="row.job_name" class="subs-job-id">{{ row.job_id }}</div>
                </template>
              </el-table-column>
              <el-table-column :label="t('subsMode', ui.lang)" width="130">
                <template #default="{ row }">
                  <span class="pill" :class="modeClass(row.mode)">
                    {{ t(modeLabelKey(row.mode), ui.lang) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column :label="t('subsChannel', ui.lang)" min-width="200">
                <template #default="{ row }">
                  <span v-if="row.channel" class="cell-mono">{{ row.channel }}</span>
                  <span v-else class="dim">{{ t('subsChannelInherit', ui.lang) }}</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('subsEnabled', ui.lang)" width="90">
                <template #default="{ row }">
                  <el-switch
                    :model-value="row.enabled"
                    :loading="toggling === row.id"
                    @change="(v: boolean) => toggle(row, v)"
                  />
                </template>
              </el-table-column>
              <el-table-column
                :label="t('auditAction', ui.lang)"
                width="240"
                fixed="right"
              >
                <template #default="{ row }">
                  <el-button size="small" @click="showDeliveries(row)">
                    <History :size="14" class="btn-icon" />
                    {{ t('subsDeliveries', ui.lang) }}
                  </el-button>
                  <el-button size="small" type="danger" @click="unsubscribe(row)">
                    <Trash2 :size="14" class="btn-icon" />
                    {{ t('subsDeleteSelf', ui.lang) }}
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </div>

      <DetailDrawer
        v-model="deliveriesOpen"
        :title="deliveriesTitle"
        width="640px"
        :close-label="t('close', ui.lang)"
      >
        <el-table
          v-loading="deliveriesLoading"
          :data="deliveries"
          class="admin-table"
          max-height="60vh"
        >
          <template #empty>
            <div class="dim">{{ t('subsDeliveriesEmpty', ui.lang) }}</div>
          </template>
          <el-table-column :label="t('subsRun', ui.lang)" width="80">
            <template #default="{ row }">
              <span class="cell-mono">{{ row.run_id }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('subsChannel', ui.lang)" min-width="140">
            <template #default="{ row }">
              <span class="cell-mono">{{ row.channel }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('jobStatus', ui.lang)" width="100">
            <template #default="{ row }">
              <span class="pill" :class="deliveryStatusClass(row.status)">
                {{ row.status === 'failed'
                  ? t('subsDeliveryFailed', ui.lang)
                  : t('subsDeliverySent', ui.lang) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('subsExcerpt', ui.lang)" min-width="220">
            <template #default="{ row }">
              <div class="subs-excerpt" :title="row.excerpt">{{ row.excerpt }}</div>
              <div v-if="row.error" class="subs-excerpt-err">{{ row.error }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('subsDeliveredAt', ui.lang)" width="170">
            <template #default="{ row }">
              <span class="cell-mono">{{ fmtDateTime(row.created_at) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </DetailDrawer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { History, MessageSquare, Trash2 } from 'lucide-vue-next'
import Sidebar from '../components/layout/Sidebar.vue'
import DetailDrawer from '../components/base/DetailDrawer.vue'
import { useUiStore } from '../stores/ui'
import { useRouter } from 'vue-router'
import { t } from '../i18n'
import { notifySuccess, toastError } from '../utils/notify'
import { fmtDateTime } from '../utils/format'
import {
  deleteMySubscription,
  deliveryStatusClass,
  fetchMyDeliveries,
  fetchMySubscriptions,
  modeClass,
  modeLabelKey,
  patchMySubscription,
  type SubscriptionDelivery,
  type SubscriptionRow,
} from '../api/subscriptions'

const ui = useUiStore()
const router = useRouter()

const rows = ref<SubscriptionRow[]>([])
const loading = ref(false)
const toggling = ref('')

const deliveriesOpen = ref(false)
const deliveries = ref<SubscriptionDelivery[]>([])
const deliveriesLoading = ref(false)
const deliveriesFor = ref<SubscriptionRow | null>(null)

// 抽屉标题带上任务名:抽屉是盖在列表上的,标题说清"这是哪一条的投递"。
const deliveriesTitle = computed(() => {
  const job = deliveriesFor.value?.job_name || deliveriesFor.value?.job_id || ''
  return job ? `${t('subsDeliveries', ui.lang)} · ${job}` : t('subsDeliveries', ui.lang)
})

async function load() {
  loading.value = true
  try {
    const body = await fetchMySubscriptions()
    rows.value = body.subscriptions ?? []
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

function backToChat() {
  void router.push('/')
}

async function toggle(row: SubscriptionRow, enabled: boolean) {
  toggling.value = row.id
  try {
    await patchMySubscription(row.id, { enabled })
    await load()
  } catch (e) {
    toastError(e)
  } finally {
    toggling.value = ''
  }
}

async function showDeliveries(row: SubscriptionRow) {
  deliveriesFor.value = row
  deliveriesOpen.value = true
  deliveriesLoading.value = true
  deliveries.value = []
  try {
    const body = await fetchMyDeliveries(row.id)
    deliveries.value = body.deliveries ?? []
  } catch (e) {
    toastError(e)
  } finally {
    deliveriesLoading.value = false
  }
}

async function unsubscribe(row: SubscriptionRow) {
  try {
    await ElMessageBox.confirm(
      t('subsConfirmUnsub', ui.lang),
      t('subsDeleteSelf', ui.lang),
      {
        type: 'warning',
        confirmButtonText: t('subsDeleteSelf', ui.lang),
        cancelButtonText: t('cancel', ui.lang),
      },
    )
  } catch {
    return
  }
  try {
    await deleteMySubscription(row.id)
    notifySuccess(t('subsUnsubscribed', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

onMounted(load)
</script>

<style scoped>
.subs-main {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  background: var(--chat-bg);
}

.subs-page {
  max-width: 1040px;
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-6) var(--sp-8);
}

.subs-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  margin-bottom: var(--sp-5);
}

.subs-title {
  margin: 0 0 var(--sp-1);
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--text-primary);
}

.subs-desc {
  margin: 0;
  max-width: 60ch;
  font-size: var(--fs-sm);
  line-height: var(--lh-normal);
  color: var(--text-secondary);
}

.subs-back {
  flex: none;
}

.subs-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  overflow: hidden;
}

.subs-empty {
  padding: var(--sp-6) var(--sp-4);
  color: var(--text-tertiary);
}

.subs-job {
  font-weight: 500;
  color: var(--text-primary);
}

.subs-job-id {
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.subs-excerpt {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-line;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.subs-excerpt-err {
  margin-top: 2px;
  font-size: var(--fs-2xs);
  color: var(--danger-text);
}
</style>
