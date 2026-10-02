<!--
  KbView — single-datasource KB content operations (W3-K).

  The console standard, applied to the knowledge base (P6/P7):
    · tabs map one-to-one to the real assets under .trove/kb/<ds>/:
      pending / assets / entries / examples / lessons / rules;
    · every KPI is an entry point, not a read-only number;
    · ds, tab and every filter live in the URL (useListQuery) — a filtered
      queue is shareable, and a failed datasource switch clears the content
      instead of leaving the previous source's rows on screen;
    · the pending queue is decision cards: source, votes, confidence and
      evidence sit next to the buttons; single actions go through per-item
      endpoints whose receipts name the audit event;
    · bulk = N single actions: real progress, per-item failures listed with
      retry, and the same audit names as the single path;
    · the assets tab is adoption health: files are red when the mirror did
      not adopt them, with the reason and the three dispositions.
-->
<template>
  <div class="admin-view kb-page">
    <PageHeader
      :title="`${t('kb', ui.lang)}${values.ds ? ' · ' + values.ds : ''}`"
      :description="t('kbDesc', ui.lang)"
      :breadcrumbs="crumbs"
    >
      <template #actions>
        <el-select
          v-model="values.ds"
          class="ds-select"
          :aria-label="t('kbSelectDs', ui.lang)"
          :placeholder="t('kbSelectDs', ui.lang)"
        >
          <el-option
            v-for="d in connected"
            :key="d.name"
            :value="d.name"
            :label="d.default ? `${d.name} · default` : d.name"
          />
        </el-select>
        <el-button :loading="busy('reload')" :disabled="!values.ds" @click="reloadKb">
          <RefreshCw :size="15" class="btn-icon" />
          {{ t('dsReload', ui.lang) }}
        </el-button>
        <div class="more-wrap">
          <button
            type="button"
            class="icon-btn"
            :aria-label="t('kbMore', ui.lang)"
            :title="t('kbMore', ui.lang)"
            :aria-expanded="menuOpen ? 'true' : 'false'"
            @click="menuOpen = !menuOpen"
          >
            <MoreHorizontal :size="16" />
          </button>
          <div v-if="menuOpen" class="more-menu" role="menu">
            <button
              type="button"
              role="menuitem"
              class="menu-item"
              :disabled="!values.ds"
              @click="askMenuAction('init')"
            >
              <Sparkles :size="14" />
              {{ t('dsInit', ui.lang) }}
            </button>
            <button
              type="button"
              role="menuitem"
              class="menu-item"
              :disabled="!values.ds || !initialized"
              @click="askMenuAction('merge')"
            >
              <Recycle :size="14" />
              {{ t('kbReinit', ui.lang) }}
            </button>
            <button
              type="button"
              role="menuitem"
              class="menu-item is-danger"
              :disabled="!values.ds || !initialized"
              @click="askMenuAction('overwrite')"
            >
              <TriangleAlert :size="14" />
              {{ t('kbOverwrite', ui.lang) }}
            </button>
            <button
              type="button"
              role="menuitem"
              class="menu-item is-danger"
              :disabled="!values.ds || !initialized"
              @click="askMenuAction('delete')"
            >
              <Trash2 :size="14" />
              {{ t('kbDelete', ui.lang) }}
            </button>
          </div>
        </div>
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

    <!-- init / reload / bulk progress -->
    <div v-if="busy('init')" class="kb-init-progress" role="status">
      <el-progress :percentage="initProgress?.progress || 0" :stroke-width="8" />
      <div class="kb-init-stage">
        {{ initStageLabel(initProgress?.stage) }}
        <span v-if="initProgress?.detail" class="cell-muted">{{ initProgress?.detail }}</span>
      </div>
    </div>

    <!-- load failed: content is cleared, never the previous source's data -->
    <StatePanel
      v-if="loadError"
      mode="error"
      :title="t('kbErrorTitle', ui.lang)"
      :description="t('kbErrorDesc', ui.lang)"
      :detail="loadError"
      :retry-text="t('retry', ui.lang)"
      @retry="loadAll"
    />

    <!-- first paint -->
    <StatePanel
      v-else-if="loading && !detail"
      mode="loading"
      :title="t('kbLoading', ui.lang)"
    />

    <!-- never initialized: the first-run empty state carries the CTA -->
    <StatePanel
      v-else-if="detail && !initialized"
      mode="empty"
      :title="t('kbNotInitTitle', ui.lang)"
      :description="t('kbNotInitDesc', ui.lang)"
    >
      <template #action>
        <el-button type="primary" :loading="busy('init')" @click="askMenuAction('init')">
          <Sparkles :size="15" class="btn-icon" />
          {{ t('dsInit', ui.lang) }}
        </el-button>
      </template>
    </StatePanel>

    <template v-else-if="detail">
      <div class="kb-tabs" role="tablist" :aria-label="t('kb', ui.lang)">
        <button
          v-for="tabDef in tabDefs"
          :key="tabDef.key"
          type="button"
          class="kb-tab"
          role="tab"
          :class="{ 'is-active': values.tab === tabDef.key }"
          :aria-selected="values.tab === tabDef.key ? 'true' : 'false'"
          @click="switchTab(tabDef.key)"
        >
          {{ tabDef.label }}
          <span class="tab-badge">{{ tabDef.count }}</span>
        </button>
      </div>

      <!-- ── 待审批: lessons (votes / distillation) + pending example drafts ── -->
      <section v-if="values.tab === 'pending'" class="kb-pane">
        <div class="list-toolbar">
          <el-select v-model="values.kind" class="filter-select">
            <el-option :label="t('kbQueueAllKinds', ui.lang)" value="" />
            <el-option
              :label="t('kbQueueLessonsOnly', ui.lang, pendingLessons.length)"
              value="lesson"
            />
            <el-option
              :label="t('kbQueueExamplesOnly', ui.lang, pendingExamples.length)"
              value="example"
            />
          </el-select>
          <el-select v-model="values.src" class="filter-select">
            <el-option :label="t('kbFilterSource', ui.lang)" value="" />
            <el-option :label="t('kbSrcVote', ui.lang)" value="vote" />
            <el-option :label="t('kbSrcDistill', ui.lang)" value="distill" />
            <el-option :label="t('kbSrcDraft', ui.lang)" value="draft" />
            <el-option :label="t('kbSrcOther', ui.lang)" value="other" />
          </el-select>
          <el-select v-model="values.sort" class="filter-select">
            <el-option :label="t('kbSortNewest', ui.lang)" value="newest" />
            <el-option :label="t('kbSortVotes', ui.lang)" value="votes" />
            <el-option :label="t('kbSortConfidence', ui.lang)" value="confidence" />
          </el-select>
          <span class="spacer" />
          <span class="view-count">{{ t('kbItemsCount', ui.lang, queue.length) }}</span>
        </div>

        <div v-if="selected.length" class="bulk-bar" role="status">
          <span class="bulk-count">{{ t('kbSelectedCount', ui.lang, selected.length) }}</span>
          <el-button size="small" type="primary" :loading="bulkBusy" @click="confirmSelected">
            {{ t('kbConfirmSelected', ui.lang) }}
          </el-button>
          <el-button size="small" type="danger" plain :disabled="bulkBusy" @click="askRejectSelected">
            {{ t('kbRejectSelected', ui.lang) }}
          </el-button>
          <span v-if="bulkProgress" class="bulk-progress">
            {{ bulkProgress.done }} / {{ bulkProgress.total }}
          </span>
          <span class="spacer" />
          <el-button size="small" text :disabled="bulkBusy" @click="selected = []">
            {{ t('kbClearSelection', ui.lang) }}
          </el-button>
        </div>

        <div v-if="failedHandled.length && bulkFailuresVisible" class="partial-panel" role="alert">
          <div class="partial-title">{{ t('kbBulkPartial', ui.lang) }}</div>
          <div v-for="h in failedHandled" :key="h.id" class="partial-row">
            <span class="partial-name">{{ h.title }}</span>
            <span class="partial-error">{{ h.error }}</span>
            <el-button size="small" @click="retryHandled(h)">{{ t('kbBulkRetry', ui.lang) }}</el-button>
          </div>
        </div>

        <StatePanel
          v-if="!queue.length"
          mode="empty"
          :title="queueFiltered ? t('kbQueueFilteredEmpty', ui.lang) : t('kbQueueEmptyTitle', ui.lang)"
          :description="queueFiltered ? '' : t('kbQueueEmptyDesc', ui.lang)"
        >
          <template #action>
            <el-button v-if="queueFiltered" size="small" @click="clearQueueFilters">
              {{ t('kbClearFilters', ui.lang) }}
            </el-button>
          </template>
        </StatePanel>

        <div v-else class="queue-list">
          <DiffCard
            v-for="item in queue"
            :key="item.id"
            :kind="item.kind === 'lesson' ? t('kbKindLesson', ui.lang) : t('kbKindExample', ui.lang)"
            :title="item.title"
            :meta="itemMeta(item)"
            :selected="selected.includes(item.id)"
            :busy="!!busyIds[item.id]"
            :refused-reason="refused[item.id] || ''"
            :confirm-label="t('confirm', ui.lang)"
            :reject-label="t('kbReject', ui.lang)"
            :edit-label="item.kind === 'lesson' ? t('kbEditConfirm', ui.lang) : ''"
            :select-label="t('kbSelectedCount', ui.lang, 1)"
            :refused-label="t('kbRefusedByGateLabel', ui.lang)"
            @toggle="toggleSelect(item.id)"
            @confirm="confirmCard(item)"
            @reject="askReject(item)"
            @edit-confirm="openEdit(item)"
          >
            <template v-if="item.note" #evidence>
              <strong>{{ t('kbNote', ui.lang) }}</strong> {{ item.note }}
              <template v-if="item.evidence">
                <br>
                <strong>{{ t('kbEvidence', ui.lang) }}</strong> {{ item.evidence }}
              </template>
            </template>
            <template #impact>
              {{ item.kind === 'lesson' ? t('kbImpactLessonConfirm', ui.lang) : t('kbImpactExampleConfirm', ui.lang) }}
            </template>
          </DiffCard>
        </div>

        <section v-if="handled.length" class="handled-sec">
          <div class="handled-head">
            <h3 class="handled-title">{{ t('kbJustHandled', ui.lang) }}</h3>
            <button
              type="button"
              class="seg-btn"
              :class="{ 'is-active': handledFilter === '' }"
              @click="handledFilter = ''"
            >
              {{ t('kbShowAll', ui.lang) }}
            </button>
            <button
              type="button"
              class="seg-btn"
              :class="{ 'is-active': handledFilter === 'failed' }"
              @click="handledFilter = 'failed'"
            >
              {{ t('kbShowFailed', ui.lang) }}
            </button>
          </div>
          <p v-if="!visibleHandled.length" class="dsec-note">{{ t('kbHandledEmpty', ui.lang) }}</p>
          <div v-for="h in visibleHandled" :key="h.id" class="handled-row">
            <span class="handled-badge" :class="h.status === 'ok' ? 'is-ok' : 'is-failed'">
              {{ h.status === 'ok'
                ? (h.action === 'confirm' ? t('kbReceiptConfirmed', ui.lang, h.audit) : t('kbReceiptRejected', ui.lang, h.audit))
                : t('kbHandledFailed', ui.lang) }}
            </span>
            <span class="handled-name">{{ h.title }}</span>
            <span v-if="h.error" class="handled-error">{{ h.error }}</span>
            <el-button v-if="h.status !== 'ok'" size="small" @click="retryHandled(h)">
              {{ t('kbBulkRetry', ui.lang) }}
            </el-button>
          </div>
        </section>
      </section>

      <!-- ── 资产: per-file adoption health + dispositions ── -->
      <section v-else-if="values.tab === 'assets'" class="kb-pane kb-assets">
        <div class="list-toolbar">
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.prob !== '1' }"
            @click="values.prob = ''"
          >
            {{ t('kbShowAll', ui.lang) }}
          </button>
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.prob === '1' }"
            @click="values.prob = '1'"
          >
            {{ t('kbShowFailed', ui.lang) }}
          </button>
          <span class="spacer" />
          <span class="view-count">{{ t('kbItemsCount', ui.lang, assetRows.length) }}</span>
        </div>
        <p class="toolbar-note">{{ t('kbAssetFrom', ui.lang) }}</p>

        <StatePanel
          v-if="!assetRows.length"
          mode="empty"
          :title="t('kbKpiAssetsSubNone', ui.lang)"
        />

        <DataTable
          v-else
          :columns="assetColumns"
          :rows="assetRows"
          row-key="file"
          row-clickable
          @row-click="openAsset"
        >
          <template #cell-file="{ row }">
            <span class="asset-file" :class="{ 'is-refused': !!(row as KbAssetRow).refusedReason }">
              {{ (row as KbAssetRow).file }}
            </span>
          </template>
          <template #cell-format="{ row }">
            <span class="cell-mono">{{ (row as KbAssetRow).format ?? '—' }}</span>
          </template>
          <template #cell-generated_at="{ row }">
            <span class="cell-time">{{ fmtDateTime((row as KbAssetRow).generated_at) || '—' }}</span>
          </template>
          <template #cell-edited="{ row }">
            <span class="cell-muted">{{ editedLabel((row as KbAssetRow).edited) }}</span>
          </template>
          <template #cell-has_baseline="{ row }">
            <span class="cell-muted">
              {{ (row as KbAssetRow).has_baseline ? t('kbBaselineYes', ui.lang) : t('kbBaselineNo', ui.lang) }}
            </span>
          </template>
          <template #cell-adoption="{ row }">
            <span v-if="(row as KbAssetRow).error" class="adoption-badge is-refused">
              {{ (row as KbAssetRow).error }}
            </span>
            <span
              v-else-if="(row as KbAssetRow).refusedReason"
              class="adoption-badge is-refused"
              :title="(row as KbAssetRow).refusedReason"
            >
              {{ t('kbRefused', ui.lang) }}
            </span>
            <span v-else class="adoption-badge is-ok">{{ t('kbAdopted', ui.lang) }}</span>
          </template>
        </DataTable>
      </section>

      <!-- ── 术语与指标: metrics / dimensions / tables from the semantic model ── -->
      <section v-else-if="values.tab === 'entries'" class="kb-pane">
        <div class="list-toolbar">
          <el-input
            v-model="values.q"
            class="toolbar-search"
            :prefix-icon="Search"
            :placeholder="t('kbSearchEntries', ui.lang)"
            clearable
          />
          <span class="spacer" />
          <span class="view-count">{{ t('kbItemsCount', ui.lang, filteredEntries.length) }}</span>
          <el-button type="primary" @click="openTerm">
            <Plus :size="15" class="btn-icon" />
            {{ t('kbAddTerm', ui.lang) }}
          </el-button>
        </div>

        <StatePanel
          v-if="!entries.length"
          mode="empty"
          :title="t('kbEntriesEmpty', ui.lang)"
        />

        <DataTable v-else :columns="entryColumns" :rows="filteredEntries" row-key="key">
          <template #empty>
            <StatePanel mode="empty" :title="t('kbEntriesFilteredEmpty', ui.lang)">
              <template #action>
                <el-button size="small" @click="values.q = ''">{{ t('kbClearFilters', ui.lang) }}</el-button>
              </template>
            </StatePanel>
          </template>
          <template #cell-name="{ row }">
            <span class="entry-name">{{ (row as KbSemanticEntry).name || (row as KbSemanticEntry).key }}</span>
          </template>
          <template #cell-kind="{ row }">
            <span class="tag-pill" :class="`is-${(row as KbSemanticEntry).kind}`">
              {{ entryKindLabel((row as KbSemanticEntry).kind) }}
            </span>
          </template>
          <template #cell-synonyms="{ row }">
            <span v-if="synonymsText(row as KbSemanticEntry)" class="cell-muted">
              {{ synonymsText(row as KbSemanticEntry) }}
            </span>
            <span v-else class="cell-muted">—</span>
          </template>
          <template #cell-expression="{ row }">
            <code v-if="expressionText(row as KbSemanticEntry)" class="cell-mono">
              {{ expressionText(row as KbSemanticEntry) }}
            </code>
            <span v-else class="cell-muted">—</span>
          </template>
          <template #cell-datasets="{ row }">
            <span class="cell-muted">{{ datasetsText(row as KbSemanticEntry) }}</span>
          </template>
          <template #cell-definition="{ row }">
            <span class="cell-muted">
              {{ (row as KbSemanticEntry).definition || (row as KbSemanticEntry).description || '—' }}
            </span>
          </template>
        </DataTable>
      </section>

      <!-- ── 示例: certified assets and the drafts waiting for review ── -->
      <section v-else-if="values.tab === 'examples'" class="kb-pane">
        <div class="list-toolbar">
          <el-input
            v-model="values.q"
            class="toolbar-search"
            :prefix-icon="Search"
            :placeholder="t('kbSearchExamples', ui.lang)"
            clearable
          />
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.status === '' }"
            @click="values.status = ''"
          >
            {{ t('kbExAll', ui.lang, exampleRows.length) }}
          </button>
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.status === 'pending' }"
            @click="values.status = 'pending'"
          >
            {{ t('kbExPending', ui.lang, pendingExamples.length) }}
          </button>
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.status === 'certified' }"
            @click="values.status = 'certified'"
          >
            {{ t('kbExCertified', ui.lang) }}
          </button>
          <span class="spacer" />
          <el-button type="primary" @click="openExample">
            <Plus :size="15" class="btn-icon" />
            {{ t('kbAddExample', ui.lang) }}
          </el-button>
        </div>

        <StatePanel v-if="!exampleRows.length" mode="empty" :title="t('kbExamplesEmpty', ui.lang)" />

        <DataTable v-else :columns="exampleColumns" :rows="visibleExamples" row-key="id">
          <template #empty>
            <StatePanel mode="empty" :title="t('kbEntriesFilteredEmpty', ui.lang)" />
          </template>
          <template #cell-sql="{ row }">
            <button
              v-if="(row as ExampleRow).sql"
              type="button"
              class="sql-snippet"
              :title="t('copy', ui.lang)"
              @click="copySnippet((row as ExampleRow).sql)"
            >
              <code>{{ (row as ExampleRow).sql }}</code>
            </button>
            <span v-else class="cell-muted">—</span>
          </template>
          <template #cell-tags="{ row }">
            <span v-if="(row as ExampleRow).template" class="kb-chip">template</span>
            <span v-for="tag in (row as ExampleRow).tags" :key="tag" class="kb-chip">{{ tag }}</span>
            <span
              v-if="!(row as ExampleRow).tags.length && !(row as ExampleRow).template"
              class="cell-muted"
            >—</span>
          </template>
          <template #cell-provenance="{ row }">
            <span class="cell-muted">{{ (row as ExampleRow).provenance || '—' }}</span>
          </template>
          <template #cell-status="{ row }">
            <span
              class="tag-pill"
              :class="(row as ExampleRow).status === 'pending' ? 'is-pending' : 'is-ok'"
            >
              {{ exampleStatusLabel((row as ExampleRow).status) }}
            </span>
          </template>
          <template #cell-actions="{ row }">
            <div v-if="(row as ExampleRow).status === 'pending'" class="row-actions">
              <button
                type="button"
                class="mini-btn icon primary"
                :title="t('confirm', ui.lang)"
                @click.stop="confirmExampleRow(row as ExampleRow)"
              >
                <Check :size="13" />
              </button>
              <button
                type="button"
                class="mini-btn icon is-danger"
                :title="t('kbReject', ui.lang)"
                @click.stop="askRejectExampleRow(row as ExampleRow)"
              >
                <X :size="13" />
              </button>
            </div>
          </template>
        </DataTable>
      </section>

      <!-- ── 教训: the whole Hint Bank, pending and confirmed ── -->
      <section v-else-if="values.tab === 'lessons'" class="kb-pane">
        <div class="list-toolbar">
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.status === '' }"
            @click="values.status = ''"
          >
            {{ t('kbLesAll', ui.lang, lessons.length) }}
          </button>
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.status === 'pending' }"
            @click="values.status = 'pending'"
          >
            {{ t('kbLesPending', ui.lang, pendingLessons.length) }}
          </button>
          <button
            type="button"
            class="seg-btn"
            :class="{ 'is-active': values.status === 'confirmed' }"
            @click="values.status = 'confirmed'"
          >
            {{ t('kbLesConfirmed', ui.lang, confirmedLessons.length) }}
          </button>
          <span class="spacer" />
          <span class="view-count">{{ t('kbItemsCount', ui.lang, visibleLessons.length) }}</span>
        </div>

        <StatePanel v-if="!lessons.length" mode="empty" :title="t('kbQueueEmptyTitle', ui.lang)" />

        <DataTable v-else :columns="lessonColumns" :rows="visibleLessons" row-key="id">
          <template #cell-note="{ row }">
            <span class="cell-muted">{{ (row as LessonRow).note || '—' }}</span>
          </template>
          <template #cell-source="{ row }">
            <span class="kb-chip">{{ (row as LessonRow).sourceLabel }}</span>
          </template>
          <template #cell-votes="{ row }">
            <span class="cell-muted">
              ▲ {{ (row as LessonRow).upvotes }} · ▼ {{ (row as LessonRow).downvotes }}
            </span>
          </template>
          <template #cell-time="{ row }">
            <span class="cell-time" :title="(row as LessonRow).time">
              {{ fmtDateTime((row as LessonRow).time) || '—' }}
            </span>
          </template>
          <template #cell-status="{ row }">
            <span class="tag-pill" :class="(row as LessonRow).confirmed ? 'is-ok' : 'is-pending'">
              {{ (row as LessonRow).confirmed ? t('kbStatusConfirmed', ui.lang) : t('kbStatusPending', ui.lang) }}
            </span>
          </template>
        </DataTable>
      </section>

      <!-- ── 规则: human-authored, no write path — read-only by design ── -->
      <section v-else class="kb-pane">
        <p class="toolbar-note">{{ t('kbRulesReadonly', ui.lang) }}</p>
        <StatePanel v-if="!rules.length" mode="empty" :title="t('kbRulesEmpty', ui.lang)" />
        <ul v-else class="rule-list">
          <li v-for="(r, i) in rules" :key="i" class="rule-item">{{ r }}</li>
        </ul>
      </section>
    </template>

    <!-- ── single / bulk reject: blast radius before the delete ── -->
    <ConfirmDialog
      v-model="rejectOpen"
      :title="t('kbRejectConfirmTitle', ui.lang, rejectTargets.length)"
      :confirm-text="t('kbReject', ui.lang)"
      :cancel-text="t('cancel', ui.lang)"
      danger
      :loading="rejecting"
      @confirm="doReject"
    >
      <p class="confirm-names">{{ rejectTargets.map((x) => x.title).join(' · ') }}</p>
      <template #impact>
        <ul class="impact-list">
          <li v-if="rejectHasKind('lesson')">{{ t('kbImpactLessonReject', ui.lang) }}</li>
          <li v-if="rejectHasKind('example')">{{ t('kbImpactExampleReject', ui.lang) }}</li>
        </ul>
      </template>
    </ConfirmDialog>

    <!-- ── edit then confirm (lessons) ── -->
    <el-dialog
      v-model="editOpen"
      :title="t('kbEditConfirm', ui.lang)"
      width="520"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="saveEdit">
        <el-form-item :label="t('kbEditNote', ui.lang)">
          <el-input v-model="editNote" type="textarea" :rows="3" />
        </el-form-item>
        <p class="form-hint">{{ t('kbImpactLessonConfirm', ui.lang) }}</p>
      </el-form>
      <template #footer>
        <el-button @click="editOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="editSaving" @click="saveEdit">
          {{ t('kbSaveConfirm', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── init / merge / overwrite / delete: one dialog, four blast radii ── -->
    <ConfirmDialog
      v-if="menuAction"
      v-model="menuOpen2"
      :title="menuTitle"
      :confirm-text="menuConfirmText"
      :cancel-text="t('cancel', ui.lang)"
      :danger="menuAction === 'overwrite' || menuAction === 'delete'"
      :loading="busy(menuAction)"
      @confirm="runMenuAction"
    >
      <template #impact>
        <ul class="impact-list">
          <li v-for="(line, i) in menuImpact" :key="i">{{ line }}</li>
        </ul>
      </template>
    </ConfirmDialog>

    <!-- ── asset detail / dispositions ── -->
    <AssetDrawer
      v-model="assetOpen"
      :title="assetDrawerTitle"
      :close-label="t('close', ui.lang)"
      :aria-label="t('kbDrawerAria', ui.lang)"
      :fields="assetFields"
      :notes="assetNotes"
      :disposition-label="t('kbAssetDisposition', ui.lang)"
      :dispositions="assetDispositions"
      @disposition="onDisposition"
    />

    <!-- ── add term ── -->
    <el-dialog
      v-model="termOpen"
      :title="t('kbAddTerm', ui.lang)"
      width="520"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="addTerm">
        <el-form-item
          :label="t('kbTermField', ui.lang)"
          prop="term"
          :error="termErrors.term"
        >
          <el-input v-model="termForm.term" :placeholder="t('kbTermField', ui.lang)" />
        </el-form-item>
        <el-form-item :label="`${t('kbAliases', ui.lang)} · ${t('kbOptional', ui.lang)}`">
          <el-input v-model="termForm.aliases" />
        </el-form-item>
        <el-form-item
          :label="t('kbMapping', ui.lang)"
          prop="mapping"
          :error="termErrors.mapping"
        >
          <el-input
            v-model="termForm.mapping"
            :placeholder="t('kbMappingHint', ui.lang)"
            class="mono-input"
          />
          <div class="form-hint">{{ t('kbMappingHint', ui.lang) }}</div>
        </el-form-item>
        <el-form-item :label="`${t('kbTables', ui.lang)} · ${t('kbOptional', ui.lang)}`">
          <el-input v-model="termForm.tables" />
        </el-form-item>
        <el-form-item :label="`${t('kbDefinition', ui.lang)} · ${t('kbOptional', ui.lang)}`">
          <el-input v-model="termForm.definition" type="textarea" :rows="2" />
        </el-form-item>
        <div v-if="termError" class="form-error">
          <span>{{ termError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="termOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="acting" @click="addTerm">
          {{ t('confirm', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ── add example ── -->
    <el-dialog
      v-model="exampleOpen"
      :title="t('kbAddExample', ui.lang)"
      width="560"
      class="admin-dialog"
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="addExample">
        <el-form-item :label="t('kbQuestion', ui.lang)" prop="question" :error="exErrors.question">
          <el-input v-model="exForm.question" />
        </el-form-item>
        <el-form-item :label="t('sql', ui.lang)" prop="sql" :error="exErrors.sql">
          <el-input
            v-model="exForm.sql"
            type="textarea"
            :rows="4"
            spellcheck="false"
            class="mono-input"
          />
        </el-form-item>
        <el-form-item :label="`${t('kbTags', ui.lang)} · ${t('kbOptional', ui.lang)}`">
          <el-input v-model="exForm.tags" />
        </el-form-item>
        <div v-if="exError" class="form-error">
          <span>{{ exError }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="exampleOpen = false">{{ t('cancel', ui.lang) }}</el-button>
        <el-button type="primary" :loading="acting" @click="addExample">
          {{ t('confirm', ui.lang) }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Check,
  MoreHorizontal,
  Plus,
  Recycle,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import { apiDelete, apiGet, apiPost, ApiError } from '../../api/http'
import type {
  DatasourceInfo,
  KbAsset,
  KbDetail,
  KbLesson,
  KbPendingExample,
  KbSemanticEntry,
} from '../../api/types'
import { useUiStore } from '../../stores/ui'
import { t } from '../../i18n'
import { notifySuccess, toastError } from '../../utils/notify'
import { copyText, fmtDateTime } from '../../utils/format'
import { useListQuery } from '../../composables/useListQuery'
import PageHeader from '../../components/base/PageHeader.vue'
import KpiTile from '../../components/base/KpiTile.vue'
import StatePanel from '../../components/base/StatePanel.vue'
import DataTable, { type DataTableColumn } from '../../components/base/DataTable.vue'
import ConfirmDialog from '../../components/base/ConfirmDialog.vue'
import DiffCard from '../../components/kb/DiffCard.vue'
import AssetDrawer from '../../components/kb/AssetDrawer.vue'

type TabKey = 'pending' | 'assets' | 'entries' | 'examples' | 'lessons' | 'rules'
type KpiKey = TabKey

interface QueueItem {
  id: string
  kind: 'lesson' | 'example'
  title: string
  note: string
  sql: string
  sourceBucket: string
  sourceLabel: string
  votes: string
  /** Numeric votes for ordering; examples have none (-1 sorts last). */
  voteCount: number
  confidence: string
  time: string
  evidence: string
  /** lesson: pattern-or-question key; example: question + sql. */
  key: string
  question: string
}

interface HandledItem {
  id: string
  title: string
  status: 'ok' | 'failed'
  action: 'confirm' | 'reject'
  audit: string
  error: string
  item: QueueItem
}

interface KbAssetRow extends KbAsset {
  rel: string
  refusedReason: string
}

interface ExampleRow {
  id: string
  question: string
  sql: string
  tags: string[]
  template: boolean
  status: string
  provenance: string
  createdAt: string
}

interface LessonRow {
  id: string
  pattern: string
  note: string
  sql: string
  sourceLabel: string
  sourceBucket: string
  confidence: number | null
  upvotes: number
  downvotes: number
  time: string
  confirmed: boolean
}

const ui = useUiStore()

/* ── state in the URL: ds, tab and every filter ─────────────────────────── */
const { values } = useListQuery({
  ds: '',
  tab: 'pending',
  kind: '',
  src: '',
  sort: 'newest',
  prob: '',
  q: '',
  status: '',
})

const datasources = ref<DatasourceInfo[]>([])
const detail = ref<KbDetail | null>(null)
const entries = ref<KbSemanticEntry[]>([])
const pendingExamples = ref<KbPendingExample[]>([])
const loading = ref(false)
const loadError = ref('')
const acting = ref(false)
const menuOpen = ref(false)
const menuAction = ref<'' | 'init' | 'merge' | 'overwrite' | 'delete'>('')
const menuOpen2 = ref(false)
const busyMap: Record<string, boolean> = {}

const selected = ref<string[]>([])
const busyIds = ref<Record<string, boolean>>({})
const refused = ref<Record<string, string>>({})
const bulkBusy = ref(false)
const bulkProgress = ref<{ done: number; total: number } | null>(null)
const handled = ref<HandledItem[]>([])
const handledFilter = ref<'' | 'failed'>('')
const rejectTargets = ref<QueueItem[]>([])
const rejectOpen = ref(false)
const rejecting = ref(false)

const editOpen = ref(false)
const editTarget = ref<QueueItem | null>(null)
const editNote = ref('')
const editSaving = ref(false)

const assetOpen = ref(false)
const assetSel = ref<KbAssetRow | null>(null)

const initProgress = ref<{ stage?: string; progress?: number; detail?: string } | null>(null)

const connected = computed(() => datasources.value.filter((d) => d.status === 'connected'))
const initialized = computed(() => !!detail.value?.status.initialized)
const assets = computed(() => (detail.value?.status.assets ?? []) as KbAsset[])
const refusedMap = computed(() => detail.value?.status.refused_assets ?? {})
const lessons = computed(() => detail.value?.lessons ?? [])
const rules = computed(() => detail.value?.rules ?? [])
const pendingLessons = computed(() => lessons.value.filter((l) => !l.confirmed))
const confirmedLessons = computed(() => lessons.value.filter((l) => l.confirmed))

function busy(name: string): boolean {
  return !!busyMap[name]
}
function setBusy(name: string, v: boolean) {
  if (v) busyMap[name] = true
  else delete busyMap[name]
}

const crumbs = computed(() => [
  { label: t('admin', ui.lang), to: '/admin' },
  { label: t('datasources', ui.lang), to: '/admin/datasources' },
  { label: `${t('kb', ui.lang)}${values.ds ? ' · ' + values.ds : ''}` },
])

/* ── loading ────────────────────────────────────────────────────────────── */

async function loadDatasources() {
  try {
    const body = await apiGet('/v1/admin/datasources')
    datasources.value = body.datasources ?? []
  } catch (e) {
    datasources.value = []
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}

/** One load for one datasource. A failure clears the content — showing the
 *  previous source's rows under a new name is the one thing forbidden here. */
async function loadAll() {
  const ds = values.ds
  if (!ds) return
  loading.value = true
  loadError.value = ''
  try {
    const [kbBody, entriesBody, pendingBody] = await Promise.all([
      apiGet(`/v1/admin/datasources/${encodeURIComponent(ds)}/kb`),
      apiGet(`/v1/kb/entries?datasource=${encodeURIComponent(ds)}`),
      apiGet(`/v1/kb/examples/pending?datasource=${encodeURIComponent(ds)}`),
    ])
    if (values.ds !== ds) return
    detail.value = kbBody.kb
    entries.value = entriesBody.entries ?? []
    pendingExamples.value = pendingBody.examples ?? []
    // prune the selection to what still exists in this datasource
    const live = new Set(queueSource.value.map((i) => i.id))
    selected.value = selected.value.filter((id) => live.has(id))
  } catch (e) {
    if (values.ds !== ds) return
    detail.value = null
    entries.value = []
    pendingExamples.value = []
    selected.value = []
    handled.value = []
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (values.ds === ds) loading.value = false
  }
}

watch(
  () => values.ds,
  () => {
    selected.value = []
    handled.value = []
    refused.value = {}
    void loadAll()
  },
)

onMounted(async () => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onDocumentKeydown)
  await loadDatasources()
  if (!values.ds && connected.value.length) {
    const dflt = connected.value.find((d) => d.default) ?? connected.value[0]
    values.ds = dflt.name
    return // the watcher owns the load
  }
  await loadAll()
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onDocumentKeydown)
})

/* The ⋯ menu is the one transient popover on the page: a click anywhere
 * outside it, or Esc, closes it (a menu that only closes on its own items
 * is the classic way to leave a stale popover open). */
function onDocumentClick(e: MouseEvent) {
  if (!menuOpen.value) return
  const target = e.target as HTMLElement | null
  if (target?.closest('.more-wrap')) return
  menuOpen.value = false
}

function onDocumentKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') menuOpen.value = false
}

/* ── queue items: lessons (pending) + example drafts ────────────────────── */

function lessonSourceLabel(src?: string): string {
  const s = (src || '').trim()
  if (s === 'user_feedback') return t('kbSrcVote', ui.lang)
  if (s === 'auto_failure') return t('kbSrcDistill', ui.lang)
  if (s === 'correction') return t('kbSrcCorrection', ui.lang)
  if (s === 'manual' || !s) return t('kbSrcManual', ui.lang)
  return s // unknown provenance is shown as recorded, never guessed
}

function lessonSourceBucket(src?: string): string {
  const s = (src || '').trim()
  if (s === 'user_feedback') return 'vote'
  if (s === 'auto_failure' || s === 'correction') return 'distill'
  return 'other'
}

const queueSource = computed<QueueItem[]>(() => {
  const items: QueueItem[] = []
  for (const l of pendingLessons.value as KbLesson[]) {
    const key = (l.pattern || l.question || '').trim()
    if (!key) continue
    items.push({
      id: `lesson:${key}`,
      kind: 'lesson',
      title: (l.question || l.pattern || '').trim() || key,
      note: l.note || '',
      sql: l.sql_snippet || '',
      sourceBucket: lessonSourceBucket(l.source),
      sourceLabel: lessonSourceLabel(l.source),
      votes: `▲ ${l.upvotes ?? 0} · ▼ ${l.downvotes ?? 0}`,
      voteCount: l.upvotes ?? 0,
      confidence:
        typeof l.confidence === 'number' ? `${Math.round(l.confidence * 100)}%` : '—',
      time: l.created_at || l.updated_at || '',
      evidence: l.evidence || '',
      key,
      question: l.question || '',
    })
  }
  for (const ex of pendingExamples.value) {
    const q = (ex.question || '').trim()
    if (!q) continue
    const auto = (ex.tags ?? []).includes('auto')
    items.push({
      id: `example:${q}\u0000${ex.sql || ''}`,
      kind: 'example',
      title: q,
      note: ex.note || '',
      sql: ex.sql || '',
      sourceBucket: auto ? 'other' : 'draft',
      sourceLabel: auto ? t('kbSrcAutoDraft', ui.lang) : t('kbSrcDraft', ui.lang),
      votes: '—',
      voteCount: -1,
      confidence: '—',
      time: ex.created_at || '',
      evidence: '',
      key: q,
      question: q,
    })
  }
  return items
})

const queue = computed<QueueItem[]>(() => {
  let items = queueSource.value
  if (values.kind) items = items.filter((i) => i.kind === values.kind)
  if (values.src) items = items.filter((i) => i.sourceBucket === values.src)
  const sorted = [...items]
  const byTime = (a: QueueItem, b: QueueItem) => (b.time || '').localeCompare(a.time || '')
  if (values.sort === 'votes') {
    sorted.sort((a, b) => b.voteCount - a.voteCount || byTime(a, b))
  } else if (values.sort === 'confidence') {
    sorted.sort((a, b) => confValue(b) - confValue(a) || byTime(a, b))
  } else {
    sorted.sort(byTime)
  }
  return sorted
})

function confValue(i: QueueItem): number {
  const m = /(\d+)%/.exec(i.confidence)
  return m ? Number(m[1]) : -1
}

const queueFiltered = computed(() => !!(values.kind || values.src))
const selectedItems = computed(() => queue.value.filter((i) => selected.value.includes(i.id)))
const failedHandled = computed(() => handled.value.filter((h) => h.status === 'failed'))
const bulkFailuresVisible = computed(() => !bulkBusy.value && failedHandled.value.length > 0)
const visibleHandled = computed(() =>
  handledFilter.value === 'failed' ? failedHandled.value : handled.value,
)

function clearQueueFilters() {
  values.kind = ''
  values.src = ''
}

function toggleSelect(id: string) {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((x) => x !== id)
    : [...selected.value, id]
}

function itemMeta(item: QueueItem) {
  return [
    { label: t('kbTime', ui.lang), value: fmtDateTime(item.time) || '—' },
    { label: t('kbFilterSource', ui.lang), value: item.sourceLabel },
    ...(item.kind === 'lesson'
      ? [
          { label: t('kbVotes', ui.lang), value: item.votes },
          { label: t('kbConfidence', ui.lang), value: item.confidence },
        ]
      : []),
  ]
}

/* ── per-item actions (the only path; bulk is N of these) ───────────────── */

/** One row per (item, action): a retry replaces its own failed row instead of
 *  stacking a second one — otherwise a stale failure outlives the retry. */
function markHandled(item: QueueItem, action: 'confirm' | 'reject', status: 'ok' | 'failed', audit: string, error = '') {
  const id = `${item.id}:${action}`
  handled.value = [
    { id, title: item.title, status, action, audit, error, item },
    ...handled.value.filter((h) => h.id !== id),
  ].slice(0, 50)
}

function clearRefused(id: string) {
  if (!(id in refused.value)) return
  const next = { ...refused.value }
  delete next[id]
  refused.value = next
}

function setItemBusy(id: string, v: boolean) {
  const next = { ...busyIds.value }
  if (v) next[id] = true
  else delete next[id]
  busyIds.value = next
}

function errorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 404) return t('kbGoneAlready', ui.lang)
  return e instanceof Error ? e.message : String(e)
}

async function confirmItem(item: QueueItem, note?: string, silent = false): Promise<boolean> {
  setItemBusy(item.id, true)
  try {
    const body: Record<string, unknown> = { datasource: values.ds }
    let res: { audit?: string }
    if (item.kind === 'lesson') {
      body.key = item.key
      if (note !== undefined) body.note = note
      res = await apiPost('/v1/kb/lessons/confirm-one', body)
    } else {
      body.question = item.question
      body.sql = item.sql
      res = await apiPost('/v1/kb/examples/confirm-one', body)
    }
    const audit = res?.audit || (item.kind === 'lesson' ? 'kb.lesson.confirm' : 'kb.example.confirm')
    clearRefused(item.id)
    markHandled(item, 'confirm', 'ok', audit)
    if (!silent) notifySuccess(t('kbReceiptConfirmed', ui.lang, audit))
    return true
  } catch (e) {
    const msg = errorText(e)
    refused.value = { ...refused.value, [item.id]: msg }
    markHandled(item, 'confirm', 'failed', '', msg)
    if (!silent) toastError(e)
    return false
  } finally {
    setItemBusy(item.id, false)
  }
}

async function rejectItem(item: QueueItem, silent = false): Promise<boolean> {
  setItemBusy(item.id, true)
  try {
    let res: { audit?: string }
    if (item.kind === 'lesson') {
      res = await apiPost('/v1/kb/lessons/reject-one', {
        key: item.key,
        datasource: values.ds,
      })
    } else {
      res = await apiPost('/v1/kb/examples/reject-one', {
        question: item.question,
        sql: item.sql,
        datasource: values.ds,
      })
    }
    const audit = res?.audit || (item.kind === 'lesson' ? 'kb.lesson.reject' : 'kb.example.reject')
    clearRefused(item.id)
    markHandled(item, 'reject', 'ok', audit)
    if (!silent) notifySuccess(t('kbReceiptRejected', ui.lang, audit))
    return true
  } catch (e) {
    const msg = errorText(e)
    markHandled(item, 'reject', 'failed', '', msg)
    if (!silent) toastError(e)
    return false
  } finally {
    setItemBusy(item.id, false)
  }
}

function askReject(item: QueueItem) {
  rejectTargets.value = [item]
  rejectOpen.value = true
}

function askRejectSelected() {
  rejectTargets.value = selectedItems.value
  if (!rejectTargets.value.length) return
  rejectOpen.value = true
}

function rejectHasKind(kind: 'lesson' | 'example'): boolean {
  return rejectTargets.value.some((i) => i.kind === kind)
}

async function doReject() {
  rejecting.value = true
  const batch = rejectTargets.value.length > 1
  let anyOk = false
  for (const item of rejectTargets.value) {
    const ok = await rejectItem(item, batch)
    if (ok) {
      anyOk = true
      selected.value = selected.value.filter((id) => id !== item.id)
    }
  }
  rejecting.value = false
  rejectOpen.value = false
  if (anyOk) await loadAll()
}

async function confirmSelected() {
  const items = selectedItems.value
  if (!items.length) return
  bulkBusy.value = true
  bulkProgress.value = { done: 0, total: items.length }
  let ok = 0
  for (const item of items) {
    const success = await confirmItem(item, undefined, true)
    if (success) {
      ok += 1
      selected.value = selected.value.filter((id) => id !== item.id)
    }
    bulkProgress.value = { done: (bulkProgress.value?.done ?? 0) + 1, total: items.length }
  }
  bulkBusy.value = false
  bulkProgress.value = null
  await loadAll()
  if (ok === items.length) notifySuccess(t('kbBulkAllOk', ui.lang))
}

/** The card path for one decision: confirm, then refresh — the queue is the
 *  mirror's view of the YAML, so a handled item must leave the screen. */
async function confirmCard(item: QueueItem) {
  const ok = await confirmItem(item)
  if (ok) await loadAll()
}

function retryHandled(h: HandledItem) {
  if (h.action === 'confirm') void confirmCard(h.item)
  else void rejectItem(h.item).then((ok) => (ok ? loadAll() : undefined))
}

function openEdit(item: QueueItem) {
  editTarget.value = item
  editNote.value = item.note
  editOpen.value = true
}

async function saveEdit() {
  const item = editTarget.value
  if (!item) return
  editSaving.value = true
  const changed = editNote.value !== item.note
  const ok = await confirmItem(item, changed ? editNote.value : undefined, true)
  editSaving.value = false
  if (ok) {
    editOpen.value = false
    notifySuccess(t('kbReceiptConfirmed', ui.lang, 'kb.lesson.confirm'))
    await loadAll()
  }
}

/* ── examples tab rows ─────────────────────────────────────────────────── */

const exampleRows = computed<ExampleRow[]>(() => {
  const rows: ExampleRow[] = []
  for (const ex of pendingExamples.value) {
    rows.push({
      id: `pending:${ex.question}\u0000${ex.sql || ''}`,
      question: ex.question || '',
      sql: ex.sql || '',
      tags: ex.tags ?? [],
      template: false,
      status: 'pending',
      provenance: (ex.tags ?? []).includes('auto') ? t('kbSrcAutoDraft', ui.lang) : t('kbSrcDraft', ui.lang),
      createdAt: ex.created_at || '',
    })
  }
  const seen = new Set(rows.map((r) => r.question + '\u0000' + r.sql))
  for (const ex of detail.value?.examples ?? []) {
    const q = ex.question || ''
    if (seen.has(q + '\u0000' + (ex.sql || ''))) continue
    const certified = ex.status === 'certified'
    rows.push({
      id: `certified:${q}\u0000${ex.sql || ''}`,
      question: q,
      sql: ex.sql || '',
      tags: ex.tags ?? [],
      template: !!ex.template,
      status: ex.status || 'draft',
      provenance: certified && ex.approved_by
        ? `${t('kbApprovedBy', ui.lang, ex.approved_by)}${ex.source ? ' · ' + ex.source : ''}`
        : ex.source || '',
      createdAt: ex.approved_at || '',
    })
  }
  return rows
})

const visibleExamples = computed(() => {
  let rows = exampleRows.value
  if (values.status === 'pending') rows = rows.filter((r) => r.status === 'pending')
  // 'Certified' means certified: a mirror row still at `draft` is not one
  // (it stays visible under All, and shows its own Draft badge there).
  else if (values.status === 'certified') rows = rows.filter((r) => r.status === 'certified')
  const q = values.q.trim().toLowerCase()
  if (q) rows = rows.filter((r) => `${r.question} ${r.sql}`.toLowerCase().includes(q))
  return rows
})

function exampleStatusLabel(status: string): string {
  if (status === 'pending') return t('kbPendingBadge', ui.lang)
  if (status === 'certified') return t('kbCertified', ui.lang)
  return t('kbDraft', ui.lang)
}

async function confirmExampleRow(row: ExampleRow) {
  const item = exampleRows.value.length
    ? queueSource.value.find((i) => i.id === `example:${row.question}\u0000${row.sql}`)
    : undefined
  const target: QueueItem =
    item ?? {
      id: `example:${row.question}\u0000${row.sql}`,
      kind: 'example',
      title: row.question,
      note: '',
      sql: row.sql,
      sourceBucket: 'draft',
      sourceLabel: exampleStatusLabel(row.status),
      votes: '—',
      voteCount: -1,
      confidence: '—',
      time: row.createdAt,
      evidence: '',
      key: row.question,
      question: row.question,
    }
  const ok = await confirmItem(target)
  if (ok) await loadAll()
}

function askRejectExampleRow(row: ExampleRow) {
  askReject({
    id: `example:${row.question}\u0000${row.sql}`,
    kind: 'example',
    title: row.question,
    note: '',
    sql: row.sql,
    sourceBucket: 'draft',
    sourceLabel: exampleStatusLabel(row.status),
    votes: '—',
    voteCount: -1,
    confidence: '—',
    time: row.createdAt,
    evidence: '',
    key: row.question,
    question: row.question,
  })
}

/* ── lessons tab rows ──────────────────────────────────────────────────── */

const lessonRows = computed<LessonRow[]>(() =>
  (lessons.value as KbLesson[]).map((l) => ({
    id: `lesson:${l.pattern || l.question || ''}`,
    pattern: (l.question || l.pattern || '').trim(),
    note: l.note || '',
    sql: l.sql_snippet || '',
    sourceLabel: lessonSourceLabel(l.source),
    sourceBucket: lessonSourceBucket(l.source),
    confidence: typeof l.confidence === 'number' ? l.confidence : null,
    upvotes: l.upvotes ?? 0,
    downvotes: l.downvotes ?? 0,
    time: l.created_at || l.updated_at || '',
    confirmed: !!l.confirmed,
  })),
)

const visibleLessons = computed(() => {
  if (values.status === 'pending') return lessonRows.value.filter((r) => !r.confirmed)
  if (values.status === 'confirmed') return lessonRows.value.filter((r) => r.confirmed)
  return lessonRows.value
})

/* ── assets tab ────────────────────────────────────────────────────────── */

const assetRows = computed<KbAssetRow[]>(() => {
  const rows = assets.value.map((a) => ({
    ...a,
    rel: `${values.ds}/${a.file}`,
    refusedReason: refusedMap.value[`${values.ds}/${a.file}`] || a.refused || '',
  }))
  if (values.prob === '1') return rows.filter((r) => r.refusedReason || r.error)
  return rows
})

function editedLabel(edited: boolean | null | undefined): string {
  if (edited === true) return t('kbEditedYes', ui.lang)
  if (edited === false) return t('kbEditedNo', ui.lang)
  return '—'
}

function openAsset(row: unknown) {
  assetSel.value = row as KbAssetRow
  assetOpen.value = true
}

const assetDrawerTitle = computed(() => assetSel.value?.file || t('kbDrawerAria', ui.lang))

const assetFields = computed(() => {
  const a = assetSel.value
  if (!a) return []
  return [
    { label: t('kbColFile', ui.lang), value: a.file },
    { label: t('kbColFormat', ui.lang), value: a.format === null || a.format === undefined ? '—' : String(a.format) },
    { label: t('kbColGenerator', ui.lang), value: a.generator || '—' },
    { label: t('kbColGeneratedAt', ui.lang), value: fmtDateTime(a.generated_at) || '—' },
    { label: t('kbAssetTroveVersion', ui.lang), value: a.trove || '—' },
    { label: t('kbColEdited', ui.lang), value: editedLabel(a.edited) },
    { label: t('kbColBaseline', ui.lang), value: a.has_baseline ? t('kbBaselineYes', ui.lang) : t('kbBaselineNo', ui.lang) },
    { label: t('kbColAdoption', ui.lang), value: a.refusedReason ? t('kbRefused', ui.lang) : t('kbAdopted', ui.lang) },
  ]
})

const assetNotes = computed(() => {
  const a = assetSel.value
  if (!a) return []
  const notes: { label: string; text: string; danger?: boolean }[] = []
  if (a.error) notes.push({ label: t('kbErrorTitle', ui.lang), text: a.error, danger: true })
  if (a.refusedReason) {
    notes.push({ label: t('kbRefused', ui.lang), text: t('kbRefusedNote', ui.lang, a.refusedReason), danger: true })
  }
  if (a.format === 0 || a.needs_migration || a.format === null) {
    notes.push({ label: t('kbColFormat', ui.lang), text: t('kbFormatNote', ui.lang) })
  }
  return notes
})

const assetDispositions = computed(() => [
  { id: 'reload', label: t('dsReload', ui.lang), desc: t('kbResyncDesc', ui.lang) },
  { id: 'merge', label: t('kbReinit', ui.lang), desc: t('kbReinitDesc', ui.lang) },
  { id: 'overwrite', label: t('kbOverwrite', ui.lang), desc: t('kbOverwriteDesc', ui.lang), danger: true },
])

function onDisposition(id: string) {
  assetOpen.value = false
  if (id === 'reload') void reloadKb()
  else if (id === 'merge') askMenuAction('merge')
  else if (id === 'overwrite') askMenuAction('overwrite')
}

/* ── entries tab ───────────────────────────────────────────────────────── */

const filteredEntries = computed<KbSemanticEntry[]>(() => {
  const q = values.q.trim().toLowerCase()
  if (!q) return entries.value
  return entries.value.filter((e) =>
    [
      e.key,
      e.name,
      ...(e.aliases ?? []),
      ...(e.synonyms ?? []),
      ...(e.datasets ?? []),
      e.dataset,
      e.field,
      e.definition,
      e.description,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(q),
  )
})

function entryKindLabel(kind: string): string {
  if (kind === 'metric') return t('kbEntryKindMetric', ui.lang)
  if (kind === 'entity') return t('kbEntryKindEntity', ui.lang)
  return t('kbEntryKindTable', ui.lang)
}

function expressionText(e: KbSemanticEntry): string {
  return typeof e.expression === 'string' ? e.expression : ''
}

function datasetsText(e: KbSemanticEntry): string {
  const list = [...(e.datasets ?? []), ...(e.dataset ? [e.dataset] : [])]
  return list.length ? list.join(' / ') : '—'
}

/** Synonyms column: entity synonyms, else metric aliases — never invented. */
function synonymsText(e: KbSemanticEntry): string {
  const list = [...(e.synonyms ?? []), ...(e.aliases ?? [])]
  return list.join(', ')
}

/* ── table columns / tabs / KPIs ───────────────────────────────────────── */

const assetColumns = computed<DataTableColumn[]>(() => [
  { key: 'file', label: t('kbColFile', ui.lang) },
  { key: 'format', label: t('kbColFormat', ui.lang), width: 70 },
  { key: 'generator', label: t('kbColGenerator', ui.lang), width: 120 },
  { key: 'generated_at', label: t('kbColGeneratedAt', ui.lang), width: 150 },
  { key: 'edited', label: t('kbColEdited', ui.lang), width: 110 },
  { key: 'has_baseline', label: t('kbColBaseline', ui.lang), width: 80 },
  { key: 'adoption', label: t('kbColAdoption', ui.lang), width: 170 },
])

const entryColumns = computed<DataTableColumn[]>(() => [
  { key: 'name', label: t('kbColName', ui.lang) },
  { key: 'kind', label: t('kbColType', ui.lang), width: 90 },
  { key: 'synonyms', label: t('kbColSynonyms', ui.lang) },
  { key: 'expression', label: t('kbColExpression', ui.lang) },
  { key: 'datasets', label: t('kbColDatasets', ui.lang) },
  { key: 'definition', label: t('kbColDefinition', ui.lang) },
])

const exampleColumns = computed<DataTableColumn[]>(() => [
  { key: 'question', label: t('kbQuestion', ui.lang) },
  { key: 'sql', label: t('kbColSql', ui.lang) },
  { key: 'tags', label: t('kbTags', ui.lang), width: 150 },
  { key: 'provenance', label: t('kbColProvenance', ui.lang), width: 180 },
  { key: 'status', label: t('kbColStatus', ui.lang), width: 110 },
  { key: 'actions', label: t('actions', ui.lang), width: 90 },
])

const lessonColumns = computed<DataTableColumn[]>(() => [
  { key: 'pattern', label: t('kbColPattern', ui.lang) },
  { key: 'note', label: t('kbColNote', ui.lang) },
  { key: 'source', label: t('kbColProvenance', ui.lang), width: 130 },
  { key: 'votes', label: t('kbVotes', ui.lang), width: 110 },
  { key: 'time', label: t('kbTime', ui.lang), width: 150 },
  { key: 'status', label: t('kbColStatus', ui.lang), width: 110 },
])

const tabDefs = computed(() => [
  { key: 'pending' as TabKey, label: t('kbTabPending', ui.lang), count: pendingLessons.value.length + pendingExamples.value.length },
  { key: 'assets' as TabKey, label: t('kbTabAssets', ui.lang), count: assets.value.length },
  { key: 'entries' as TabKey, label: t('kbTabEntries', ui.lang), count: entries.value.length },
  { key: 'examples' as TabKey, label: t('kbTabExamples', ui.lang), count: exampleRows.value.length },
  { key: 'lessons' as TabKey, label: t('kbTabLessons', ui.lang), count: lessons.value.length },
  { key: 'rules' as TabKey, label: t('kbTabRules', ui.lang), count: rules.value.length },
])

const refusedAssetCount = computed(
  () => assetRows.value.filter((r) => r.refusedReason || r.error).length,
)

const kpiTiles = computed(() => [
  {
    key: 'pending' as KpiKey,
    label: t('kbKpiPending', ui.lang),
    value: pendingLessons.value.length + pendingExamples.value.length,
    sub: `${t('kbKpiLessonSub', ui.lang, pendingLessons.value.length)} · ${t('kbKpiExampleSub', ui.lang, pendingExamples.value.length)}`,
    active: values.tab === 'pending',
  },
  {
    key: 'assets' as KpiKey,
    label: t('kbKpiAssets', ui.lang),
    value: `${assets.value.length - refusedAssetCount.value}·${refusedAssetCount.value}`,
    sub: !assets.value.length
      ? t('kbKpiAssetsSubNone', ui.lang)
      : refusedAssetCount.value
        ? t('kbKpiAssetsSubRefused', ui.lang, refusedAssetCount.value)
        : t('kbKpiAssetsSubOk', ui.lang),
    active: values.tab === 'assets',
  },
  {
    key: 'entries' as KpiKey,
    label: t('kbKpiEntries', ui.lang),
    value: entries.value.length,
    sub: t('kbKpiEntriesSub', ui.lang),
    active: values.tab === 'entries',
  },
  {
    key: 'examples' as KpiKey,
    label: t('kbKpiExamples', ui.lang),
    value: exampleRows.value.length,
    sub: t('kbKpiExamplesSub', ui.lang, pendingExamples.value.length),
    active: values.tab === 'examples',
  },
  {
    key: 'lessons' as KpiKey,
    label: t('kbKpiLessons', ui.lang),
    value: lessons.value.length,
    sub: t('kbKpiLessonsSub', ui.lang, pendingLessons.value.length),
    active: values.tab === 'lessons',
  },
])

function applyKpi(key: KpiKey) {
  values.tab = key
  values.status = ''
  if (key === 'pending') {
    values.prob = ''
  } else if (key === 'assets') {
    values.prob = refusedAssetCount.value ? '1' : ''
  }
}

function switchTab(key: TabKey) {
  values.tab = key
  values.status = ''
  if (key !== 'assets') values.prob = ''
  if (key !== 'entries' && key !== 'examples') values.q = ''
}

/* ── KB lifecycle: init / reload / delete ──────────────────────────────── */

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function initStageLabel(stage?: string): string {
  const map: Record<string, string> = {
    queued: t('kbInitStageQueued', ui.lang),
    schema: t('kbInitStageSchema', ui.lang),
    probe: t('kbInitStageProbe', ui.lang),
    notes: t('kbInitStageNotes', ui.lang),
    examples: t('kbInitStageExamples', ui.lang),
    semantic: t('kbInitStageSemantic', ui.lang),
    write: t('kbInitStageWrite', ui.lang),
    done: t('kbInitStageDone', ui.lang),
    error: t('kbInitStageError', ui.lang),
  }
  return (stage && map[stage]) || stage || ''
}

async function pollInitStatus(): Promise<void> {
  for (;;) {
    await sleep(2000)
    const st = await apiGet<{
      status: string
      stage?: string
      progress?: number
      detail?: string
      error?: string
    }>(`/v1/admin/datasources/${encodeURIComponent(values.ds)}/kb/init/status`)
    initProgress.value = { stage: st.stage, progress: st.progress ?? 0, detail: st.detail }
    if (st.status === 'done') {
      initProgress.value = { stage: 'done', progress: 100 }
      return
    }
    if (st.status === 'error') throw new Error(st.error || t('dsInitFail', ui.lang))
    if (st.status === 'idle') throw new Error(t('dsInitLost', ui.lang))
  }
}

async function initKb(overwrite = false, force = false) {
  setBusy('init', true)
  initProgress.value = { stage: 'queued', progress: 0 }
  const notice = ElMessage({ type: 'info', message: t('dsInitStarted', ui.lang), duration: 0 })
  try {
    await apiPost(
      `/v1/admin/datasources/${encodeURIComponent(values.ds)}/kb/init`,
      { overwrite, force },
    )
    await pollInitStatus()
    notifySuccess(t('dsInitDone', ui.lang))
    await loadAll()
  } catch (e) {
    toastError(e, t('dsInitFail', ui.lang))
  } finally {
    notice.close()
    setBusy('init', false)
    initProgress.value = null
  }
}

async function pollReloadStatus(): Promise<void> {
  for (;;) {
    await sleep(2000)
    const st = await apiGet<{ status: string; error?: string }>(
      `/v1/admin/datasources/${encodeURIComponent(values.ds)}/kb/reload/status`,
    )
    if (st.status === 'done') return
    if (st.status === 'error') throw new Error(st.error || t('dsReloadFail', ui.lang))
    if (st.status === 'idle') throw new Error(t('dsReloadLost', ui.lang))
  }
}

async function reloadKb() {
  setBusy('reload', true)
  const notice = ElMessage({ type: 'info', message: t('dsReloadStarted', ui.lang), duration: 0 })
  try {
    await apiPost(`/v1/admin/datasources/${encodeURIComponent(values.ds)}/kb/reload`)
    await pollReloadStatus()
    notifySuccess(t('dsReloadDone', ui.lang))
    await loadAll()
  } catch (e) {
    toastError(e, t('dsReloadFail', ui.lang))
  } finally {
    notice.close()
    setBusy('reload', false)
  }
}

async function deleteKb() {
  setBusy('delete', true)
  try {
    const body = await apiDelete(
      `/v1/admin/datasources/${encodeURIComponent(values.ds)}/kb`,
    )
    detail.value = body.kb
    notifySuccess(t('kbDeleteDone', ui.lang))
    await loadAll()
  } catch (e) {
    toastError(e)
  } finally {
    setBusy('delete', false)
  }
}

function askMenuAction(action: 'init' | 'merge' | 'overwrite' | 'delete') {
  menuOpen.value = false
  menuAction.value = action
  menuOpen2.value = true
}

const menuTitle = computed(() => {
  if (menuAction.value === 'overwrite') return t('kbOverwriteConfirmTitle', ui.lang, values.ds)
  if (menuAction.value === 'delete') return t('kbDeleteConfirmTitle', ui.lang, values.ds)
  if (menuAction.value === 'merge') return t('kbReinit', ui.lang)
  return t('dsInit', ui.lang)
})

const menuConfirmText = computed(() => {
  if (menuAction.value === 'overwrite') return t('kbOverwrite', ui.lang)
  if (menuAction.value === 'delete') return t('kbDelete', ui.lang)
  if (menuAction.value === 'merge') return t('kbReinit', ui.lang)
  return t('dsInit', ui.lang)
})

const menuImpact = computed(() => {
  if (menuAction.value === 'overwrite') {
    return [
      t('kbOverwriteImpact1', ui.lang),
      t('kbOverwriteImpact2', ui.lang),
      t('kbOverwriteImpact3', ui.lang),
    ]
  }
  if (menuAction.value === 'delete') {
    return [
      t('kbDeleteImpact1', ui.lang, values.ds),
      t('kbDeleteImpact2', ui.lang),
      t('kbDeleteImpact3', ui.lang),
    ]
  }
  if (menuAction.value === 'merge') return [t('kbReinitConfirm', ui.lang)]
  return [t('dsInitConfirm', ui.lang)]
})

async function runMenuAction() {
  const action = menuAction.value
  menuOpen2.value = false
  if (action === 'init') await initKb()
  else if (action === 'merge') await initKb(true, false)
  else if (action === 'overwrite') await initKb(true, true)
  else if (action === 'delete') await deleteKb()
  menuAction.value = ''
}

/* ── add term / add example (unchanged flows, page-owned validation) ───── */

function splitCsv(v: string): string[] {
  return v
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

const termOpen = ref(false)
const termError = ref('')
const termErrors = ref<{ term: string; mapping: string }>({ term: '', mapping: '' })
const termForm = ref({ term: '', aliases: '', mapping: '', tables: '', definition: '' })

function openTerm() {
  termError.value = ''
  termErrors.value = { term: '', mapping: '' }
  Object.assign(termForm.value, { term: '', aliases: '', mapping: '', tables: '', definition: '' })
  termOpen.value = true
}

async function addTerm() {
  const f = termForm.value
  termErrors.value = { term: '', mapping: '' }
  termError.value = ''
  if (!f.term.trim()) {
    termErrors.value = { term: t('kbMappingRequired', ui.lang), mapping: '' }
    return
  }
  if (!f.mapping.trim()) {
    termErrors.value = { term: '', mapping: t('kbMappingRequired', ui.lang) }
    return
  }
  acting.value = true
  try {
    await apiPost(`/v1/kb/terms?datasource=${encodeURIComponent(values.ds)}`, {
      term: f.term.trim(),
      aliases: splitCsv(f.aliases),
      mapping: f.mapping.trim(),
      tables: splitCsv(f.tables),
      definition: f.definition.trim(),
    })
    termOpen.value = false
    notifySuccess(t('kbTermAdded', ui.lang))
    await loadAll()
  } catch (e) {
    termError.value = e instanceof Error ? e.message : t('kbAddFail', ui.lang)
  } finally {
    acting.value = false
  }
}

const exampleOpen = ref(false)
const exError = ref('')
const exErrors = ref<{ question: string; sql: string }>({ question: '', sql: '' })
const exForm = ref({ question: '', sql: '', tags: '' })

function openExample() {
  exError.value = ''
  exErrors.value = { question: '', sql: '' }
  Object.assign(exForm.value, { question: '', sql: '', tags: '' })
  exampleOpen.value = true
}

async function addExample() {
  const f = exForm.value
  exErrors.value = { question: '', sql: '' }
  exError.value = ''
  if (!f.question.trim() || !f.sql.trim()) {
    exErrors.value = {
      question: f.question.trim() ? '' : t('kbMappingRequired', ui.lang),
      sql: f.sql.trim() ? '' : t('kbMappingRequired', ui.lang),
    }
    return
  }
  acting.value = true
  try {
    await apiPost(`/v1/kb/examples?datasource=${encodeURIComponent(values.ds)}`, {
      question: f.question.trim(),
      sql: f.sql.trim(),
      tags: splitCsv(f.tags),
    })
    exampleOpen.value = false
    notifySuccess(t('kbExampleAdded', ui.lang))
    await loadAll()
  } catch (e) {
    exError.value = e instanceof Error ? e.message : t('kbAddFail', ui.lang)
  } finally {
    acting.value = false
  }
}

async function copySnippet(sql: string) {
  const ok = await copyText(sql)
  notifySuccess(ok ? t('copied', ui.lang) : t('copyFailed', ui.lang))
}
</script>

<style scoped>
.kb-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.ds-select {
  width: 190px;
}

.more-wrap {
  position: relative;
}

.more-menu {
  position: absolute;
  top: calc(100% + var(--sp-1));
  right: 0;
  z-index: 20;
  min-width: 220px;
  padding: var(--sp-1);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  box-shadow: var(--shadow-lg);
}

.menu-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm);
  color: var(--text-primary);
  font-size: var(--fs-xs);
  text-align: left;
}
.menu-item:hover:not(:disabled) {
  background: var(--surface-hover);
}
.menu-item:disabled {
  color: var(--text-tertiary);
  cursor: default;
}
.menu-item.is-danger {
  color: var(--danger);
}
.menu-item:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: -2px;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--sp-2);
}
@media (max-width: 1100px) {
  .kpi-row {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.kb-init-progress {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
}
.kb-init-stage {
  display: flex;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}

.kb-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: var(--sp-1);
}

.kb-tab {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--r-sm) var(--r-sm) 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.kb-tab:hover {
  background: var(--surface-hover);
  color: var(--text-primary);
}
.kb-tab.is-active {
  background: var(--accent-soft);
  color: var(--accent-active);
}
.kb-tab:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: -2px;
}

.tab-badge {
  min-width: 18px;
  padding: 0 var(--sp-1);
  border-radius: var(--r-full);
  background: var(--surface-muted);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  text-align: center;
}
.kb-tab.is-active .tab-badge {
  background: var(--indigo-100);
  color: var(--accent-active);
}

.kb-pane {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.list-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.list-toolbar .spacer {
  flex: 1;
}

.toolbar-search {
  width: 260px;
}

.filter-select {
  width: 168px;
}

.seg-btn {
  padding: var(--sp-1) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-full);
  background: var(--surface-raised);
  color: var(--text-secondary);
  font-size: var(--fs-2xs);
  font-weight: 500;
}
.seg-btn:hover {
  border-color: var(--border-default);
  color: var(--text-primary);
}
.seg-btn.is-active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-active);
}
.seg-btn:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 1px;
}

.toolbar-note {
  margin: 0;
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
}

.bulk-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--accent);
  border-radius: var(--r-md);
  background: var(--accent-soft);
}
.bulk-count {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--accent-active);
}
.bulk-progress {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.bulk-bar .spacer {
  flex: 1;
}

.partial-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--danger);
  border-radius: var(--r-md);
  background: var(--danger-bg);
}
.partial-title {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--danger);
}
.partial-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
}
.partial-name {
  font-weight: 600;
  color: var(--text-primary);
}
.partial-error {
  flex: 1;
  min-width: 0;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.queue-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.handled-sec {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
}
.handled-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.handled-title {
  margin: 0 var(--sp-2) 0 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-primary);
}
.handled-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--border-subtle);
  font-size: var(--fs-2xs);
}
.handled-badge {
  flex: none;
  padding: 1px var(--sp-2);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  font-weight: 600;
}
.handled-badge.is-ok {
  background: var(--ok-bg);
  color: var(--ok);
}
.handled-badge.is-failed {
  background: var(--danger-bg);
  color: var(--danger);
}
.handled-name {
  font-weight: 600;
  color: var(--text-primary);
}
.handled-error {
  flex: 1;
  min-width: 0;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.entry-name {
  font-weight: 600;
  color: var(--text-primary);
}

.cell-time {
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.asset-file {
  font-weight: 600;
  color: var(--text-primary);
}
.asset-file.is-refused {
  color: var(--danger);
}
.adoption-badge {
  display: inline-flex;
  padding: 1px var(--sp-2);
  border-radius: var(--r-full);
  font-size: var(--fs-2xs);
  font-weight: 600;
}
.adoption-badge.is-ok {
  background: var(--ok-bg);
  color: var(--ok);
}
.adoption-badge.is-refused {
  background: var(--danger-bg);
  color: var(--danger);
}

.kb-assets :deep(.dt-row:has(.adoption-badge.is-refused)) {
  background: var(--danger-bg);
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
.tag-pill.is-metric {
  background: var(--accent-soft);
  color: var(--accent-active);
}
.tag-pill.is-entity {
  background: var(--warn-bg);
  color: var(--warn-text);
}
.tag-pill.is-table {
  background: var(--surface-muted);
  color: var(--text-secondary);
}
.tag-pill.is-pending {
  background: var(--indigo-100);
  color: var(--accent-active);
}
.tag-pill.is-ok {
  background: var(--ok-bg);
  color: var(--ok);
}

.rule-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.rule-item {
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--surface-raised);
  font-size: var(--fs-xs);
  color: var(--text-primary);
}

.confirm-names {
  margin: 0;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.impact-list {
  margin: 0;
  padding-left: var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
}
</style>
