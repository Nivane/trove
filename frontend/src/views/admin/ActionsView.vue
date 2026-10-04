<!--
  ActionsView — 行动支柱的管理台(模板门 + 提案审批 + 外送回执)。

  两个 Tab(URL 键 tab / status,useListQuery):
    · templates —— `.trove/actions/<name>/action.yml` 的组织资产:create →
      pending → confirm 才可被规则引用。模板名一个通道(部署配置里解析),
      从不写裸 URL,所以"审模板"不涉及审密钥。
    · proposals —— 规则命中后生成的提案。这里是**唯一的批准入口**:
      approve 之前什么都不会外送;dispatch 之后回执落在 deliveries 里。

  两条来自后端的口径原样呈现(不美化):
    · enabled=false 是"半可用":模板管理照常,propose/dispatch 会被拒 ——
      横幅说明,不是整页报错;
    · payload 在创建时冻结,抽屉里看到的就是外送的那一份 —— 页面不给
      任何"改一改再发"的入口。
-->
<template>
  <div class="admin-view">
    <header class="view-header">
      <div>
        <h2>{{ t('actionsPage', ui.lang) }}</h2>
        <p class="view-desc">{{ t('actionsPageDesc', ui.lang) }}</p>
      </div>
      <div class="view-actions">
        <div class="tab-seg" role="tablist" :aria-label="t('actionsPage', ui.lang)">
          <button
            type="button"
            role="tab"
            class="seg-btn"
            :class="{ 'is-active': tab === 'templates' }"
            :aria-selected="tab === 'templates'"
            @click="setTab('templates')"
          >
            {{ t('actionsTabTemplates', ui.lang) }}
          </button>
          <button
            type="button"
            role="tab"
            class="seg-btn"
            :class="{ 'is-active': tab === 'proposals' }"
            :aria-selected="tab === 'proposals'"
            @click="setTab('proposals')"
          >
            {{ t('actionsTabProposals', ui.lang) }}
          </button>
        </div>
        <el-button :loading="loading" @click="load">
          <RefreshCw :size="14" />
        </el-button>
        <el-button v-if="tab === 'templates'" type="primary" @click="openCreate">
          <Plus :size="14" />
          {{ t('actionsNewTemplate', ui.lang) }}
        </el-button>
      </div>
    </header>

    <!-- enabled=false:提案与外送被拒,模板管理仍可用 —— 说明,不拦整页。 -->
    <div v-if="enabled === false" class="admin-card actions-banner">
      <span class="pill pill-warn">{{ t('actionsDisabledPill', ui.lang) }}</span>
      <span>{{ t('actionsDisabledBanner', ui.lang) }}</span>
    </div>

    <div v-if="listError" class="admin-card actions-banner is-error">
      <span class="pill pill-danger">{{ t('actionsLoadFailed', ui.lang) }}</span>
      <span class="cell-mono">{{ listError }}</span>
    </div>

    <!-- ── 模板 ─────────────────────────────────────────── -->
    <div v-if="tab === 'templates'" class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('actionsTemplatesTitle', ui.lang) }}</div>
        <div class="card-actions">
          <span class="pill pill-neutral">
            {{ t('actionsTemplateCount', ui.lang, templates.length) }}
          </span>
          <span class="cell-mono dim">
            {{
              channels.length
                ? t('actionsChannels', ui.lang, channels.join(', '))
                : t('actionsNoChannels', ui.lang)
            }}
          </span>
        </div>
      </div>
      <el-table v-loading="loading" :data="templates" class="admin-table">
        <template #empty>
          <TableEmpty>{{ t('actionsTemplatesEmpty', ui.lang) }}</TableEmpty>
        </template>
        <el-table-column :label="t('actionsTemplateName', ui.lang)" width="200">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.name }}</span>
            <span v-if="row.source && row.source !== 'admin'" class="pill pill-neutral actions-src">
              {{ row.source }}
            </span>
            <div v-if="row.error" class="actions-broken cell-mono">
              {{ t('actionsTemplateBroken', ui.lang) }} · {{ row.error }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsTemplateTitle', ui.lang)" min-width="200">
          <template #default="{ row }">{{ row.title || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('actionsChannel', ui.lang)" width="140">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.target?.channel || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsRisk', ui.lang)" width="100">
          <template #default="{ row }">
            <span class="pill" :class="riskClass(row.risk)">{{ riskLabel(row.risk) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsType', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.action_type }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsStatus', ui.lang)" width="110">
          <template #default="{ row }">
            <span class="pill" :class="templateStatusClass(row.status)">
              {{ templateStatusLabel(row.status) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsUpdatedAt', ui.lang)" width="150">
          <template #default="{ row }">
            <span class="actions-time">{{ fmtDateTime(row.updated_at) || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="230" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'pending' && !row.error"
              size="small"
              type="primary"
              @click="confirmTemplate(row)"
            >
              {{ t('actionsConfirm', ui.lang) }}
            </el-button>
            <el-button size="small" type="danger" plain @click="rejectTemplate(row)">
              {{ t('actionsReject', ui.lang) }}
            </el-button>
            <el-button size="small" plain @click="openPreview(row)">
              {{ t('actionsPreview', ui.lang) }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- ── 提案 ─────────────────────────────────────────── -->
    <div v-else class="admin-card">
      <div class="card-header">
        <div class="card-title">{{ t('actionsProposalsTitle', ui.lang) }}</div>
        <div class="card-actions">
          <span v-if="openCount !== null" class="pill pill-neutral">
            {{ t('actionsOpenCount', ui.lang, openCount) }}
          </span>
          <span
            v-for="s in ACTION_PROPOSAL_STATUSES"
            v-show="counts && (counts[s] ?? 0) > 0"
            :key="s"
            class="pill"
            :class="proposalStatusClass(s)"
          >
            {{ proposalStatusLabel(s) }} {{ counts?.[s] }}
          </span>
        </div>
      </div>

      <div class="actions-filters">
        <el-select
          v-model="values.status"
          class="actions-filter-select"
          :aria-label="t('actionsStatusFilter', ui.lang)"
        >
          <el-option value="open" :label="t('actionsStatusOpen', ui.lang)" />
          <el-option :value="ACTION_PROPOSALS_ALL" :label="t('actionsStatusAll', ui.lang)" />
          <el-option
            v-for="s in ACTION_PROPOSAL_STATUSES"
            :key="s"
            :value="s"
            :label="proposalStatusLabel(s)"
          />
        </el-select>
        <span class="dim actions-frozen-hint">{{ t('actionsFrozenHint', ui.lang) }}</span>
      </div>

      <el-table v-loading="loading" :data="proposals" class="admin-table">
        <template #empty>
          <TableEmpty>
            {{ values.status === 'open' ? t('actionsProposalsEmpty', ui.lang) : t('actionsProposalsNoMatch', ui.lang) }}
          </TableEmpty>
        </template>
        <el-table-column :label="t('actionsStatus', ui.lang)" width="120">
          <template #default="{ row }">
            <span class="pill" :class="proposalStatusClass(row.status)">
              {{ proposalStatusLabel(row.status) }}
            </span>
            <span v-if="isOverdue(row)" class="pill pill-warn actions-src">
              {{ t('actionsOverdue', ui.lang) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColRule', ui.lang)" min-width="180">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.rule_id }}</span>
            <div v-if="row.rationale" class="dim actions-rationale">{{ row.rationale }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColTemplate', ui.lang)" width="150">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.template }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsRisk', ui.lang)" width="90">
          <template #default="{ row }">
            <span class="pill" :class="riskClass(row.risk)">{{ riskLabel(row.risk) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColDatasource', ui.lang)" width="120">
          <template #default="{ row }">
            <span v-if="row.datasource" class="ds-chip cell-mono">{{ row.datasource }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColCreated', ui.lang)" width="150">
          <template #default="{ row }">
            <span class="actions-time">{{ fmtDateTime(row.created_at) || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColExpires', ui.lang)" width="150">
          <template #default="{ row }">
            <span class="actions-time">{{ fmtDateTime(row.expires_at) || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColAttempts', ui.lang)" width="80">
          <template #default="{ row }">
            <span class="cell-mono">{{ row.attempts }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('actionsColError', ui.lang)" min-width="150">
          <template #default="{ row }">
            <span v-if="row.error" class="actions-error-text">{{ row.error }}</span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="260" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'pending'"
              size="small"
              type="primary"
              @click="ask('approve', row)"
            >
              {{ t('actionsApprove', ui.lang) }}
            </el-button>
            <!-- 批准/驳回是审批队列的核心一对,都在行内;cancel 等少见动词进详情抽屉。 -->
            <el-button
              v-if="row.status === 'pending'"
              size="small"
              type="danger"
              plain
              @click="ask('reject', row)"
            >
              {{ t('actionsReject', ui.lang) }}
            </el-button>
            <el-button
              v-else-if="row.status === 'approved'"
              size="small"
              type="primary"
              @click="ask('dispatch', row)"
            >
              {{ t('actionsDispatch', ui.lang) }}
            </el-button>
            <el-button
              v-else-if="row.status === 'failed'"
              size="small"
              type="warning"
              plain
              @click="ask('retry', row)"
            >
              {{ t('actionsRetry', ui.lang) }}
            </el-button>
            <el-button
              v-else-if="row.status === 'dispatched' || row.status === 'delivered'"
              size="small"
              plain
              @click="ask('ack', row)"
            >
              {{ t('actionsAck', ui.lang) }}
            </el-button>
            <el-button size="small" plain @click="openDetail(row)">
              {{ t('actionsDetail', ui.lang) }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- ── 模板预览 ─────────────────────────────────────── -->
    <el-drawer v-model="previewOpen" :title="t('actionsPreviewTitle', ui.lang)" size="560px">
      <div class="actions-preview">
        <div class="actions-preview-head">
          <span class="cell-mono">{{ previewRow?.name }}</span>
          <span v-if="previewRow" class="pill" :class="templateStatusClass(previewRow.status)">
            {{ templateStatusLabel(previewRow.status) }}
          </span>
        </div>
        <div v-if="previewRow?.injection_hits?.length" class="actions-hits">
          <div class="actions-hits-title">{{ t('actionsInjectionHits', ui.lang) }}</div>
          <ul>
            <li v-for="(h, i) in previewRow.injection_hits" :key="i" class="cell-mono">{{ h }}</li>
          </ul>
        </div>
        <div class="actions-field-label">{{ t('actionsPayloadTemplate', ui.lang) }}</div>
        <pre class="cell-mono actions-pre">{{ previewRow?.payload_template }}</pre>
        <div class="actions-field-label">{{ t('actionsSampleVars', ui.lang) }}</div>
        <div class="actions-vars">
          <span v-for="v in sampleVars" :key="v" class="cell-mono actions-var">{{ v }}</span>
        </div>
      </div>
    </el-drawer>

    <!-- ── 新建模板 ─────────────────────────────────────── -->
    <el-dialog
      v-model="createOpen"
      :title="t('actionsCreateTitle', ui.lang)"
      width="680px"
    >
      <p class="view-desc">{{ t('actionsCreateHint', ui.lang) }}</p>
      <el-form label-position="top">
        <el-form-item :label="t('actionsTemplateName', ui.lang)">
          <el-input v-model="form.name" placeholder="notify-ops" spellcheck="false" />
        </el-form-item>
        <el-form-item :label="t('actionsTemplateTitle', ui.lang)">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item :label="t('actionsFieldDesc', ui.lang)">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item :label="t('actionsChannel', ui.lang)">
          <el-select
            v-model="form.channel"
            allow-create
            filterable
            default-first-option
            :placeholder="t('actionsChannelPh', ui.lang)"
          >
            <el-option v-for="c in channels" :key="c" :value="c" :label="c" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('actionsFieldResource', ui.lang)">
          <el-input v-model="form.resource" placeholder="#ops-alerts" />
        </el-form-item>
        <el-form-item :label="t('actionsType', ui.lang)">
          <el-radio-group v-model="form.action_type">
            <el-radio value="notify">notify</el-radio>
            <el-radio value="webhook">webhook</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('actionsRisk', ui.lang)">
          <el-radio-group v-model="form.risk">
            <el-radio value="low">{{ t('actionsRiskLow', ui.lang) }}</el-radio>
            <el-radio value="medium">{{ t('actionsRiskMedium', ui.lang) }}</el-radio>
            <el-radio value="high">{{ t('actionsRiskHigh', ui.lang) }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('actionsPayloadTemplate', ui.lang)">
          <el-input
            v-model="form.payload_template"
            type="textarea"
            :rows="5"
            class="actions-yaml"
            spellcheck="false"
            :placeholder="'{\n  &quot;rule&quot;: &quot;{{rule_id}}&quot;,\n  &quot;metric&quot;: &quot;{{metric}}&quot;\n}'"
          />
        </el-form-item>
        <div class="actions-field-label">{{ t('actionsSampleVars', ui.lang) }}</div>
        <div class="actions-vars">
          <span v-for="v in sampleVars" :key="v" class="cell-mono actions-var">{{ varToken(v) }}</span>
        </div>
      </el-form>
      <div v-if="createHits.length" class="actions-hits">
        <div class="actions-hits-title">{{ t('actionsInjectionHits', ui.lang) }}</div>
        <ul>
          <li v-for="(h, i) in createHits" :key="i" class="cell-mono">{{ h }}</li>
        </ul>
      </div>
      <div v-if="createError" class="actions-dialog-error cell-mono">{{ createError }}</div>
      <template #footer>
        <el-button @click="createOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">
          {{ t('actionsCreateSubmit', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── 审批对话框(所有动词共用)─────────────────────── -->
    <el-dialog
      v-model="dialogOpen"
      :title="dialogTitle"
      width="520px"
      class="actions-dialog"
    >
      <div v-if="dialogTarget" class="actions-dialog-target">
        <span class="cell-mono">{{ dialogTarget.rule_id }}</span>
        <span class="dim"> · {{ dialogTarget.id }}</span>
        <span class="pill" :class="proposalStatusClass(dialogTarget.status)">
          {{ proposalStatusLabel(dialogTarget.status) }}
        </span>
      </div>
      <p class="view-desc">{{ dialogHint }}</p>
      <el-input
        v-model="comment"
        type="textarea"
        :rows="3"
        :placeholder="commentRequired ? t('actionsCommentRequired', ui.lang) : t('actionsCommentPh', ui.lang)"
      />
      <div v-if="dialogError" class="actions-dialog-error cell-mono">{{ dialogError }}</div>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button
          :type="dialogVerb === 'reject' || dialogVerb === 'cancel' ? 'danger' : 'primary'"
          :loading="busy"
          :disabled="!canSubmit"
          @click="submitDecision"
        >
          {{ verbLabel(dialogVerb) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── 提案详情(载荷 + 审批轨迹 + 回执)─────────────── -->
    <el-drawer
      v-model="detailOpen"
      :title="t('actionsDetailTitle', ui.lang)"
      size="640px"
      @closed="detail = null"
    >
      <div v-if="detailLoading" class="dim">{{ t('actionsDetailLoading', ui.lang) }}</div>
      <div v-else-if="detailError" class="actions-dialog-error cell-mono">{{ detailError }}</div>
      <div v-else-if="detail" class="actions-detail">
        <div class="actions-detail-head">
          <span class="cell-mono">{{ detail.proposal.id }}</span>
          <span class="pill" :class="proposalStatusClass(detail.proposal.status)">
            {{ proposalStatusLabel(detail.proposal.status) }}
          </span>
          <span class="pill" :class="riskClass(detail.proposal.risk)">
            {{ t('actionsRisk', ui.lang) }} · {{ riskLabel(detail.proposal.risk) }}
          </span>
          <span v-if="detail.stale" class="pill pill-warn">{{ t('actionsStale', ui.lang) }}</span>
        </div>

        <dl class="actions-dl">
          <dt>{{ t('actionsColRule', ui.lang) }}</dt>
          <dd class="cell-mono">{{ detail.proposal.rule_id }}</dd>
          <dt>{{ t('actionsColTemplate', ui.lang) }}</dt>
          <dd class="cell-mono">{{ detail.proposal.template }} · sha256:{{ detail.proposal.template_digest }}</dd>
          <dt>{{ t('actionsColDatasource', ui.lang) }}</dt>
          <dd class="cell-mono">{{ detail.proposal.datasource || '—' }}</dd>
          <dt>{{ t('actionsCreatedBy', ui.lang) }}</dt>
          <dd class="cell-mono">{{ detail.proposal.created_by || '—' }}</dd>
          <dt>{{ t('actionsColCreated', ui.lang) }}</dt>
          <dd>{{ fmtDateTime(detail.proposal.created_at) || '—' }}</dd>
          <dt>{{ t('actionsDecidedAt', ui.lang) }}</dt>
          <dd>{{ fmtDateTime(detail.proposal.decided_at) || '—' }}</dd>
          <dt>{{ t('actionsDispatchedAt', ui.lang) }}</dt>
          <dd>{{ fmtDateTime(detail.proposal.dispatched_at) || '—' }}</dd>
          <dt>{{ t('actionsColExpires', ui.lang) }}</dt>
          <dd>{{ fmtDateTime(detail.proposal.expires_at) || '—' }}</dd>
          <dt>{{ t('actionsColAttempts', ui.lang) }}</dt>
          <dd class="cell-mono">{{ detail.proposal.attempts }}</dd>
          <dt>{{ t('actionsIdempotencyKey', ui.lang) }}</dt>
          <dd class="cell-mono actions-wrap">{{ detail.proposal.idempotency_key || '—' }}</dd>
          <dt>{{ t('actionsEvidence', ui.lang) }}</dt>
          <dd class="cell-mono actions-wrap">{{ evidenceText }}</dd>
        </dl>

        <div v-if="detail.proposal.error" class="actions-dialog-error cell-mono">
          {{ detail.proposal.error }}
        </div>
        <p v-if="detail.proposal.rationale" class="actions-rationale-block">
          {{ detail.proposal.rationale }}
        </p>

        <div class="actions-field-label">{{ t('actionsPayload', ui.lang) }}</div>
        <pre class="cell-mono actions-pre">{{ prettyPayload }}</pre>

        <div class="actions-field-label">{{ t('actionsApprovals', ui.lang) }}</div>
        <el-table :data="detail.approvals" size="small" class="admin-table">
          <template #empty><span class="dim">{{ t('actionsApprovalsEmpty', ui.lang) }}</span></template>
          <el-table-column :label="t('actionsApprover', ui.lang)" width="120">
            <template #default="{ row }"><span class="cell-mono">{{ row.user_id }}</span></template>
          </el-table-column>
          <el-table-column :label="t('actionsColActions', ui.lang)" width="100">
            <template #default="{ row }"><span class="cell-mono">{{ row.action }}</span></template>
          </el-table-column>
          <el-table-column :label="t('actionsComment', ui.lang)" min-width="140">
            <template #default="{ row }">{{ row.comment || '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('actionsDecidedAt', ui.lang)" width="150">
            <template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>

        <div class="actions-field-label">{{ t('actionsDeliveries', ui.lang) }}</div>
        <el-table :data="detail.deliveries" size="small" class="admin-table">
          <template #empty><span class="dim">{{ t('actionsDeliveriesEmpty', ui.lang) }}</span></template>
          <el-table-column :label="t('actionsChannel', ui.lang)" width="130">
            <template #default="{ row }"><span class="cell-mono">{{ row.channel }}</span></template>
          </el-table-column>
          <el-table-column :label="t('actionsStatus', ui.lang)" width="100">
            <template #default="{ row }">
              <!-- dry_run 是中性档:它什么都没发出去,不该长成绿色的「成功」。 -->
              <span class="pill" :class="row.status === 'failed' ? 'pill-danger' : row.status === 'ack' ? 'pill-accent' : row.status === 'dry_run' ? 'pill-neutral' : 'pill-ok'">
                {{ row.status }}
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('actionsHttpStatus', ui.lang)" width="80">
            <template #default="{ row }"><span class="cell-mono">{{ row.http_status ?? '—' }}</span></template>
          </el-table-column>
          <el-table-column :label="t('actionsResponse', ui.lang)" min-width="160">
            <template #default="{ row }">
              <span class="cell-mono actions-wrap">{{ row.response_excerpt || row.error || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('actionsAttemptedAt', ui.lang)" width="150">
            <template #default="{ row }">{{ fmtDateTime(row.attempted_at) }}</template>
          </el-table-column>
        </el-table>

        <!-- 效果验收(B7 闭环):至多一行 —— 一次测量一个结局。空态文案
             说清"为什么还没有",而不是留一张看起来像失败的空白表。 -->
        <div class="actions-field-label">{{ t('actionsEffects', ui.lang) }}</div>
        <el-table :data="detail.outcomes ?? []" size="small" class="admin-table">
          <template #empty>
            <span class="dim">{{ t('actionsEffectsEmpty', ui.lang) }}</span>
          </template>
          <el-table-column :label="t('actionsEffectsConclusion', ui.lang)" width="130">
            <template #default="{ row }">
              <span class="pill" :class="outcomeBandClass(row)">
                {{ outcomeLabel(row) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('actionsEffectsDelta', ui.lang)" width="130">
            <template #default="{ row }">
              <span class="cell-mono">{{ outcomeDelta(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('actionsEffectsWindow', ui.lang)" min-width="150">
            <template #default="{ row }">
              <span class="cell-mono">
                {{ row.window_start || '—' }} → {{ row.window_end || '—' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('actionsEffectsMethod', ui.lang)" width="90">
            <template #default="{ row }">
              <span class="cell-mono">{{ row.method || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('actionsEffectsMeasuredAt', ui.lang)" width="150">
            <template #default="{ row }">{{ fmtDateTime(row.measured_at) }}</template>
          </el-table-column>
        </el-table>
        <!-- 测量失败的原话要看得见(响亮,不重试):它是"为什么判不了"的唯一线索。 -->
        <div
          v-for="o in outcomeErrors"
          :key="o.id ?? o.measured_at"
          class="actions-dialog-error cell-mono"
        >
          {{ o.error }}
        </div>

        <div v-if="detailVerbs.length" class="actions-detail-verbs">
          <el-button
            v-for="v in detailVerbs"
            :key="v"
            size="small"
            :type="v === 'reject' || v === 'cancel' ? 'danger' : v === 'approve' || v === 'dispatch' ? 'primary' : 'default'"
            :plain="v !== 'approve' && v !== 'dispatch'"
            @click="ask(v, detail.proposal)"
          >
            {{ verbLabel(v) }}
          </el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Plus, RefreshCw } from 'lucide-vue-next'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { useListQuery } from '../../composables/useListQuery'
import { fmtDateTime } from '../../utils/format'
import { notifySuccess, toastError } from '../../utils/notify'
import TableEmpty from '../../components/admin/TableEmpty.vue'
import {
  ACTION_PROPOSAL_STATUSES,
  ACTION_PROPOSALS_ALL,
  ACTION_TABS,
  createActionTemplate,
  confirmActionTemplate,
  decideActionProposal,
  fetchActionProposal,
  fetchActionTemplates,
  fetchActionProposals,
  fetchOpenActionProposals,
  isOverdue,
  openProposalCount,
  outcomeBandClass,
  proposalStatusClass,
  proposalVerbs,
  rejectActionTemplate,
  riskClass,
  templateStatusClass,
  type ActionDecision,
  type ActionOutcome,
  type ActionProposal,
  type ActionProposalDetail,
  type ActionTab,
  type ActionTemplateEntry,
} from '../../api/actions'

const ui = useUiStore()

/* ── URL 状态(tab / status;深链来自治理收件箱)────────────── */

const { values } = useListQuery({ tab: 'templates', status: 'open' }, { mode: 'push' })

const tab = computed<ActionTab>(() =>
  (ACTION_TABS as readonly string[]).includes(values.tab)
    ? (values.tab as ActionTab)
    : 'templates',
)

function setTab(next: ActionTab) {
  if (tab.value === next) return
  values.tab = next
  // 模板页没有状态切片 —— 回到默认值(useListQuery 会把它从 URL 里摘掉)。
  values.status = next === 'proposals' ? values.status || 'open' : 'open'
}

/* ── 取数 ─────────────────────────────────────────────── */

const templates = ref<ActionTemplateEntry[]>([])
const channels = ref<string[]>([])
const sampleVars = ref<string[]>([])
const proposals = ref<ActionProposal[]>([])
const counts = ref<Record<string, number> | null>(null)
const enabled = ref<boolean | null>(null)
const loading = ref(false)
const listError = ref('')

/** 竞态护栏:快速切 Tab/筛选时,旧请求的响应不得覆盖新请求的结果。 */
let reqSeq = 0

async function load() {
  const seq = ++reqSeq
  loading.value = true
  listError.value = ''
  try {
    if (tab.value === 'templates') {
      const body = await fetchActionTemplates()
      if (seq !== reqSeq) return
      templates.value = body.templates ?? []
      channels.value = body.channels ?? []
      sampleVars.value = body.sample_variables ?? []
      enabled.value = body.enabled
    } else {
      const body =
        values.status === 'open'
          ? await fetchOpenActionProposals()
          : await fetchActionProposals({
              status: values.status === ACTION_PROPOSALS_ALL ? '' : values.status,
            })
      if (seq !== reqSeq) return
      proposals.value = body.proposals ?? []
      counts.value = body.counts ?? null
      enabled.value = body.enabled
    }
  } catch (e) {
    if (seq !== reqSeq) return
    if (tab.value === 'templates') templates.value = []
    else {
      proposals.value = []
      counts.value = null
    }
    listError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

const openCount = computed(() => openProposalCount(counts.value))

watch([tab, () => values.status], () => void load())

/* ── 文案 ─────────────────────────────────────────────── */

function riskLabel(risk: string): string {
  if (risk === 'high') return t('actionsRiskHigh', ui.lang)
  if (risk === 'medium') return t('actionsRiskMedium', ui.lang)
  return t('actionsRiskLow', ui.lang)
}

function templateStatusLabel(status: string): string {
  return status === 'confirmed'
    ? t('actionsTemplateStatusConfirmed', ui.lang)
    : t('actionsTemplateStatusPending', ui.lang)
}

/** 提案状态(与 api/actions.ts 的 pill 类同一处口径)。 */
const STATUS_KEY: Record<string, Parameters<typeof t>[0]> = {
  pending: 'actionsStatusPending',
  approved: 'actionsStatusApproved',
  rejected: 'actionsStatusRejected',
  expired: 'actionsStatusExpired',
  cancelled: 'actionsStatusCancelled',
  dispatched: 'actionsStatusDispatched',
  delivered: 'actionsStatusDelivered',
  failed: 'actionsStatusFailed',
}

function proposalStatusLabel(status: string): string {
  const key = STATUS_KEY[status]
  return key ? t(key, ui.lang) : status
}

const VERB_KEY: Record<ActionDecision, Parameters<typeof t>[0]> = {
  approve: 'actionsApprove',
  reject: 'actionsReject',
  cancel: 'actionsCancel',
  dispatch: 'actionsDispatch',
  retry: 'actionsRetry',
  dry_run: 'actionsDryRun',
  ack: 'actionsAck',
}

function verbLabel(v: ActionDecision): string {
  return t(VERB_KEY[v], ui.lang)
}

/** 测量失败的行(每提案至多一行)—— 原话渲染在表格下面。 */
const outcomeErrors = computed(() =>
  (detail.value?.outcomes ?? []).filter((o) => o.error),
)

/** 效果结论 → 文案。四档里 ``null``(判不了)与 ``false``(无变化)是
 *  两种事实,文案与 pill 类名(outcomeBandClass)都必须分开。 */
function outcomeLabel(o: ActionOutcome): string {
  if (o.error) return t('actionsEffectsFailed', ui.lang)
  if (o.outside_band === true) return t('actionsEffectsOutside', ui.lang)
  if (o.outside_band === false) return t('actionsEffectsNoChange', ui.lang)
  return t('actionsEffectsUndecided', ui.lang)
}

/** delta + pct;两者都可缺(判不了的那档),缺就 '—',不补零。 */
function outcomeDelta(o: ActionOutcome): string {
  if (o.delta == null && o.pct == null) return '—'
  const d = o.delta == null ? '' : String(Math.round(o.delta * 100) / 100)
  const p = o.pct == null ? '' : `${(o.pct * 100).toFixed(1)}%`
  return [d, p].filter(Boolean).join(' · ')
}

const VERB_HINT_KEY: Record<ActionDecision, Parameters<typeof t>[0]> = {
  approve: 'actionsHintApprove',
  reject: 'actionsHintReject',
  cancel: 'actionsHintCancel',
  dispatch: 'actionsHintDispatch',
  retry: 'actionsHintRetry',
  dry_run: 'actionsHintDryRun',
  ack: 'actionsHintAck',
}

/** `{{var}}` 不能在模板里字面量写(双花括号是插值定界符),包一层。 */
function varToken(name: string): string {
  return `{{${name}}}`
}

/* ── 模板:确认 / 拒绝 / 预览 ──────────────────────────── */

const previewOpen = ref(false)
const previewRow = ref<ActionTemplateEntry | null>(null)

function openPreview(row: ActionTemplateEntry) {
  previewRow.value = row
  previewOpen.value = true
}

async function confirmTemplate(row: ActionTemplateEntry) {
  try {
    const res = await confirmActionTemplate(row.name)
    // 注入扫描命中:不阻断,但确认的人必须看见(与确认一同返回)。
    if (res.injection_hits?.length) {
      previewRow.value = { ...row, status: 'confirmed', injection_hits: res.injection_hits }
      previewOpen.value = true
    }
    notifySuccess(t('actionsConfirmed', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

async function rejectTemplate(row: ActionTemplateEntry) {
  try {
    await rejectActionTemplate(row.name)
    notifySuccess(t('actionsRejected', ui.lang))
    await load()
  } catch (e) {
    toastError(e)
  }
}

/* ── 模板:新建 ───────────────────────────────────────── */

const createOpen = ref(false)
const saving = ref(false)
const createError = ref('')
const createHits = ref<string[]>([])
const form = ref({
  name: '',
  title: '',
  description: '',
  action_type: 'notify',
  channel: '',
  resource: '',
  risk: 'low',
  payload_template: '',
})

function openCreate() {
  createError.value = ''
  createHits.value = []
  form.value = {
    name: '',
    title: '',
    description: '',
    action_type: 'notify',
    channel: channels.value[0] ?? '',
    resource: '',
    risk: 'low',
    payload_template: '',
  }
  createOpen.value = true
}

async function submitCreate() {
  saving.value = true
  createError.value = ''
  createHits.value = []
  try {
    const res = await createActionTemplate({
      name: form.value.name.trim(),
      title: form.value.title.trim(),
      description: form.value.description.trim(),
      action_type: form.value.action_type,
      target: {
        channel: form.value.channel.trim(),
        ...(form.value.resource.trim() ? { resource: form.value.resource.trim() } : {}),
      },
      risk: form.value.risk,
      payload_template: form.value.payload_template,
    })
    createHits.value = res.injection_hits ?? []
    if (createHits.value.length) {
      // 命中注入样式内容时不自动关:让创建者当场读完再决定(模板已落盘,
      // 仍是 pending —— 关不掉任何东西,只是别让人没看见就离开)。
      notifySuccess(t('actionsCreatedToast', ui.lang))
      await load()
      return
    }
    notifySuccess(t('actionsCreatedToast', ui.lang))
    createOpen.value = false
    await load()
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

/* ── 提案:审批对话框 ─────────────────────────────────── */

const dialogOpen = ref(false)
const dialogVerb = ref<ActionDecision>('approve')
const dialogTarget = ref<ActionProposal | null>(null)
const comment = ref('')
const dialogError = ref('')
const busy = ref(false)

const commentRequired = computed(() => dialogVerb.value === 'reject')
const canSubmit = computed(() => !commentRequired.value || comment.value.trim().length > 0)

const dialogTitle = computed(() =>
  dialogTarget.value
    ? `${verbLabel(dialogVerb.value)} · ${dialogTarget.value.rule_id}`
    : verbLabel(dialogVerb.value),
)

const dialogHint = computed(() => t(VERB_HINT_KEY[dialogVerb.value], ui.lang))

function ask(verb: ActionDecision, p: ActionProposal) {
  dialogVerb.value = verb
  dialogTarget.value = p
  comment.value = ''
  dialogError.value = ''
  dialogOpen.value = true
}

async function submitDecision() {
  const target = dialogTarget.value
  if (!target || !canSubmit.value) return
  busy.value = true
  dialogError.value = ''
  try {
    await decideActionProposal(target.id, dialogVerb.value, comment.value.trim())
    notifySuccess(t('actionsDecided', ui.lang, verbLabel(dialogVerb.value)))
    dialogOpen.value = false
    await load()
    if (detailOpen.value && detail.value?.proposal.id === target.id) {
      await loadDetail(target.id)
    }
  } catch (e) {
    // 状态机的拒绝理由(「已经外送,取消不了」)是给人看的 —— 留在对话框里。
    dialogError.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

/* ── 提案:详情抽屉 ───────────────────────────────────── */

const detailOpen = ref(false)
const detail = ref<ActionProposalDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')

async function openDetail(p: ActionProposal) {
  detailOpen.value = true
  await loadDetail(p.id)
}

async function loadDetail(id: string) {
  detailLoading.value = true
  detailError.value = ''
  try {
    detail.value = await fetchActionProposal(id)
  } catch (e) {
    detail.value = null
    detailError.value = e instanceof Error ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

const prettyPayload = computed(() =>
  detail.value ? JSON.stringify(detail.value.proposal.payload, null, 2) : '',
)

const evidenceText = computed(() => {
  const refs = detail.value?.proposal.evidence_refs
  if (!refs || !Object.keys(refs).length) return '—'
  return Object.entries(refs)
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(' · ')
})

const detailVerbs = computed(() =>
  detail.value ? proposalVerbs(detail.value.proposal.status) : [],
)

onMounted(load)
</script>

<style scoped>
.view-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.tab-seg {
  display: inline-flex;
  border: 1px solid var(--border-default);
  border-radius: var(--r-sm);
  overflow: hidden;
}
.tab-seg .seg-btn {
  padding: 4px 12px;
  border: none;
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.tab-seg .seg-btn + .seg-btn {
  border-left: 1px solid var(--border-subtle);
}
.tab-seg .seg-btn.is-active {
  background: var(--accent);
  color: var(--on-accent);
  font-weight: 600;
}
.actions-banner {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.actions-banner.is-error {
  color: var(--danger-text, #d64545);
}
.actions-src {
  margin-left: 6px;
}
.actions-broken {
  margin-top: 2px;
  font-size: var(--fs-2xs);
  color: var(--danger-text, #d64545);
  white-space: normal;
}
.actions-time {
  color: var(--text-tertiary);
  white-space: nowrap;
}
.actions-rationale {
  font-size: var(--fs-2xs);
}
.actions-error-text {
  color: var(--danger-text, #d64545);
  font-size: var(--fs-2xs);
  white-space: normal;
}
.actions-filters {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 0 var(--sp-4) var(--sp-2);
}
.actions-filter-select {
  width: 180px;
}
.actions-frozen-hint {
  font-size: var(--fs-2xs);
}
.actions-preview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.actions-preview-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.actions-field-label {
  font-size: var(--fs-2xs);
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-top: var(--sp-2);
}
.actions-pre {
  margin: 0;
  padding: var(--sp-2);
  background: var(--surface-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  font-size: var(--fs-2xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 40vh;
  overflow: auto;
}
.actions-vars {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.actions-var {
  padding: 1px 6px;
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  border: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
}
.actions-hits {
  padding: var(--sp-2);
  border: 1px solid var(--el-color-warning);
  border-radius: var(--r-sm);
  font-size: var(--fs-2xs);
}
.actions-hits-title {
  font-weight: 600;
  color: var(--el-color-warning);
}
.actions-hits ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.actions-dialog-error {
  margin-top: 8px;
  padding: 8px 10px;
  font-size: var(--fs-2xs);
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  border-radius: 4px;
  white-space: pre-wrap;
}
.actions-dialog-target {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: var(--sp-2);
}
.actions-yaml :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--fs-2xs);
  line-height: 1.5;
}
.actions-detail {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.actions-detail-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.actions-dl {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 4px var(--sp-2);
  margin: 0;
  font-size: var(--fs-xs);
}
.actions-dl dt {
  color: var(--text-tertiary);
}
.actions-dl dd {
  margin: 0;
}
.actions-wrap {
  white-space: normal;
  word-break: break-all;
}
.actions-rationale-block {
  margin: 0;
  padding: var(--sp-2);
  background: var(--surface-muted);
  border-radius: var(--r-sm);
  font-size: var(--fs-xs);
}
.actions-detail-verbs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  padding-top: var(--sp-2);
}
</style>
