<!--
  覆盖率体检（逐题编译级回放）。

  页面只做三件事：把一批真实问题交给后端、如实展示两套覆盖率口径、把缺口
  转成**待确认**草稿。口径一个都不在这里算 —— `num/den/rate` 全部原样渲染，
  `rate === null` 显示「无样本 · 无法计算」而不是 0%（后端 ratio() 的纪律）。

  成本确认是显式的：preflight 展示解析出的模型与调用量；没有勾选就禁开始。
  轮询只在 queued/running 时存在，用 generation token 丢弃过期响应。
-->
<template>
  <div class="admin-view">
    <PageHeader
      :title="t('qcTitle', ui.lang)"
      :description="t('qcDesc', ui.lang)"
      :breadcrumbs="crumbs"
    >
      <template #actions>
        <el-button :loading="datasourcesLoading" @click="loadDatasources">
          <RefreshCw :size="14" />
        </el-button>
      </template>
    </PageHeader>

    <!-- 数据源 -->
    <div class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('qcDatasource', ui.lang) }}</div>
        <div class="card-actions">
          <el-select
            v-model="values.ds"
            class="filter-select qc-ds-select"
            :placeholder="t('qcPickDatasource', ui.lang)"
            :aria-label="t('qcDatasource', ui.lang)"
          >
            <el-option
              v-for="d in datasources"
              :key="d.name"
              :label="d.name"
              :value="d.name"
            />
          </el-select>
        </div>
      </div>
    </div>

    <!-- 新建体检 -->
    <div class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('qcNewTitle', ui.lang) }}</div>
        <div class="card-actions">
          <el-radio-group v-model="mode" size="small">
            <el-radio-button value="paste">
              {{ t('qcModePaste', ui.lang) }}
            </el-radio-button>
            <el-radio-button value="csv">
              {{ t('qcModeCsv', ui.lang) }}
            </el-radio-button>
          </el-radio-group>
        </div>
      </div>

      <div class="qc-body">
        <template v-if="mode === 'paste'">
          <label class="qc-label" for="qc-paste">
            {{ t('qcQuestions', ui.lang) }}
          </label>
          <el-input
            id="qc-paste"
            v-model="pasteText"
            type="textarea"
            :rows="6"
            :placeholder="t('qcQuestionsPlaceholder', ui.lang)"
          />
        </template>

        <template v-else>
          <div class="qc-csv-row">
            <input
              ref="fileInput"
              class="qc-file-input"
              type="file"
              accept=".csv,text/csv"
              @change="onFileChange"
            >
            <el-button :loading="csvParsing" @click="pickFile">
              <Upload :size="14" />
              {{ t('qcCsvPick', ui.lang) }}
            </el-button>
            <span v-if="csvFile" class="qc-csv-name cell-mono">
              {{ csvFile.name }}
            </span>
            <el-input
              v-model="csvColumn"
              class="qc-column-input"
              :placeholder="t('qcCsvColumnAuto', ui.lang)"
              :aria-label="t('qcCsvColumn', ui.lang)"
              @change="reparseCsv"
            />
          </div>
          <div v-if="csvError" class="qc-error" role="alert">{{ csvError }}</div>
        </template>

        <!-- 解析预览 -->
        <div v-if="parsed && mode === 'csv'" class="qc-preview">
          <div class="qc-preview-title">{{ t('qcParseTitle', ui.lang) }}</div>
          <div class="qc-preview-grid">
            <span>
              {{ t('qcParsedCount', ui.lang) }}:
              <b>{{ parsed.question_count }}</b>
            </span>
            <span>{{ t('qcEncoding', ui.lang) }}: {{ parsed.encoding }}</span>
            <span>
              {{ t('qcHeader', ui.lang) }}:
              {{ parsed.header?.length ? parsed.header.join(', ') : '—' }}
            </span>
            <span>{{ t('qcRowsSeen', ui.lang) }}: {{ parsed.rows_seen }}</span>
            <span>{{ t('qcDuplicates', ui.lang) }}: {{ parsed.duplicates }}</span>
            <span>
              {{ t('qcSkipped', ui.lang) }}:
              {{ parsed.skipped_blank + parsed.skipped_long }}
            </span>
          </div>
          <div v-if="parsed.truncated" class="qc-warn">
            <TriangleAlert :size="14" aria-hidden="true" />
            {{ t('qcTruncatedWarning', ui.lang) }}
          </div>
          <ul v-if="parsed.issues.length" class="qc-issues">
            <li v-for="(issue, i) in parsed.issues" :key="i">
              <span class="cell-mono">
                {{ issue.row != null ? `row ${issue.row}` : '' }}
                {{ issue.reason || issue.message || '' }}
              </span>
            </li>
          </ul>
        </div>
        <div v-else-if="mode === 'paste'" class="qc-hint">
          {{ questions.length }} × {{ t('qcQuestions', ui.lang) }}
        </div>
      </div>
    </div>

    <!-- 成本预检 -->
    <div class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('qcPreflightTitle', ui.lang) }}</div>
        <div class="card-actions">
          <span v-if="preflightLoading" class="pill pill-neutral">
            {{ t('qcLoading', ui.lang) }}
          </span>
        </div>
      </div>
      <div class="qc-body">
        <p class="qc-hint">{{ t('qcPreflightHint', ui.lang) }}</p>

        <div v-if="!values.ds || !questions.length" class="qc-empty">
          {{ t('qcPreflightEmpty', ui.lang) }}
        </div>
        <div v-else-if="preflightError" class="qc-error" role="alert">
          {{ t('qcPreflightFailed', ui.lang) }} · {{ preflightError }}
          <el-button text size="small" @click="runPreflight">
            {{ t('retry', ui.lang) }}
          </el-button>
        </div>

        <template v-else-if="preflight">
          <div class="qc-stat-grid">
            <div class="qc-stat">
              <span class="qc-stat-label">{{ t('qcQuestions', ui.lang) }}</span>
              <span class="qc-stat-value">{{ preflight.question_count }}</span>
            </div>
            <div class="qc-stat">
              <span class="qc-stat-label">{{ t('qcNotQuery', ui.lang) }}</span>
              <span class="qc-stat-value">{{ preflight.not_query }}</span>
            </div>
            <div class="qc-stat">
              <span class="qc-stat-label">{{ t('qcCertified', ui.lang) }}</span>
              <span class="qc-stat-value">{{ preflight.certified }}</span>
            </div>
            <div class="qc-stat">
              <span class="qc-stat-label">{{ t('qcUnmatched', ui.lang) }}</span>
              <span class="qc-stat-value">{{ preflight.unmatched }}</span>
            </div>
            <div class="qc-stat">
              <span class="qc-stat-label">{{ t('qcNeedsPlan', ui.lang) }}</span>
              <span class="qc-stat-value">{{ preflight.needs_plan }}</span>
            </div>
            <div class="qc-stat">
              <span class="qc-stat-label">{{ t('qcTypicalCalls', ui.lang) }}</span>
              <span class="qc-stat-value">
                {{ preflight.estimated_calls.typical }}
                <span class="qc-stat-sub">
                  {{ t('qcHardCap', ui.lang) }}
                  {{ preflight.hard_call_cap }}
                </span>
              </span>
            </div>
          </div>

          <div class="qc-meta">
            <div>
              <span class="qc-meta-label">{{ t('qcModel', ui.lang) }}</span>
              <span class="cell-mono">{{ preflight.model || '—' }}</span>
              <span class="pill pill-neutral">{{ preflight.model_source || '—' }}</span>
            </div>
            <div>
              <span class="qc-meta-label">{{ t('qcDigest', ui.lang) }}</span>
              <span class="cell-mono">{{ shortDigest(preflight.semantic_digest) }}</span>
            </div>
          </div>

          <div
            v-for="blocker in preflight.blockers"
            :key="blocker.code"
            class="qc-blocker"
          >
            <TriangleAlert :size="14" aria-hidden="true" />
            <span>{{ blocker.message }}</span>
            <router-link v-if="blocker.href" :to="blocker.href">
              {{ t('qcOpenKb', ui.lang) }}
            </router-link>
          </div>

          <div v-if="preflight.active_run_id" class="qc-blocker">
            <span>{{ t('qcActiveRun', ui.lang) }}</span>
            <el-button text size="small" @click="selectRun(preflight.active_run_id)">
              {{ t('qcOpenActiveRun', ui.lang) }}
            </el-button>
          </div>

          <p class="qc-hint">{{ t('qcPrivacyNote', ui.lang) }}</p>
          <el-checkbox v-model="costConfirmed">
            {{ t('qcConfirmCost', ui.lang) }}
          </el-checkbox>
          <div class="qc-start-row">
            <el-button
              type="primary"
              :disabled="!canStart"
              :loading="starting"
              @click="startRun"
            >
              {{ t('qcStart', ui.lang) }}
            </el-button>
            <span v-if="!canStart" class="qc-hint">
              {{ t('qcStartHint', ui.lang) }}
            </span>
          </div>
        </template>
      </div>
    </div>

    <!-- 进度 -->
    <div
      v-if="status && isActiveRun(status.status)"
      class="admin-card qc-progress-card"
    >
      <div class="card-header">
        <div class="card-title">
          {{ t('qcRunning', ui.lang) }}
          <span class="cell-mono qc-run-id">{{ shortId(status.run_id) }}</span>
        </div>
        <el-button
          size="small"
          :disabled="status.cancel_requested"
          @click="cancelOpen = true"
        >
          {{ t('qcCancelRun', ui.lang) }}
        </el-button>
      </div>
      <div class="qc-body">
        <el-progress :percentage="progressPct" :stroke-width="8" />
        <div class="qc-progress-meta">
          <span>{{ t('qcProcessed', ui.lang, status.processed_count) }}</span>
          <span>{{ t('qcCallsUsed', ui.lang) }}: {{ status.llm_calls_total }}</span>
          <span class="cell-mono">{{ status.model }}</span>
        </div>
        <p v-if="status.cancel_requested" class="qc-hint">
          {{ t('qcCancelRequested', ui.lang) }}
        </p>
      </div>
    </div>

    <!-- 中断 / 运行级失败 -->
    <div
      v-if="status && status.status === 'interrupted'"
      class="admin-card qc-banner-card"
    >
      <div class="qc-banner">
        <TriangleAlert :size="16" aria-hidden="true" />
        <span>{{ t('qcInterrupted', ui.lang) }}</span>
        <el-button size="small" @click="rerun">{{ t('qcRerun', ui.lang) }}</el-button>
      </div>
    </div>
    <div
      v-else-if="status && status.status === 'error'"
      class="admin-card qc-banner-card"
    >
      <div class="qc-banner qc-banner-danger" role="alert">
        <TriangleAlert :size="16" aria-hidden="true" />
        <span>{{ t('qcRunError', ui.lang) }}: {{ status.error }}</span>
        <el-button size="small" @click="rerun">{{ t('qcRerun', ui.lang) }}</el-button>
      </div>
    </div>

    <!-- 报告 -->
    <template v-if="report">
      <div class="admin-card">
        <div class="card-header">
          <div class="card-title">
            {{ t('qcReportTitle', ui.lang) }}
            <span class="cell-mono qc-run-id">{{ shortId(report.run.run_id) }}</span>
            <span class="pill" :class="statusClass(report.run.status)">
              {{ statusLabel(report.run.status) }}
            </span>
          </div>
          <div class="card-actions qc-report-meta">
            <span>{{ t('qcCreated', ui.lang) }}: {{ fmtDateTime(report.run.created_at) }}</span>
            <span class="cell-mono">{{ report.run.model || '—' }}</span>
            <span>
              {{ t('qcCallsUsed', ui.lang) }}:
              {{ report.summary.llm_calls_total ?? '—' }}
            </span>
          </div>
        </div>
        <div class="qc-body">
          <div v-if="report.summary.warnings.length" class="qc-warnings">
            <div
              v-for="w in report.summary.warnings"
              :key="w"
              class="qc-warn"
            >
              <TriangleAlert :size="14" aria-hidden="true" />
              {{ warningLabel(w) }}
            </div>
          </div>

          <div class="qc-kpis">
            <KpiTile
              :label="t('qcKpiDeterministic', ui.lang)"
              :value="pct(metrics?.deterministic_coverage.rate)"
              :sub="kpiSub('deterministic_coverage')"
            />
            <KpiTile
              :label="t('qcKpiGoverned', ui.lang)"
              :value="pct(metrics?.governed_coverage.rate)"
              :sub="kpiSub('governed_coverage')"
            />
            <KpiTile
              :label="t('qcKpiRefusal', ui.lang)"
              :value="ratioNum(report.summary.counts.refused)"
              :sub="kpiSub('refusal_rate')"
            />
            <KpiTile
              :label="t('qcKpiUnmatched', ui.lang)"
              :value="ratioNum(report.summary.counts.unmatched)"
              :sub="kpiSub('unmatched_rate')"
            />
          </div>

          <div class="qc-dist-title">{{ t('qcOutcomeDist', ui.lang) }}</div>
          <div class="qc-dist" role="img" :aria-label="t('qcOutcomeDist', ui.lang)">
            <span
              v-for="row in report.summary.by_outcome"
              v-show="row.count > 0"
              :key="row.outcome"
              class="qc-dist-seg"
              :class="`qc-out-${row.outcome}`"
              :style="{ flexGrow: row.count }"
              :title="`${outcomeLabel(row.outcome)}: ${row.count}`"
            />
          </div>
          <div class="qc-dist-legend">
            <span v-for="row in report.summary.by_outcome" :key="row.outcome">
              <i class="qc-dot" :class="`qc-out-${row.outcome}`" />
              {{ outcomeLabel(row.outcome) }} {{ row.count }}
            </span>
          </div>

          <div v-if="report.summary.by_miss_reason.length" class="qc-reasons">
            <span class="qc-meta-label">{{ t('qcGapReason', ui.lang) }}</span>
            <span
              v-for="row in report.summary.by_miss_reason"
              :key="row.reason"
              class="pill pill-neutral cell-mono"
            >
              {{ row.reason }} · {{ row.count }}
            </span>
          </div>
        </div>
      </div>

      <!-- 缺口汇总 -->
      <div class="admin-card">
        <div class="card-header">
          <div class="card-title">{{ t('qcGapTitle', ui.lang) }}</div>
          <div class="card-actions">
            <el-button
              size="small"
              :disabled="!selectedGapSeqs.length"
              @click="openDrafts('selected')"
            >
              {{ t('qcDraftSelected', ui.lang) }}
            </el-button>
            <el-button
              size="small"
              :disabled="!draftableGroups.length"
              @click="openDrafts('all')"
            >
              {{ t('qcDraftAll', ui.lang) }}
            </el-button>
          </div>
        </div>
        <div class="qc-body">
          <p class="qc-hint">{{ t('qcGapHint', ui.lang) }}</p>
          <el-table
            :data="report.gap_groups"
            class="admin-table"
            row-key="reason"
            max-height="320"
            @selection-change="onGapSelection"
          >
            <template #empty>
              <TableEmpty>{{ t('qcNoGaps', ui.lang) }}</TableEmpty>
            </template>
            <el-table-column
              type="selection"
              width="46"
              :selectable="isGapSelectable"
            />
            <el-table-column :label="t('qcGapKind', ui.lang)" width="110">
              <template #default="{ row }">
                <span class="pill pill-neutral">{{ gapKindLabel(row.kind) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcGapReason', ui.lang)" min-width="180">
              <template #default="{ row }">
                <span>{{ reasonLabel(row) }}</span>
                <span class="cell-mono qc-reason-slug">{{ row.reason }}</span>
                <span v-if="row.value" class="cell-mono qc-reason-slug">
                  = {{ row.value }}
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcGapComponent', ui.lang)" min-width="140">
              <template #default="{ row }">
                <span class="cell-mono">{{ row.component || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcGapCount', ui.lang)" width="80" align="right">
              <template #default="{ row }">{{ row.count }}</template>
            </el-table-column>
            <el-table-column :label="t('qcGapSample', ui.lang)" min-width="240">
              <template #default="{ row }">
                <div v-for="(sample, i) in row.samples" :key="i" class="qc-sample">
                  {{ sample }}
                </div>
              </template>
            </el-table-column>
            <el-table-column width="210">
              <template #default="{ row }">
                <span
                  class="pill"
                  :class="row.draftable ? 'pill-accent' : 'pill-neutral'"
                >
                  {{ row.draftable ? t('qcGapDraftable', ui.lang) : t('qcGapNotDraftable', ui.lang) }}
                </span>
                <router-link
                  v-if="row.workbench"
                  :to="row.workbench"
                  class="qc-workbench-link"
                >
                  {{ t('qcGapWorkbench', ui.lang) }}
                </router-link>
              </template>
            </el-table-column>
          </el-table>

          <div v-if="draftsResult" class="qc-drafts-result">
            <div class="qc-drafts-title">{{ t('qcDraftDone', ui.lang) }}</div>
            <div class="qc-drafts-counts">
              <span
                v-for="(count, draftStatus) in draftsResult.counts"
                :key="draftStatus"
                class="pill"
                :class="draftStatusClass(draftStatus)"
              >
                {{ draftStatusLabel(draftStatus) }} · {{ count }}
              </span>
              <span v-if="draftsResult.truncated" class="qc-warn">
                {{ t('qcDraftTruncated', ui.lang, draftsResult.truncated_assets) }}
              </span>
            </div>
            <div class="qc-drafts-rows">
              <div
                v-for="(row, i) in draftsResult.results"
                :key="`${row.seq}-${row.kind}-${row.name}-${i}`"
                class="qc-drafts-row"
              >
                <span class="cell-mono">{{ row.kind }}:{{ row.name }}</span>
                <span class="pill" :class="draftStatusClass(row.status)">
                  {{ draftStatusLabel(row.status) }}
                </span>
                <span v-if="row.errors.length" class="qc-error-text">
                  {{ row.errors.join('; ') }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 题目明细 -->
      <div class="admin-card">
        <div class="card-header">
          <div class="card-title">{{ t('qcDetailTitle', ui.lang) }}</div>
          <div class="card-actions">
            <el-select
              v-model="values.outcome"
              class="filter-select"
              :aria-label="t('qcFilterOutcome', ui.lang)"
            >
              <el-option :label="t('qcFilterAll', ui.lang)" value="" />
              <el-option
                v-for="o in OUTCOMES"
                :key="o"
                :label="outcomeLabel(o)"
                :value="o"
              />
            </el-select>
            <el-input
              v-model="values.q"
              class="qc-search"
              clearable
              :placeholder="t('qcSearch', ui.lang)"
              :aria-label="t('qcSearch', ui.lang)"
            />
          </div>
        </div>
        <div v-loading="reportLoading" class="qc-body">
          <el-table
            :data="filteredResults"
            class="admin-table"
            max-height="520"
            @row-click="openDetail"
          >
            <template #empty>
              <TableEmpty>{{ t('qcNoGaps', ui.lang) }}</TableEmpty>
            </template>
            <el-table-column :label="t('qcColSeq', ui.lang)" width="60">
              <template #default="{ row }">{{ row.seq + 1 }}</template>
            </el-table-column>
            <el-table-column :label="t('qcColQuestion', ui.lang)" min-width="320">
              <template #default="{ row }">
                <span class="qc-question-cell">{{ row.question }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcColOutcome', ui.lang)" width="150">
              <template #default="{ row }">
                <span class="pill" :class="outcomePillClass(row.outcome)">
                  {{ outcomeLabel(row.outcome) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcColReason', ui.lang)" min-width="150">
              <template #default="{ row }">
                <span class="cell-mono">{{ row.miss_reason || row.outcome_reason || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcColCalls', ui.lang)" width="70" align="right">
              <template #default="{ row }">{{ row.llm_calls }}</template>
            </el-table-column>
            <el-table-column :label="t('qcColElapsed', ui.lang)" width="90" align="right">
              <template #default="{ row }">{{ elapsed(row.elapsed_ms) }}</template>
            </el-table-column>
            <el-table-column width="90">
              <template #default="{ row }">
                <el-button text size="small" @click.stop="openDetail(row)">
                  {{ t('qcRowDetail', ui.lang) }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </template>

    <!-- 历史与趋势 -->
    <div class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('qcHistoryTitle', ui.lang) }}</div>
        <div class="card-actions">
          <span v-if="historyLoading" class="pill pill-neutral">
            {{ t('qcLoading', ui.lang) }}
          </span>
          <el-button size="small" @click="loadHistory">
            {{ t('refresh', ui.lang) }}
          </el-button>
        </div>
      </div>
      <div class="qc-body">
        <div v-if="historyError" class="qc-error" role="alert">
          {{ t('qcLoadFailed', ui.lang) }} · {{ historyError }}
        </div>

        <div v-if="trendRuns.length" class="qc-trend">
          <div class="qc-meta-label">{{ t('qcTrendTitle', ui.lang) }}</div>
          <div class="qc-trend-bars">
            <span
              v-for="run in trendRuns"
              :key="run.run_id"
              class="qc-trend-bar"
              :style="{ height: trendHeight(run) }"
              :title="`${fmtDateTime(run.created_at)} · ${pct(run.summary?.metrics?.deterministic_coverage.rate)}`"
            />
          </div>
        </div>

        <el-table :data="history" class="admin-table" max-height="360">
          <template #empty>
            <TableEmpty>{{ t('qcHistoryEmpty', ui.lang) }}</TableEmpty>
          </template>
          <el-table-column :label="t('qcColRun', ui.lang)" width="130">
            <template #default="{ row }">
              <span class="cell-mono">{{ shortId(row.run_id) }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('qcColStatus', ui.lang)" width="120">
            <template #default="{ row }">
              <span class="pill" :class="statusClass(row.status)">
                {{ statusLabel(row.status) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('qcColCreated', ui.lang)" width="170">
            <template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('qcColQuestions', ui.lang)" width="80" align="right">
            <template #default="{ row }">{{ row.question_count }}</template>
          </el-table-column>
          <el-table-column :label="t('qcColCoverage', ui.lang)" width="170">
            <template #default="{ row }">
              {{ pct(row.summary?.metrics?.deterministic_coverage.rate) }}
              <span class="qc-history-sub">
                {{ ratioText(row.summary?.metrics?.deterministic_coverage) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('qcColCalls', ui.lang)" width="80" align="right">
            <template #default="{ row }">
              {{ row.summary?.llm_calls_total ?? '—' }}
            </template>
          </el-table-column>
          <el-table-column width="200">
            <template #default="{ row }">
              <el-button text size="small" @click="selectRun(row.run_id)">
                {{ t('qcViewRun', ui.lang) }}
              </el-button>
              <el-button
                text
                size="small"
                :disabled="!row.previous_done_run_id"
                @click="selectRun(row.run_id, true)"
              >
                {{ t('qcCompare', ui.lang) }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <!-- 两轮对比 -->
    <div v-if="diff && (diff.base_run_id || diffRequested)" class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('qcDiffTitle', ui.lang) }}</div>
        <div class="card-actions">
          <span v-if="diff.base_run_id" class="cell-mono qc-run-id">
            {{ shortId(diff.base_run_id) }} → {{ shortId(diff.run_id) }}
          </span>
        </div>
      </div>
      <div class="qc-body">
        <div v-if="!diff.base_run_id" class="qc-empty">
          {{ t('qcDiffNoPrevious', ui.lang) }}
        </div>
        <template v-else>
          <div v-if="diff.semantic_changed" class="qc-warn">
            <TriangleAlert :size="14" aria-hidden="true" />
            {{ t('qcDiffSemanticChanged', ui.lang) }}
          </div>
          <div v-if="diff.taxonomy_changed" class="qc-warn">
            <TriangleAlert :size="14" aria-hidden="true" />
            {{ t('qcDiffTaxonomyChanged', ui.lang) }}
          </div>

          <div class="qc-diff-counts">
            <span class="pill pill-ok">{{ t('qcDiffImproved', ui.lang) }} · {{ diff.changes.improved }}</span>
            <span class="pill pill-danger">{{ t('qcDiffRegressed', ui.lang) }} · {{ diff.changes.regressed }}</span>
            <span class="pill pill-neutral">{{ t('qcDiffChanged', ui.lang) }} · {{ diff.changes.changed }}</span>
            <span class="pill pill-neutral">{{ t('qcDiffSame', ui.lang) }} · {{ diff.changes.same }}</span>
            <span class="pill pill-disabled">{{ t('qcDiffNotComparable', ui.lang) }} · {{ diff.changes.not_comparable }}</span>
            <span class="qc-hint">
              {{ t('qcDiffMatched', ui.lang) }} {{ diff.matched }} ·
              {{ t('qcDiffBaseOnly', ui.lang) }} {{ diff.base_only }} ·
              {{ t('qcDiffCurrentOnly', ui.lang) }} {{ diff.current_only }}
            </span>
          </div>

          <el-table :data="diffMetricRows" class="admin-table">
            <el-table-column :label="t('qcDiffMetric', ui.lang)" min-width="200">
              <template #default="{ row }">{{ metricLabel(row.name) }}</template>
            </el-table-column>
            <el-table-column :label="t('qcDiffBase', ui.lang)" width="120" align="right">
              <template #default="{ row }">{{ pct(row.base) }}</template>
            </el-table-column>
            <el-table-column :label="t('qcDiffCurrent', ui.lang)" width="120" align="right">
              <template #default="{ row }">{{ pct(row.current) }}</template>
            </el-table-column>
            <el-table-column :label="t('qcDiffDelta', ui.lang)" width="130" align="right">
              <template #default="{ row }">{{ pp(row.delta) }}</template>
            </el-table-column>
          </el-table>

          <el-table :data="diff.questions" class="admin-table" max-height="360">
            <template #empty>
              <TableEmpty>{{ t('qcDiffNoPrevious', ui.lang) }}</TableEmpty>
            </template>
            <el-table-column :label="t('qcColSeq', ui.lang)" width="60">
              <template #default="{ row }">{{ row.seq + 1 }}</template>
            </el-table-column>
            <el-table-column :label="t('qcColQuestion', ui.lang)" min-width="280">
              <template #default="{ row }">{{ row.question }}</template>
            </el-table-column>
            <el-table-column :label="t('qcDiffBase', ui.lang)" width="150">
              <template #default="{ row }">
                <span class="pill" :class="outcomePillClass(row.base_outcome)">
                  {{ outcomeLabel(row.base_outcome) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcDiffCurrent', ui.lang)" width="150">
              <template #default="{ row }">
                <span class="pill" :class="outcomePillClass(row.outcome)">
                  {{ outcomeLabel(row.outcome) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="t('qcDiffDelta', ui.lang)" width="150">
              <template #default="{ row }">
                <span class="pill" :class="changePillClass(row.change)">
                  {{ changeLabel(row.change) }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </div>

    <!-- 题目详情抽屉 -->
    <DetailDrawer
      v-model="detailOpen"
      :title="t('qcDetailTitle', ui.lang)"
      width="640px"
      :close-label="t('close', ui.lang)"
    >
      <div v-if="detailRow" class="qc-detail">
        <div class="qc-detail-question">{{ detailRow.question }}</div>
        <div class="qc-detail-meta">
          <span class="pill" :class="outcomePillClass(detailRow.outcome)">
            {{ outcomeLabel(detailRow.outcome) }}
          </span>
          <span v-if="detailRow.miss_reason" class="cell-mono">
            {{ detailRow.miss_reason }}
          </span>
          <span class="qc-hint">
            {{ detailRow.llm_calls }} calls · {{ elapsed(detailRow.elapsed_ms) }}
          </span>
        </div>

        <section class="qc-detail-block">
          <h4>{{ t('qcAnchors', ui.lang) }}</h4>
          <div class="qc-anchor-list">
            <span
              v-for="tbl in detailRow.detail.matched_tables || []"
              :key="tbl"
              class="pill pill-neutral cell-mono"
            >
              {{ tbl }}
            </span>
            <span v-if="!(detailRow.detail.matched_tables || []).length" class="qc-hint">—</span>
          </div>
        </section>

        <section v-if="detailRow.detail.plan_preview" class="qc-detail-block">
          <h4>{{ t('qcPlanPreview', ui.lang) }}</h4>
          <pre class="qc-pre">{{ detailRow.detail.plan_preview }}</pre>
        </section>

        <section v-if="detailRow.detail.sql_preview" class="qc-detail-block">
          <h4>{{ t('qcSqlPreview', ui.lang) }}</h4>
          <pre class="qc-pre">{{ detailRow.detail.sql_preview }}</pre>
        </section>

        <section class="qc-detail-block">
          <h4>{{ t('qcGapsTitle', ui.lang) }}</h4>
          <div v-if="!detailRow.gaps.length" class="qc-hint">
            {{ t('qcNoGaps', ui.lang) }}
          </div>
          <div v-for="(gap, i) in detailRow.gaps" :key="i" class="qc-detail-gap">
            <span class="pill pill-neutral">{{ gapKindLabel(gap.kind) }}</span>
            <span class="cell-mono">{{ gap.reason }}</span>
            <span v-if="gap.component" class="cell-mono">{{ gap.component }}</span>
            <span v-if="gap.value" class="cell-mono">= {{ gap.value }}</span>
            <span class="pill" :class="gap.draftable ? 'pill-accent' : 'pill-neutral'">
              {{ gap.draftable ? t('qcGapDraftable', ui.lang) : t('qcGapNotDraftable', ui.lang) }}
            </span>
          </div>
        </section>

        <section
          v-if="(detailRow.detail.plan_validation?.errors || []).length"
          class="qc-detail-block"
        >
          <h4>{{ t('qcValidationTitle', ui.lang) }}</h4>
          <ul class="qc-issues">
            <li v-for="(e, i) in detailRow.detail.plan_validation?.errors" :key="i">
              <span class="cell-mono">{{ e }}</span>
            </li>
          </ul>
        </section>

        <section v-if="detailRow.detail.error" class="qc-detail-block">
          <h4>{{ t('qcErrorDetail', ui.lang) }}</h4>
          <pre class="qc-pre qc-pre-error">{{ detailRow.detail.error }}</pre>
        </section>

        <details class="qc-detail-block">
          <summary>{{ t('qcCompileMeta', ui.lang) }}</summary>
          <pre class="qc-pre">{{ JSON.stringify(detailRow.detail.compile_meta || {}, null, 2) }}</pre>
        </details>
      </div>
    </DetailDrawer>

    <!-- 取消确认 -->
    <ConfirmDialog
      v-model="cancelOpen"
      :title="t('qcCancelTitle', ui.lang)"
      :confirm-text="t('qcCancelConfirm', ui.lang)"
      :cancel-text="t('cancel', ui.lang)"
      danger
      :loading="cancelBusy"
      @confirm="confirmCancel"
    >
      <p>{{ t('qcCancelText', ui.lang) }}</p>
    </ConfirmDialog>

    <!-- 草稿确认 -->
    <ConfirmDialog
      v-model="draftsOpen"
      :title="t('qcDraftTitle', ui.lang)"
      :confirm-text="t('qcDraftConfirm', ui.lang)"
      :cancel-text="t('cancel', ui.lang)"
      :loading="draftBusy"
      @confirm="confirmDrafts"
    >
      <p>{{ t('qcDraftText', ui.lang) }}</p>
      <p v-if="draftMode === 'selected'" class="qc-hint">
        {{ selectedGapSeqs.length }} × {{ t('qcQuestions', ui.lang) }}
      </p>
      <p v-else class="qc-hint">
        {{ draftableGroups.length }} × {{ t('qcGapDraftable', ui.lang) }}
      </p>
    </ConfirmDialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RefreshCw, TriangleAlert, Upload } from 'lucide-vue-next'
import { apiGet } from '../../api/http'
import {
  cancelCoverageRun,
  coverageRunStatus,
  createCoverageRun,
  draftCoverageGaps,
  fetchCoverageDiff,
  fetchCoverageReport,
  listCoverageRuns,
  parseCoverageCsv,
  preflightCoverage,
  type CoverageDiff,
  type CoverageDraftsResult,
  type CoverageGapGroup,
  type CoverageOutcome,
  type CoverageParsed,
  type CoveragePreflight,
  type CoverageRatio,
  type CoverageReport,
  type CoverageResult,
  type CoverageRunMeta,
  type CoverageStatus,
} from '../../api/questionCoverage'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import { fmtDateTime } from '../../utils/format'
import { notifySuccess, toastError } from '../../utils/notify'
import { useListQuery } from '../../composables/useListQuery'
import PageHeader from '../../components/base/PageHeader.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import DetailDrawer from '../../components/base/DetailDrawer.vue'
import ConfirmDialog from '../../components/base/ConfirmDialog.vue'
import TableEmpty from '../../components/admin/TableEmpty.vue'

interface DsRow {
  name: string
}

const OUTCOMES: CoverageOutcome[] = [
  'certified',
  'compiled',
  'partial',
  'soft_miss',
  'refused',
  'unmatched',
  'not_query',
  'unplanned',
  'error',
  'skipped',
]

const ui = useUiStore()
const { values } = useListQuery({ ds: '', run: '', outcome: '', q: '' })

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('qcNav', ui.lang) },
])

/* ── datasource ─────────────────────────────────────────────────── */

const datasources = ref<DsRow[]>([])
const datasourcesLoading = ref(false)

async function loadDatasources() {
  datasourcesLoading.value = true
  try {
    const body = await apiGet('/v1/admin/datasources')
    datasources.value = (body.datasources ?? []) as DsRow[]
  } catch (err) {
    toastError(err, t('qcLoadFailed', ui.lang))
  } finally {
    datasourcesLoading.value = false
  }
}

/* ── input ──────────────────────────────────────────────────────── */

const mode = ref<'paste' | 'csv'>('paste')
const pasteText = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const csvFile = ref<File | null>(null)
const csvColumn = ref('')
const csvParsing = ref(false)
const csvError = ref('')
const parsed = ref<CoverageParsed | null>(null)

const pasteQuestions = computed(() =>
  pasteText.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0),
)

const questions = computed(() =>
  mode.value === 'csv' ? (parsed.value?.questions ?? []) : pasteQuestions.value,
)

function pickFile() {
  fileInput.value?.click()
}

async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  csvFile.value = file
  if (!file) return
  await parseCsvFile()
}

async function parseCsvFile() {
  if (!csvFile.value) return
  csvParsing.value = true
  csvError.value = ''
  try {
    parsed.value = await parseCoverageCsv(csvFile.value, csvColumn.value.trim())
  } catch (err) {
    parsed.value = null
    csvError.value = errorMessage(err)
  } finally {
    csvParsing.value = false
  }
}

function reparseCsv() {
  if (csvFile.value) void parseCsvFile()
}

/* ── preflight ──────────────────────────────────────────────────── */

const preflight = ref<CoveragePreflight | null>(null)
const preflightLoading = ref(false)
const preflightError = ref('')
const costConfirmed = ref(false)
const starting = ref(false)

let preflightTimer: ReturnType<typeof setTimeout> | null = null
let preflightToken = 0

function sourceLabel(): string {
  return mode.value === 'csv' ? (parsed.value?.source_label || 'csv') : 'paste'
}

async function runPreflight() {
  if (!values.ds || !questions.value.length) return
  const token = ++preflightToken
  preflightLoading.value = true
  preflightError.value = ''
  try {
    const body = await preflightCoverage({
      datasource: values.ds,
      questions: questions.value,
      source_label: sourceLabel(),
    })
    if (token !== preflightToken) return
    preflight.value = body
  } catch (err) {
    if (token !== preflightToken) return
    preflight.value = null
    preflightError.value = errorMessage(err)
  } finally {
    if (token === preflightToken) preflightLoading.value = false
  }
}

function schedulePreflight() {
  preflightToken += 1
  costConfirmed.value = false
  if (preflightTimer) {
    clearTimeout(preflightTimer)
    preflightTimer = null
  }
  preflight.value = null
  preflightError.value = ''
  preflightLoading.value = false
  if (!values.ds || !questions.value.length) return
  preflightTimer = setTimeout(() => {
    preflightTimer = null
    void runPreflight()
  }, 400)
}

watch(
  () => [values.ds, mode.value, questions.value.join('\u0000')] as const,
  () => schedulePreflight(),
)

const canStart = computed(
  () =>
    !!preflight.value &&
    preflight.value.blockers.length === 0 &&
    !preflight.value.active_run_id &&
    costConfirmed.value &&
    questions.value.length > 0,
)

/* ── run / polling ──────────────────────────────────────────────── */

const currentRunId = ref('')
const status = ref<CoverageStatus | null>(null)
const report = ref<CoverageReport | null>(null)
const reportLoading = ref(false)
const diff = ref<CoverageDiff | null>(null)
const diffRequested = ref(false)

const POLL_MS = 2000
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollToken = 0

function isActiveRun(value: string): boolean {
  return value === 'queued' || value === 'running'
}

function stopPolling() {
  pollToken += 1
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function pollOnce(runId: string, token: number) {
  if (token !== pollToken) return
  try {
    const body = await coverageRunStatus(runId)
    if (token !== pollToken) return
    status.value = body
    if (isActiveRun(body.status)) {
      pollTimer = setTimeout(() => void pollOnce(runId, token), POLL_MS)
      return
    }
    await loadReport(runId)
    await loadHistory()
  } catch {
    // 网络抖动不该把进度卡死；状态错误不覆盖已有状态，5s 后重试。
    if (token !== pollToken) return
    pollTimer = setTimeout(() => void pollOnce(runId, token), 5000)
  }
}

function startPolling(runId: string) {
  stopPolling()
  const token = pollToken
  void pollOnce(runId, token)
}

const progressPct = computed(() => {
  const s = status.value
  if (!s || !s.question_count) return 0
  const pct = Math.round((s.processed_count / s.question_count) * 100)
  return isActiveRun(s.status) ? Math.min(pct, 99) : pct
})

async function startRun() {
  if (!canStart.value) return
  starting.value = true
  try {
    const created = await createCoverageRun({
      datasource: values.ds,
      questions: questions.value,
      source_label: sourceLabel(),
      confirm_cost: true,
    })
    stopPolling()
    currentRunId.value = created.run_id
    values.run = created.run_id
    status.value = null
    report.value = null
    diff.value = null
    diffRequested.value = false
    draftsResult.value = null
    gapSelection.value = []
    costConfirmed.value = false
    startPolling(created.run_id)
    await loadHistory()
  } catch (err) {
    toastError(err, t('qcRunError', ui.lang))
  } finally {
    starting.value = false
  }
}

async function loadDiff(runId: string) {
  try {
    diff.value = await fetchCoverageDiff(runId)
  } catch {
    diff.value = null
  }
}

async function loadReport(runId: string) {
  reportLoading.value = true
  draftsResult.value = null
  gapSelection.value = []
  try {
    report.value = await fetchCoverageReport(runId)
    await loadDiff(runId)
  } catch (err) {
    toastError(err, t('qcLoadFailed', ui.lang))
  } finally {
    reportLoading.value = false
  }
}

async function selectRun(runId: string, compare = false) {
  if (!runId) return
  stopPolling()
  currentRunId.value = runId
  values.run = runId
  diffRequested.value = compare
  report.value = null
  status.value = null
  try {
    const body = await coverageRunStatus(runId)
    status.value = body
    if (isActiveRun(body.status)) {
      startPolling(runId)
    } else {
      await loadReport(runId)
    }
  } catch (err) {
    toastError(err, t('qcLoadFailed', ui.lang))
  }
}

function resetForDatasource() {
  stopPolling()
  currentRunId.value = ''
  status.value = null
  report.value = null
  diff.value = null
  diffRequested.value = false
  draftsResult.value = null
  gapSelection.value = []
  preflight.value = null
  preflightError.value = ''
  costConfirmed.value = false
  values.run = ''
}

watch(
  () => values.ds,
  () => {
    resetForDatasource()
    void loadHistory()
  },
)

function rerun() {
  const qs = report.value?.run.questions ?? []
  if (!qs.length) return
  mode.value = 'paste'
  pasteText.value = qs.join('\n')
  parsed.value = null
  csvFile.value = null
  resetForDatasource()
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

/* ── cancel ─────────────────────────────────────────────────────── */

const cancelOpen = ref(false)
const cancelBusy = ref(false)

async function confirmCancel() {
  if (!currentRunId.value) return
  cancelBusy.value = true
  try {
    await cancelCoverageRun(currentRunId.value)
    cancelOpen.value = false
    if (status.value) status.value.cancel_requested = true
    notifySuccess(t('qcCancelRequested', ui.lang))
  } catch (err) {
    toastError(err)
  } finally {
    cancelBusy.value = false
  }
}

/* ── report display ─────────────────────────────────────────────── */

const metrics = computed(() => report.value?.summary.metrics)

const filteredResults = computed(() => {
  const q = values.q.trim().toLowerCase()
  return (report.value?.results ?? []).filter((row) => {
    if (values.outcome && row.outcome !== values.outcome) return false
    if (q && !row.question.toLowerCase().includes(q)) return false
    return true
  })
})

function pct(rate: number | null | undefined): string {
  return rate == null ? '—' : `${(rate * 100).toFixed(1)}%`
}

function pp(delta: number | null | undefined): string {
  if (delta == null) return '—'
  const sign = delta > 0 ? '+' : ''
  return `${sign}${(delta * 100).toFixed(1)}pp`
}

function ratioNum(value: number | undefined): string {
  return value == null ? '—' : String(value)
}

function ratioText(value: CoverageRatio | undefined): string {
  if (!value) return ''
  return `${value.num}/${value.den}`
}

function kpiSub(name: keyof NonNullable<typeof metrics.value>): string {
  const r = metrics.value?.[name]
  if (!r) return ''
  const base = `${r.num}/${r.den}`
  if (r.rate == null) return `${base} · ${t('qcNoSample', ui.lang)}`
  const delta = diff.value?.summary_delta?.[name]?.delta
  if (delta != null) {
    return `${base} · ${t('qcVsPrevious', ui.lang)} ${pp(delta)}`
  }
  return base
}

function shortId(value: string): string {
  return value ? value.slice(0, 8) : '—'
}

function shortDigest(value: string): string {
  return value ? value.slice(0, 12) : '—'
}

function elapsed(ms: number): string {
  if (ms == null) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

function outcomeLabel(o: string): string {
  switch (o) {
    case 'certified': return t('qcOutcomeCertified', ui.lang)
    case 'compiled': return t('qcOutcomeCompiled', ui.lang)
    case 'partial': return t('qcOutcomePartial', ui.lang)
    case 'soft_miss': return t('qcOutcomeSoftMiss', ui.lang)
    case 'refused': return t('qcOutcomeRefused', ui.lang)
    case 'unmatched': return t('qcOutcomeUnmatched', ui.lang)
    case 'not_query': return t('qcOutcomeNotQuery', ui.lang)
    case 'unplanned': return t('qcOutcomeUnplanned', ui.lang)
    case 'error': return t('qcOutcomeError', ui.lang)
    case 'skipped': return t('qcOutcomeSkipped', ui.lang)
    default: return o
  }
}

function outcomePillClass(o: string): string {
  if (o === 'certified' || o === 'compiled') return 'pill-ok'
  if (o === 'partial' || o === 'soft_miss') return 'pill-warn'
  if (o === 'refused' || o === 'error') return 'pill-danger'
  if (o === 'unmatched') return 'pill-warn'
  return 'pill-neutral'
}

function statusLabel(s: string): string {
  switch (s) {
    case 'queued': return t('qcStatusQueued', ui.lang)
    case 'running': return t('qcStatusRunning', ui.lang)
    case 'done': return t('qcStatusDone', ui.lang)
    case 'error': return t('qcStatusError', ui.lang)
    case 'cancelled': return t('qcStatusCancelled', ui.lang)
    case 'interrupted': return t('qcStatusInterrupted', ui.lang)
    default: return s
  }
}

function statusClass(s: string): string {
  if (s === 'done') return 'pill-ok'
  if (s === 'running' || s === 'queued') return 'pill-accent'
  if (s === 'error') return 'pill-danger'
  if (s === 'interrupted') return 'pill-warn'
  return 'pill-neutral'
}

function gapKindLabel(kind: string): string {
  switch (kind) {
    case 'metric': return t('qcGapKindMetric', ui.lang)
    case 'field': return t('qcGapKindField', ui.lang)
    case 'value': return t('qcGapKindValue', ui.lang)
    case 'entity': return t('qcGapKindEntity', ui.lang)
    case 'relation': return t('qcGapKindRelation', ui.lang)
    case 'dataset': return t('qcGapKindDataset', ui.lang)
    default: return t('qcGapKindOther', ui.lang)
  }
}

function reasonLabel(group: CoverageGapGroup): string {
  const label = group.label || {}
  return (ui.lang === 'zh' ? label.zh : label.en) || group.reason
}

function warningLabel(code: string): string {
  switch (code) {
    case 'high_unmeasured': return t('qcWarnHighUnmeasured', ui.lang)
    case 'semantic_changed_during_run': return t('qcWarnSemanticChanged', ui.lang)
    case 'model_changed_during_run': return t('qcWarnModelChanged', ui.lang)
    case 'interrupted': return t('qcWarnInterrupted', ui.lang)
    case 'preflight_errors': return t('qcWarnPreflightErrors', ui.lang)
    default: return code
  }
}

function metricLabel(name: string): string {
  switch (name) {
    case 'deterministic_coverage': return t('qcMetricDeterministicCoverage', ui.lang)
    case 'governed_coverage': return t('qcMetricGovernedCoverage', ui.lang)
    case 'gap_rate': return t('qcMetricGapRate', ui.lang)
    case 'refusal_rate': return t('qcMetricRefusalRate', ui.lang)
    case 'unmatched_rate': return t('qcMetricUnmatchedRate', ui.lang)
    case 'passthrough_rate': return t('qcMetricPassthroughRate', ui.lang)
    default: return name
  }
}

const diffMetricRows = computed(() => {
  const delta = diff.value?.summary_delta ?? {}
  return Object.keys(delta).map((name) => ({
    name,
    base: delta[name]?.base?.rate ?? null,
    current: delta[name]?.current?.rate ?? null,
    delta: delta[name]?.delta ?? null,
  }))
})

function changeLabel(change: string): string {
  switch (change) {
    case 'improved': return t('qcDiffImproved', ui.lang)
    case 'regressed': return t('qcDiffRegressed', ui.lang)
    case 'changed': return t('qcDiffChanged', ui.lang)
    case 'same': return t('qcDiffSame', ui.lang)
    default: return t('qcDiffNotComparable', ui.lang)
  }
}

function changePillClass(change: string): string {
  if (change === 'improved') return 'pill-ok'
  if (change === 'regressed') return 'pill-danger'
  if (change === 'changed') return 'pill-warn'
  if (change === 'same') return 'pill-neutral'
  return 'pill-disabled'
}

/* ── detail drawer ──────────────────────────────────────────────── */

const detailOpen = ref(false)
const detailRow = ref<CoverageResult | null>(null)

function openDetail(row: CoverageResult) {
  detailRow.value = row
  detailOpen.value = true
}

/* ── drafts ─────────────────────────────────────────────────────── */

const gapSelection = ref<CoverageGapGroup[]>([])
const draftsOpen = ref(false)
const draftMode = ref<'selected' | 'all'>('selected')
const draftBusy = ref(false)
const draftsResult = ref<CoverageDraftsResult | null>(null)

const draftableGroups = computed(
  () => report.value?.gap_groups.filter((g) => g.draftable && g.seqs.length > 0) ?? [],
)

const selectedGapSeqs = computed(() => {
  const seqs = new Set<number>()
  for (const group of gapSelection.value) {
    for (const seq of group.seqs) seqs.add(seq)
  }
  return [...seqs].sort((a, b) => a - b)
})

function onGapSelection(rows: CoverageGapGroup[]) {
  gapSelection.value = rows
}

function openDrafts(mode: 'selected' | 'all') {
  draftMode.value = mode
  draftsOpen.value = true
}

async function confirmDrafts() {
  const runId = report.value?.run.run_id
  if (!runId) return
  draftBusy.value = true
  try {
    const body =
      draftMode.value === 'all'
        ? await draftCoverageGaps(runId, { all: true })
        : await draftCoverageGaps(runId, { seqs: selectedGapSeqs.value })
    draftsResult.value = body
    draftsOpen.value = false
    notifySuccess(`${t('qcDraftDone', ui.lang)} · ${body.counts.created}`)
  } catch (err) {
    toastError(err)
  } finally {
    draftBusy.value = false
  }
}

function draftStatusLabel(status: string): string {
  switch (status) {
    case 'created': return t('qcDraftStatusCreated', ui.lang)
    case 'exists': return t('qcDraftStatusExists', ui.lang)
    case 'invalid': return t('qcDraftStatusInvalid', ui.lang)
    case 'not_draftable': return t('qcDraftStatusNotDraftable', ui.lang)
    case 'stale': return t('qcDraftStatusStale', ui.lang)
    default: return status
  }
}

function draftStatusClass(status: string): string {
  if (status === 'created') return 'pill-ok'
  if (status === 'exists') return 'pill-neutral'
  if (status === 'invalid') return 'pill-danger'
  return 'pill-warn'
}

/* ── history / trend ────────────────────────────────────────────── */

const history = ref<CoverageRunMeta[]>([])
const historyLoading = ref(false)
const historyError = ref('')

async function loadHistory() {
  if (!values.ds) {
    history.value = []
    return
  }
  historyLoading.value = true
  historyError.value = ''
  try {
    const body = await listCoverageRuns(values.ds, { limit: 20 })
    history.value = body.runs
  } catch (err) {
    historyError.value = errorMessage(err)
  } finally {
    historyLoading.value = false
  }
}

const trendRuns = computed(() =>
  history.value
    .filter((run) => run.status === 'done')
    .slice(0, 10)
    .reverse(),
)

function trendHeight(run: CoverageRunMeta): string {
  const rate = run.summary?.metrics?.deterministic_coverage?.rate
  if (rate == null) return '4px'
  return `${Math.max(4, Math.round(rate * 56))}px`
}

/* ── helpers ────────────────────────────────────────────────────── */

function isGapSelectable(row: CoverageGapGroup): boolean {
  return row.draftable && row.seqs.length > 0
}

function errorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return String((err as { message: unknown }).message)
  }
  return String(err ?? '')
}

onMounted(async () => {
  await loadDatasources()
  if (!values.ds && datasources.value.length) {
    values.ds = datasources.value[0].name
  }
  if (values.ds) await loadHistory()
  if (values.run) await selectRun(values.run)
})

onBeforeUnmount(() => {
  stopPolling()
  if (preflightTimer) {
    clearTimeout(preflightTimer)
    preflightTimer = null
  }
})
</script>

<style scoped>
.qc-ds-select {
  min-width: 220px;
}

.qc-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
}

.qc-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.qc-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.qc-empty {
  padding: var(--sp-3) 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.qc-error {
  font-size: var(--fs-xs);
  color: var(--danger-text);
}

.qc-error-text {
  font-size: var(--fs-2xs);
  color: var(--danger-text);
}

.qc-csv-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}

.qc-file-input {
  display: none;
}

.qc-csv-name {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.qc-column-input {
  width: 220px;
}

.qc-preview {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-2) var(--sp-3);
  background: var(--surface-muted);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.qc-preview-title {
  font-size: var(--fs-xs);
  font-weight: 600;
}

.qc-preview-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.qc-warn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--warn-text);
}

.qc-warnings {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.qc-issues {
  margin: 0;
  padding-left: var(--sp-4);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.qc-stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: var(--sp-2);
}

.qc-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}

.qc-stat-label {
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.qc-stat-value {
  font-size: var(--fs-lg);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.qc-stat-sub {
  margin-left: var(--sp-2);
  font-size: var(--fs-2xs);
  font-weight: 400;
  color: var(--text-tertiary);
}

.qc-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-5);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.qc-meta-label {
  margin-right: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.qc-blocker {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--warn-border);
  border-radius: var(--r-md);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-xs);
}

.qc-start-row {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}

.qc-run-id {
  margin-left: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.qc-progress-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.qc-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--warn-text);
}

.qc-banner-danger {
  color: var(--danger-text);
}

.qc-banner-card {
  overflow: hidden;
}

.qc-report-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.qc-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-2);
}

.qc-dist-title {
  font-size: var(--fs-xs);
  font-weight: 600;
}

.qc-dist {
  display: flex;
  height: 14px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--surface-muted);
}

.qc-dist-seg {
  min-width: 2px;
}

.qc-dist-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}

.qc-dist-legend > span {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}

.qc-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.qc-out-certified { background: var(--text-primary); }
.qc-out-compiled { background: var(--accent); }
.qc-out-partial { background: var(--accent-graphic); opacity: 0.65; }
.qc-out-soft_miss { background: var(--warn); }
.qc-out-refused { background: var(--danger); }
.qc-out-unmatched { background: var(--warn); opacity: 0.5; }
.qc-out-not_query { background: var(--border-default); }
.qc-out-unplanned { background: var(--text-tertiary); opacity: 0.55; }
.qc-out-error { background: var(--danger); opacity: 0.55; }
.qc-out-skipped { background: var(--surface-muted); border: 1px solid var(--border-default); }

.qc-reasons {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
}

.qc-reason-slug {
  margin-left: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.qc-sample {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qc-workbench-link {
  margin-left: var(--sp-2);
  font-size: var(--fs-2xs);
}

.qc-drafts-result {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-muted);
}

.qc-drafts-title {
  font-size: var(--fs-xs);
  font-weight: 600;
}

.qc-drafts-counts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
}

.qc-drafts-rows {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  max-height: 200px;
  overflow-y: auto;
}

.qc-drafts-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
}

.qc-search {
  width: 220px;
}

.qc-question-cell {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qc-history-sub {
  margin-left: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}

.qc-trend {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.qc-trend-bars {
  display: flex;
  align-items: flex-end;
  gap: var(--sp-1);
  height: 60px;
}

.qc-trend-bar {
  display: inline-block;
  width: 14px;
  border-radius: 2px 2px 0 0;
  background: var(--accent);
  opacity: 0.75;
}

.qc-diff-counts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
}

.qc-detail {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-1);
}

.qc-detail-question {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.qc-detail-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
}

.qc-detail-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.qc-detail-block h4 {
  margin: 0;
  font-size: var(--fs-xs);
  font-weight: 600;
}

.qc-anchor-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}

.qc-pre {
  margin: 0;
  padding: var(--sp-2);
  border-radius: var(--r-md);
  border: 1px solid var(--border-subtle);
  background: var(--surface-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow: auto;
}

.qc-pre-error {
  color: var(--danger-text);
}

.qc-detail-gap {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
}
</style>
