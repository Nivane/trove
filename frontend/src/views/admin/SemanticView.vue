<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('semanticLayer', ui.lang) }}</h2>
        <p class="view-desc">{{ t('semanticPageDesc', ui.lang) }}</p>
      </div>
    </header>

    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-icon"><Gauge :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('semMetrics', ui.lang) }}</span>
          <span class="stat-value">{{ metrics.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><Table2 :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('semDatasets', ui.lang) }}</span>
          <span class="stat-value">{{ datasets.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon"><Columns3 :size="18" /></span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('semFields', ui.lang) }}</span>
          <span class="stat-value">{{ fields.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <span class="stat-icon" :class="pending.length ? 'warn' : 'ok'">
          <Inbox :size="18" />
        </span>
        <div class="stat-meta">
          <span class="stat-label">{{ t('semPending', ui.lang) }}</span>
          <span class="stat-value">{{ pending.length }}</span>
        </div>
      </div>
    </div>

    <div class="admin-card kb-summary">
      <div class="card-header">
        <div class="card-title">{{ t('semanticLayer', ui.lang) }}</div>
        <div class="card-actions">
          <el-select
            v-model="ds"
            class="ds-select"
            :placeholder="t('kbSelectDs', ui.lang)"
            @change="loadDetail"
          >
            <el-option
              v-for="d in connected"
              :key="d.name"
              :value="d.name"
              :label="d.name"
            >
              <span>{{ d.name }}</span>
              <span v-if="d.default" class="cell-muted"> · default</span>
            </el-option>
          </el-select>
          <el-button class="refresh-btn" :loading="loading" @click="loadAll">
            <RefreshCw :size="15" class="btn-icon" />
            {{ t('refresh', ui.lang) }}
          </el-button>
        </div>
      </div>

      <div class="kb-stat-row">
        <span class="pill" :class="enabled ? 'pill-ok' : 'pill-warn'">
          <span class="pill-dot" :class="enabled ? 'pill-dot-ok' : ''" />
          {{
            enabled
              ? t('dsKbReady', ui.lang)
              : t('semNotEnabled', ui.lang)
          }}
        </span>
        <span v-if="enabled && issues.length" class="pill pill-danger">
          {{ t('semIssues', ui.lang) }} · {{ issues.length }}
        </span>
        <span v-else-if="enabled" class="pill pill-ok">
          {{ t('semNoIssues', ui.lang) }}
        </span>
      </div>
      <ul v-if="enabled && issues.length" class="issue-list">
        <li v-for="(issue, i) in issues" :key="i" class="issue-item">
          <AlertCircle :size="13" />
          <span>{{ issue }}</span>
        </li>
      </ul>
      <div v-if="!enabled" class="empty-note">
        {{ t('semNotEnabled', ui.lang) }}
      </div>
    </div>

    <template v-if="enabled">
      <div class="admin-tabs">
        <el-tabs v-model="tab">
          <el-tab-pane :name="'metrics'">
            <template #label>
              <span class="tab-label">
                {{ t('semMetrics', ui.lang) }}
                <span v-if="metrics.length" class="tab-badge">{{
                  metrics.length
                }}</span>
              </span>
            </template>
            <div class="admin-card">
              <div class="card-toolbar">
                <span class="view-count">
                  {{ metrics.length }} · {{ t('semMetrics', ui.lang) }}
                </span>
                <span class="spacer" />
                <el-button type="primary" @click="openMetric">
                  <Plus :size="15" class="btn-icon" />
                  {{ t('semAddMetric', ui.lang) }}
                </el-button>
              </div>
              <el-table
                v-loading="loading"
                :data="metrics"
                class="admin-table"
                max-height="calc(100vh - 420px)"
              >
                <template #empty>
                  <TableEmpty>{{ t('semNoMetrics', ui.lang) }}</TableEmpty>
                </template>
                <el-table-column :label="t('semName', ui.lang)" min-width="150">
                  <template #default="{ row }">
                    <span class="lesson-title">{{ row.name }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="expression" min-width="200">
                  <template #default="{ row }">
                    <code class="mapping-code">{{ row.expression }}</code>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semSynonyms', ui.lang)" min-width="150">
                  <template #default="{ row }">
                    <span
                      v-for="s in row.synonyms || []"
                      :key="s"
                      class="kb-chip"
                      >{{ s }}</span>
                    <span v-if="!(row.synonyms || []).length" class="cell-muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semTables', ui.lang)" min-width="150">
                  <template #default="{ row }">
                    <span
                      v-for="d in row.datasets || []"
                      :key="d"
                      class="kb-chip"
                      >{{ d }}</span>
                    <span v-if="!(row.datasets || []).length" class="cell-muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semDefinition', ui.lang)" min-width="180">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ row.definition || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('actions', ui.lang)"
                  width="70"
                  fixed="right"
                >
                  <template #default="{ row }">
                    <div class="row-actions">
                      <button
                        class="mini-btn icon edit"
                        :title="t('semEdit', ui.lang)"
                        :disabled="acting"
                        @click="openMetric(row)"
                      >
                        <Pencil :size="13" />
                      </button>
                      <button
                        class="mini-btn icon is-danger"
                        :title="t('semDelete', ui.lang)"
                        :disabled="acting"
                        @click="deleteMetric(row)"
                      >
                        <Trash2 :size="13" />
                      </button>
                    </div>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-tab-pane>

          <el-tab-pane :name="'datasets'">
            <template #label>
              <span class="tab-label">
                {{ t('semDatasets', ui.lang) }}
                <span v-if="datasets.length" class="tab-badge">{{
                  datasets.length
                }}</span>
              </span>
            </template>
            <div class="admin-card">
              <div class="card-toolbar">
                <span class="view-count">
                  {{ datasets.length }} · {{ t('semDatasets', ui.lang) }}
                </span>
                <span class="spacer" />
                <el-button type="primary" @click="openDataset">
                  <Plus :size="15" class="btn-icon" />
                  {{ t('semAddDataset', ui.lang) }}
                </el-button>
              </div>
              <el-table
                v-loading="loading"
                :data="datasets"
                class="admin-table"
                max-height="calc(100vh - 420px)"
              >
                <template #empty>
                  <TableEmpty>{{ t('semNoDatasets', ui.lang) }}</TableEmpty>
                </template>
                <el-table-column :label="t('semName', ui.lang)" min-width="140">
                  <template #default="{ row }">
                    <span class="lesson-title">{{ row.name }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semSource', ui.lang)" min-width="120">
                  <template #default="{ row }">
                    <span class="cell-mono">{{ row.source || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semPrimaryKey', ui.lang)" min-width="120">
                  <template #default="{ row }">
                    <span
                      v-for="k in row.primary_key || []"
                      :key="k"
                      class="kb-chip"
                      >{{ k }}</span>
                    <span v-if="!(row.primary_key || []).length" class="cell-muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semFieldsCount', ui.lang)" width="90">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ (row.fields || []).length }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semDefinition', ui.lang)" min-width="180">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ row.description || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('actions', ui.lang)"
                  width="70"
                  fixed="right"
                >
                  <template #default="{ row }">
                    <div class="row-actions">
                      <button
                        class="mini-btn icon edit"
                        :title="t('semEdit', ui.lang)"
                        :disabled="acting"
                        @click="openDataset(row)"
                      >
                        <Pencil :size="13" />
                      </button>
                      <button
                        class="mini-btn icon is-danger"
                        :title="t('semDelete', ui.lang)"
                        :disabled="acting"
                        @click="deleteDataset(row)"
                      >
                        <Trash2 :size="13" />
                      </button>
                    </div>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-tab-pane>

          <el-tab-pane :name="'fields'">
            <template #label>
              <span class="tab-label">
                {{ t('semFields', ui.lang) }}
                <span v-if="fields.length" class="tab-badge">{{
                  fields.length
                }}</span>
              </span>
            </template>
            <div class="admin-card">
              <div class="card-toolbar">
                <span class="view-count">
                  {{ fields.length }} · {{ t('semFields', ui.lang) }}
                </span>
                <span class="spacer" />
                <el-button type="primary" @click="openField">
                  <Plus :size="15" class="btn-icon" />
                  {{ t('semAddField', ui.lang) }}
                </el-button>
              </div>
              <el-table
                v-loading="loading"
                :data="fields"
                class="admin-table"
                max-height="calc(100vh - 420px)"
              >
                <template #empty>
                  <TableEmpty>{{ t('semNoFields', ui.lang) }}</TableEmpty>
                </template>
                <el-table-column :label="t('semDatasetForField', ui.lang)" min-width="120">
                  <template #default="{ row }">
                    <span class="kb-chip">{{ row.dataset }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semName', ui.lang)" min-width="130">
                  <template #default="{ row }">
                    <span class="lesson-title">{{ row.name }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="expression" min-width="130">
                  <template #default="{ row }">
                    <code class="mapping-code">{{ row.expression }}</code>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semDatatype', ui.lang)" width="100">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ row.datatype || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semRole', ui.lang)" width="100">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ roleLabel(row.semantic_role) }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semSynonyms', ui.lang)" min-width="140">
                  <template #default="{ row }">
                    <span
                      v-for="s in row.synonyms || []"
                      :key="s"
                      class="kb-chip"
                      >{{ s }}</span>
                    <span v-if="!(row.synonyms || []).length" class="cell-muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('actions', ui.lang)"
                  width="70"
                  fixed="right"
                >
                  <template #default="{ row }">
                    <div class="row-actions">
                      <button
                        class="mini-btn icon edit"
                        :title="t('semEdit', ui.lang)"
                        :disabled="acting"
                        @click="openField(row)"
                      >
                        <Pencil :size="13" />
                      </button>
                      <button
                        class="mini-btn icon is-danger"
                        :title="t('semDelete', ui.lang)"
                        :disabled="acting"
                        @click="deleteField(row)"
                      >
                        <Trash2 :size="13" />
                      </button>
                    </div>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-tab-pane>

          <el-tab-pane :name="'pending'">
            <template #label>
              <span class="tab-label">
                {{ t('semPending', ui.lang) }}
                <span v-if="pending.length" class="tab-badge">{{
                  pending.length
                }}</span>
              </span>
            </template>
            <div class="admin-card">
              <div class="card-toolbar">
                <span class="view-count">
                  {{ pending.length }} {{ t('semPending', ui.lang) }} ·
                  {{ applied.length }} {{ t('semApplied', ui.lang) }} ·
                  {{ rejected.length }} {{ t('semRejected', ui.lang) }}
                </span>
                <span class="spacer" />
                <el-button
                  v-if="pending.length"
                  :loading="acting"
                  @click="confirmAll"
                >
                  {{ t('kbConfirmAll', ui.lang) }}
                </el-button>
              </div>
              <el-table
                v-loading="loading"
                :data="pending"
                class="admin-table"
                max-height="calc(100vh - 420px)"
              >
                <template #empty>
                  <TableEmpty>{{ t('semNoPending', ui.lang) }}</TableEmpty>
                </template>
                <el-table-column :label="t('semKindLabel', ui.lang)" width="120">
                  <template #default="{ row }">
                    <span class="kb-chip">{{ kindLabel(row) }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semUpsert', ui.lang)" width="130">
                  <template #default="{ row }">
                    <span
                      class="pill"
                      :class="row.action === 'delete' ? 'pill-danger' : 'pill-ok'"
                    >
                      {{ row.action }}
                    </span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semName', ui.lang)" min-width="180">
                  <template #default="{ row }">
                    <span class="lesson-title">{{ row.name }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="note" min-width="200">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ row.note || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('semCreatedAt', ui.lang)" width="160">
                  <template #default="{ row }">
                    <span class="cell-muted">{{ row.created_at || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('actions', ui.lang)"
                  width="90"
                  fixed="right"
                >
                  <template #default="{ row }">
                    <div class="row-actions">
                      <button
                        class="mini-btn icon primary"
                        :title="t('semConfirmDraft', ui.lang)"
                        :disabled="acting"
                        @click="confirmDraft(row)"
                      >
                        <Check :size="13" />
                      </button>
                      <button
                        class="mini-btn icon is-danger"
                        :title="t('semRejectDraft', ui.lang)"
                        :disabled="acting"
                        @click="rejectDraft(row)"
                      >
                        <X :size="13" />
                      </button>
                    </div>
                  </template>
                </el-table-column>
              </el-table>
              <div v-if="applied.length || rejected.length" class="history-block">
                <div class="history-title">{{ t('semHistory', ui.lang) }}</div>
                <el-table
                  :data="[...applied, ...rejected]"
                  class="admin-table"
                  max-height="300"
                >
                  <el-table-column :label="t('semKindLabel', ui.lang)" width="110">
                    <template #default="{ row }">
                      <span class="kb-chip">{{ kindLabel(row) }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('semName', ui.lang)" min-width="170">
                    <template #default="{ row }">
                      <span class="lesson-title">{{ row.name }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('status', ui.lang)" width="100">
                    <template #default="{ row }">
                      <span
                        class="pill"
                        :class="
                          row.status === 'applied' ? 'pill-ok' : 'pill-danger'
                        "
                      >
                        {{
                          row.status === 'applied'
                            ? t('semDraftConfirmed', ui.lang)
                            : t('semDraftRejected', ui.lang)
                        }}
                      </span>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('semCreatedAt', ui.lang)" width="160">
                    <template #default="{ row }">
                      <span class="cell-muted">{{ row.created_at || '—' }}</span>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </template>

    <!-- 指标 新增/编辑对话框 -->
    <el-dialog
      v-model="metricOpen"
      :title="`${t('semMetrics', ui.lang)} · ${metricForm.name || t('semAddMetric', ui.lang)}`"
      width="560"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveMetric">
        <el-form-item :label="t('semName', ui.lang)">
          <el-input
            v-model="metricForm.name"
            :disabled="!!metricEditName"
            :placeholder="t('semName', ui.lang)"
          />
        </el-form-item>
        <el-form-item :label="t('semExpression', ui.lang)">
          <el-input
            v-model="metricForm.expression"
            class="mono-input"
            :placeholder="t('semExpressionHint', ui.lang)"
            spellcheck="false"
          />
          <div class="form-hint">{{ t('semExpressionHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item
          :label="`${t('semSynonyms', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="metricForm.synonyms" placeholder="avg, average" />
        </el-form-item>
        <el-form-item
          :label="`${t('semTables', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="metricForm.tables" :placeholder="t('semTables', ui.lang)" />
        </el-form-item>
        <el-form-item
          :label="`${t('semDefinition', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="metricForm.definition" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item :label="`${t('semNote', ui.lang)} · ${t('kbOptional', ui.lang)}`">
          <el-input v-model="metricForm.note" :placeholder="t('semNoteHint', ui.lang)" />
        </el-form-item>
        <div v-if="metricError" class="form-error">
          <span>{{ metricError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="metricOpen = false">
          {{ t('cancel', ui.lang) }}
        </el-button>
        <el-button type="primary" :loading="acting" @click="saveMetric">
          {{ t('semSaveDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 字段 新增/编辑对话框 -->
    <el-dialog
      v-model="fieldOpen"
      :title="`${t('semFields', ui.lang)} · ${fieldForm.name || t('semAddField', ui.lang)}`"
      width="560"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveField">
        <el-form-item :label="t('semDatasetForField', ui.lang)">
          <el-select
            v-model="fieldForm.dataset"
            class="ds-type-select"
            :disabled="!!fieldEditKey"
          >
            <el-option
              v-for="d in datasetNames"
              :key="d"
              :value="d"
              :label="d"
            />
          </el-select>
          <div class="form-hint">{{ t('semFieldTargetHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item :label="t('semName', ui.lang)">
          <el-input
            v-model="fieldForm.name"
            :disabled="!!fieldEditKey"
            :placeholder="'grade'"
          />
        </el-form-item>
        <el-form-item :label="t('semExpression', ui.lang)">
          <el-input
            v-model="fieldForm.expression"
            class="mono-input"
            :placeholder="t('semExpressionHint', ui.lang)"
            spellcheck="false"
          />
        </el-form-item>
        <div class="form-row">
          <el-form-item :label="t('semDatatype', ui.lang)" class="form-col">
            <el-input v-model="fieldForm.datatype" placeholder="Integer" />
          </el-form-item>
          <el-form-item :label="t('semRole', ui.lang)" class="form-col">
            <el-select v-model="fieldForm.semantic_role" clearable class="role-select">
              <el-option
                v-for="r in roleOptions"
                :key="r.value"
                :label="r.label"
                :value="r.value"
              />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item
          :label="`${t('semSynonyms', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="fieldForm.synonyms" />
        </el-form-item>
        <el-form-item
          :label="`${t('semDefinition', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="fieldForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="fieldForm.is_time">
            {{ t('semIsTime', ui.lang) }}
          </el-checkbox>
        </el-form-item>
        <div v-if="fieldError" class="form-error">
          <span>{{ fieldError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="fieldOpen = false">
          {{ t('cancel', ui.lang) }}
        </el-button>
        <el-button type="primary" :loading="acting" @click="saveField">
          {{ t('semSaveDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 数据集 新增/编辑对话框 -->
    <el-dialog
      v-model="datasetOpen"
      :title="`${t('semDatasets', ui.lang)} · ${datasetForm.name || t('semAddDataset', ui.lang)}`"
      width="520"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveDataset">
        <el-form-item :label="t('semName', ui.lang)">
          <el-input
            v-model="datasetForm.name"
            :disabled="!!datasetEditName"
            placeholder="students"
          />
        </el-form-item>
        <el-form-item :label="t('semSource', ui.lang)">
          <el-input v-model="datasetForm.source" placeholder="students" />
        </el-form-item>
        <el-form-item
          :label="`${t('semPrimaryKey', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="datasetForm.primary_key" placeholder="id" />
        </el-form-item>
        <el-form-item
          :label="`${t('semSynonyms', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="datasetForm.synonyms" />
        </el-form-item>
        <el-form-item
          :label="`${t('semDefinition', ui.lang)} · ${t('kbOptional', ui.lang)}`"
        >
          <el-input v-model="datasetForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <div v-if="datasetError" class="form-error">
          <span>{{ datasetError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="datasetOpen = false">
          {{ t('cancel', ui.lang) }}
        </el-button>
        <el-button type="primary" :loading="acting" @click="saveDataset">
          {{ t('semSaveDraft', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Plus,
  RefreshCw,
  Gauge,
  Table2,
  Columns3,
  Inbox,
  AlertCircle,
  Pencil,
  Trash2,
  Check,
  X,
} from 'lucide-vue-next'
import { ElMessageBox } from 'element-plus'
import { apiGet, apiPost } from '../../api/http'
import type { DatasourceInfo } from '../../api/types'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { toastError, notifySuccess } from '../../utils/notify'
import TableEmpty from '../../components/admin/TableEmpty.vue'

interface SemanticField {
  name: string
  expression: string
  datatype?: string
  is_time?: boolean
  description?: string
  synonyms?: string[]
  semantic_role?: string
  enum_display?: Record<string, string>
}
interface SemanticDataset {
  name: string
  source: string
  primary_key: string[]
  description?: string
  synonyms?: string[]
  fields: SemanticField[]
}
interface SemanticMetric {
  name: string
  expression: string
  synonyms?: string[]
  datasets?: string[]
  definition?: string
}
interface SemanticModel {
  name: string
  description: string
  instructions: string
  metrics: SemanticMetric[]
  datasets: SemanticDataset[]
  relationships: unknown[]
}
interface SemanticDraft {
  id: string
  kind: 'metric' | 'field' | 'dataset'
  action: 'upsert' | 'delete'
  name: string
  note: string
  status: 'pending' | 'applied' | 'rejected'
  created_at: string
}
interface SemanticDetail {
  enabled: boolean
  model: SemanticModel | null
  issues: string[]
  drafts: {
    pending: SemanticDraft[]
    applied: SemanticDraft[]
    rejected: SemanticDraft[]
  }
}

const ui = useUiStore()
const tab = ref('metrics')
const ds = ref('')
const datasources = ref<DatasourceInfo[]>([])
const detail = ref<SemanticDetail | null>(null)
const loading = ref(false)
const acting = ref(false)

const connected = computed(() =>
  datasources.value.filter((d) => d.status === 'connected'),
)
const enabled = computed(() => !!detail.value?.enabled)
const issues = computed(() => detail.value?.issues || [])
const model = computed(() => detail.value?.model || null)
const metrics = computed(() => model.value?.metrics || [])
const datasets = computed(() => model.value?.datasets || [])
const fields = computed(() =>
  (model.value?.datasets || []).flatMap((d) =>
    (d.fields || []).map((f) => ({ ...f, dataset: d.name })),
  ),
)
const pending = computed(() => detail.value?.drafts.pending || [])
const applied = computed(() => detail.value?.drafts.applied || [])
const rejected = computed(() => detail.value?.drafts.rejected || [])
const datasetNames = computed(() => datasets.value.map((d) => d.name))

const ROLE_KEYS = {
  identifier: 'semRoleIdentifier',
  measure: 'semRoleMeasure',
  dimension: 'semRoleDimension',
  enum: 'semRoleEnum',
  time: 'semRoleTime',
} as const
const KIND_KEYS = {
  metric: 'semMetric',
  field: 'semField',
  dataset: 'semDataset',
} as const

const roleOptions = Object.entries(ROLE_KEYS).map(([value, key]) => ({
  value,
  label: t(key, ui.lang),
}))
function roleLabel(role: string): string {
  if (!role) return '—'
  const key = ROLE_KEYS[role as keyof typeof ROLE_KEYS]
  return key ? t(key, ui.lang) : role
}
function kindLabel(draft: SemanticDraft): string {
  return t(KIND_KEYS[draft.kind], ui.lang)
}
function splitCsv(v: string): string[] {
  return v
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

async function loadDatasources() {
  const body = await apiGet('/v1/admin/datasources')
  datasources.value = body.datasources ?? []
  if (!ds.value && connected.value.length) {
    const dflt = connected.value.find((d) => d.default)
    ds.value = dflt ? dflt.name : connected.value[0].name
  }
}

async function loadDetail() {
  if (!ds.value) return
  loading.value = true
  try {
    const body = await apiGet<{ semantic: SemanticDetail }>(
      `/v1/admin/semantic/${encodeURIComponent(ds.value)}`,
    )
    detail.value = body.semantic
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  loading.value = true
  try {
    await loadDatasources()
    await loadDetail()
  } finally {
    loading.value = false
  }
}

// ── 草稿 API ──
async function createDraft(
  kind: 'metric' | 'field' | 'dataset',
  action: 'upsert' | 'delete',
  name: string,
  payload?: Record<string, unknown>,
  note?: string,
): Promise<boolean> {
  acting.value = true
  try {
    await apiPost(
      `/v1/admin/semantic/${encodeURIComponent(ds.value)}/drafts`,
      { kind, action, name, payload, note: note || '' },
    )
    notifySuccess(t('semDraftCreated', ui.lang))
    await loadDetail()
    return true
  } catch (e) {
    toastError(e)
    return false
  } finally {
    acting.value = false
  }
}

async function confirmDraft(row: SemanticDraft) {
  acting.value = true
  try {
    await apiPost(
      `/v1/admin/semantic/${encodeURIComponent(ds.value)}/drafts/${row.id}/confirm`,
    )
    notifySuccess(t('semDraftConfirmed', ui.lang))
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}
async function rejectDraft(row: SemanticDraft) {
  acting.value = true
  try {
    await apiPost(
      `/v1/admin/semantic/${encodeURIComponent(ds.value)}/drafts/${row.id}/reject`,
    )
    notifySuccess(t('semDraftRejected', ui.lang))
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}
async function confirmAll() {
  acting.value = true
  try {
    for (const row of pending.value) {
      await apiPost(
        `/v1/admin/semantic/${encodeURIComponent(ds.value)}/drafts/${row.id}/confirm`,
      )
    }
    notifySuccess(t('kbConfirmAllDone', ui.lang, pending.value.length))
    await loadDetail()
  } catch (e) {
    toastError(e)
  } finally {
    acting.value = false
  }
}

// ── 指标对话框 ──
const metricOpen = ref(false)
const metricError = ref('')
const metricEditName = ref('')
const metricForm = reactive({
  name: '',
  expression: '',
  synonyms: '',
  tables: '',
  definition: '',
  note: '',
})
function openMetric(row?: SemanticMetric) {
  metricError.value = ''
  metricEditName.value = row?.name || ''
  Object.assign(metricForm, {
    name: row?.name || '',
    expression: row?.expression || '',
    synonyms: (row?.synonyms || []).join(', '),
    tables: (row?.datasets || []).join(', '),
    definition: row?.definition || '',
    note: '',
  })
  metricOpen.value = true
}
async function saveMetric() {
  const f = metricForm
  if (!f.name.trim()) {
    metricError.value = t('semNameRequired', ui.lang)
    return
  }
  if (!f.expression.trim()) {
    metricError.value = t('semExpressionRequired', ui.lang)
    return
  }
  metricError.value = ''
  const ok = await createDraft(
    'metric',
    'upsert',
    f.name.trim(),
    {
      expression: f.expression.trim(),
      synonyms: splitCsv(f.synonyms),
      datasets: splitCsv(f.tables),
      definition: f.definition.trim(),
    },
    f.note.trim(),
  )
  if (ok) metricOpen.value = false
}
async function deleteMetric(row: SemanticMetric) {
  try {
    await ElMessageBox.confirm(t('semDeleteConfirm', ui.lang), 'Confirm', {
      type: 'warning',
    })
  } catch {
    return
  }
  await createDraft('metric', 'delete', row.name)
}

// ── 字段对话框 ──
const fieldOpen = ref(false)
const fieldError = ref('')
const fieldEditKey = ref('')
const fieldForm = reactive({
  dataset: '',
  name: '',
  expression: '',
  datatype: '',
  semantic_role: '',
  synonyms: '',
  description: '',
  is_time: false,
  note: '',
})
function openField(row?: { dataset: string } & SemanticField) {
  fieldError.value = ''
  fieldEditKey.value = row ? `${row.dataset}.${row.name}` : ''
  Object.assign(fieldForm, {
    dataset: row?.dataset || datasetNames.value[0] || '',
    name: row?.name || '',
    expression: row?.expression || '',
    datatype: row?.datatype || '',
    semantic_role: row?.semantic_role || '',
    synonyms: (row?.synonyms || []).join(', '),
    description: row?.description || '',
    is_time: !!row?.is_time,
    note: '',
  })
  fieldOpen.value = true
}
async function saveField() {
  const f = fieldForm
  if (!f.dataset || !f.name.trim()) {
    fieldError.value = t('semNameRequired', ui.lang)
    return
  }
  if (!f.expression.trim()) {
    fieldError.value = t('semExpressionRequired', ui.lang)
    return
  }
  fieldError.value = ''
  const ok = await createDraft(
    'field',
    'upsert',
    `${f.dataset}.${f.name.trim()}`,
    {
      expression: f.expression.trim(),
      datatype: f.datatype.trim(),
      semantic_role: f.semantic_role,
      synonyms: splitCsv(f.synonyms),
      description: f.description.trim(),
      is_time: f.is_time,
    },
    f.note.trim(),
  )
  if (ok) fieldOpen.value = false
}
async function deleteField(row: { dataset: string } & SemanticField) {
  try {
    await ElMessageBox.confirm(t('semDeleteConfirm', ui.lang), 'Confirm', {
      type: 'warning',
    })
  } catch {
    return
  }
  await createDraft('field', 'delete', `${row.dataset}.${row.name}`)
}

// ── 数据集对话框 ──
const datasetOpen = ref(false)
const datasetError = ref('')
const datasetEditName = ref('')
const datasetForm = reactive({
  name: '',
  source: '',
  primary_key: '',
  synonyms: '',
  description: '',
  note: '',
})
function openDataset(row?: SemanticDataset) {
  datasetError.value = ''
  datasetEditName.value = row?.name || ''
  Object.assign(datasetForm, {
    name: row?.name || '',
    source: row?.source || '',
    primary_key: (row?.primary_key || []).join(', '),
    synonyms: (row?.synonyms || []).join(', '),
    description: row?.description || '',
    note: '',
  })
  datasetOpen.value = true
}
async function saveDataset() {
  const f = datasetForm
  if (!f.name.trim()) {
    datasetError.value = t('semNameRequired', ui.lang)
    return
  }
  datasetError.value = ''
  const ok = await createDraft(
    'dataset',
    'upsert',
    f.name.trim(),
    {
      source: f.source.trim() || f.name.trim(),
      primary_key: splitCsv(f.primary_key),
      synonyms: splitCsv(f.synonyms),
      description: f.description.trim(),
    },
    f.note.trim(),
  )
  if (ok) datasetOpen.value = false
}
async function deleteDataset(row: SemanticDataset) {
  try {
    await ElMessageBox.confirm(t('semDeleteConfirm', ui.lang), 'Confirm', {
      type: 'warning',
    })
  } catch {
    return
  }
  await createDraft('dataset', 'delete', row.name)
}

onMounted(loadAll)
</script>
