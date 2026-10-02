<!--
  GovernanceMap — 治理中心的第一屏(§2.2):治理动作的索引表(正文 10 行;
  任务书写「9 类」,规范 §2.2 表为 10 行,按规范渲染并在报告中说明)。

  它回答的是「收口」本身:哪个动作去哪做、什么端点、留没留痕、缺口在哪 ——
  管理员不必记住 11 个页面里治理能力的分工。**这一屏静态,零后端依赖**
  (端点与 file:line 是仓库事实,照录入表;动作/落点/缺口文案走 i18n)。

  「缺口」列是刻意保留的:治理工具必须承认自己哪些地方看不见,
  否则管理员会把「绿」读成「没事」(§6-D)。
-->
<script setup lang="ts">
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'

const ui = useUiStore()

/**
 * 技能 confirm / reject / tier 三处审计(§4.4a 的「最小修」)由**本批后端**补齐。
 * 补齐后治理地图的「留痕」列对技能行显示 ✅,缺口列随之清空;
 * 若后端批未落地这一条,这里必须翻回 'no'(❌)并恢复缺口文案
 * 「8 条路由零审计(建议后端补)」—— §4.4a 与 §6-D 对这一点没有余地。
 */
const SKILL_AUDIT_LANDED = true

type AuditMark = 'yes' | 'no' | 'none'
type GovKey = keyof typeof import('../../i18n').messages['zh']

interface MapRow {
  action: GovKey
  where: GovKey
  /** 端点 / file:line —— 仓库事实,原文照录(不翻译)。 */
  endpoints: string
  audit: AuditMark
  /** 缺口说明;'' = 无缺口(渲染 —)。 */
  gap: GovKey | ''
}

const rows: MapRow[] = [
  {
    action: 'govMapRowApprovals',
    where: 'govMapWhereInbox',
    endpoints: 'GET /v1/admin/todos',
    audit: 'yes',
    gap: '',
  },
  {
    action: 'govMapRowEdit',
    where: 'govMapWherePro',
    endpoints: '/v1/kb/* · /v1/admin/semantic/* · /v1/admin/skills/*',
    audit: 'yes',
    gap: 'govMapGapDeepLink',
  },
  {
    action: 'govMapRowDrift',
    where: 'govMapWhereTab3',
    endpoints: '/v1/admin/drift/* ×7',
    audit: 'yes',
    gap: 'govMapGapImpactEmpty',
  },
  {
    action: 'govMapRowRollback',
    where: 'govMapWhereTab3',
    endpoints: '/v1/admin/semantic/{ds}/history · /rollback',
    audit: 'yes',
    gap: 'govMapGapRollback',
  },
  {
    action: 'govMapRowCoverage',
    where: 'govMapWhereTab2',
    endpoints: 'GET /v1/admin/coverage',
    audit: 'none',
    gap: '',
  },
  {
    action: 'govMapRowLineage',
    where: 'govMapWhereTab4',
    endpoints: 'GET /v1/lineage/tables/{name}',
    audit: 'none',
    gap: '',
  },
  {
    action: 'govMapRowGrants',
    where: 'govMapWhereUsers',
    endpoints: 'admin.py:303 / 257 / 271 / 283',
    audit: 'yes',
    gap: 'govMapGapGrants',
  },
  {
    action: 'govMapRowAudit',
    where: 'govMapWhereAudit',
    endpoints: 'GET /v1/admin/audit',
    audit: 'none',
    gap: 'govMapGapAuditUser',
  },
  {
    action: 'govMapRowSkills',
    where: 'govMapWhereSkills',
    endpoints: 'skills.py:94 / 113',
    audit: SKILL_AUDIT_LANDED ? 'yes' : 'no',
    gap: SKILL_AUDIT_LANDED ? '' : 'govMapGapSkillAudit',
  },
  {
    action: 'govMapRowDecisions',
    where: 'govMapWhereDecisions',
    endpoints: 'decisions.py:58 / 143',
    audit: 'yes',
    gap: '',
  },
]

/** 留痕三态:✅ 写入审计 / ❌ 该路径零审计(可见的缺失标记) / — 只读,无动作可留痕。 */
const AUDIT_MARK: Record<AuditMark, string> = { yes: '✅', no: '❌', none: '—' }
</script>

<template>
  <section class="gov-map" :aria-label="t('govMapTitle', ui.lang)">
    <div class="gm-head">
      <h2 class="gm-title">{{ t('govMapTitle', ui.lang) }}</h2>
      <p class="gm-desc">{{ t('govMapDesc', ui.lang) }}</p>
    </div>
    <div class="gm-scroll">
      <table class="gm-table">
        <thead>
          <tr>
            <th>{{ t('govMapColAction', ui.lang) }}</th>
            <th>{{ t('govMapColWhere', ui.lang) }}</th>
            <th>{{ t('govMapColEndpoints', ui.lang) }}</th>
            <th class="gm-audit-col">{{ t('govMapColAudit', ui.lang) }}</th>
            <th>{{ t('govMapColGap', ui.lang) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.action">
            <td class="gm-action">{{ t(row.action, ui.lang) }}</td>
            <td>{{ t(row.where, ui.lang) }}</td>
            <td class="gm-ep"><code>{{ row.endpoints }}</code></td>
            <td class="gm-audit-col" :class="`is-${row.audit}`">
              <span :title="row.audit === 'none' ? '' : row.audit === 'yes' ? 'audited' : 'no audit'">
                {{ AUDIT_MARK[row.audit] }}
              </span>
            </td>
            <td class="gm-gap">
              <span v-if="row.gap">{{ t(row.gap, ui.lang) }}</span>
              <span v-else class="gm-none">{{ t('govMapGapNone', ui.lang) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.gov-map {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--surface-raised);
  padding: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.gm-head {
  margin-bottom: var(--sp-2);
}
.gm-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
}
.gm-desc {
  margin: 2px 0 0;
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.gm-scroll {
  overflow-x: auto;
}
.gm-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-2xs);
}
.gm-table th {
  text-align: left;
  color: var(--text-tertiary);
  font-weight: 500;
  padding: 2px 10px 2px 0;
  border-bottom: 1px solid var(--border-subtle);
  white-space: nowrap;
}
.gm-table td {
  padding: 3px 10px 3px 0;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: top;
}
.gm-action {
  font-weight: 600;
  white-space: nowrap;
}
.gm-ep code {
  font-size: var(--fs-2xs);
  color: var(--text-secondary);
  white-space: nowrap;
}
.gm-audit-col {
  text-align: center;
  width: 46px;
}
.gm-gap {
  color: var(--text-secondary);
}
.gm-none {
  color: var(--text-tertiary);
}
</style>
