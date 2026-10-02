import { describe, expect, it, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ProvenanceStrip from '../src/components/chat/ProvenanceStrip.vue'
import {
  buildChips,
  buildProvenance,
  fmtStamp,
  sourceLabelKey,
  statusLabelKey,
} from '../src/utils/provenance'
import { useChatStore } from '../src/stores/chat'
import type { DoneSummary } from '../src/api/types'

describe('fmtStamp', () => {
  it('renders a second-precision local stamp', () => {
    const iso = new Date(2026, 9, 2, 14, 3, 12).toISOString()
    expect(fmtStamp(iso)).toBe('2026-10-02 14:03:12')
  })

  it('returns empty on missing / invalid input — never a guessed time', () => {
    expect(fmtStamp(undefined)).toBe('')
    expect(fmtStamp('')).toBe('')
    expect(fmtStamp('not-a-date')).toBe('')
  })
})

describe('buildProvenance — 宁缺毋假', () => {
  it('empty summary yields empty strings, not "unknown"', () => {
    const v = buildProvenance(null)
    expect(v.datasource).toBe('')
    expect(v.time).toBe('')
    expect(v.model).toBe('')
    expect(v.runId).toBe('')
    expect(v.runShort).toBe('')
    expect(v.source).toBe('')
    expect(v.confidence).toBe('')
    expect(v.sqlConfidence).toBe('')
    expect(v.asOf).toBe('')
    expect(v.asOfBasis).toBe('')
    expect(v.kbHits).toEqual([])
    expect(v.verdict).toBe('')
    expect(v.mask).toBeNull()
    expect(v.principalSubject).toBe('')
    expect(v.confidenceWhy).toEqual([])
  })

  it('confidence 0 means "no disclosable answer", not 0%', () => {
    const v = buildProvenance({ confidence: 0, sql_confidence: 0 })
    expect(v.confidence).toBe('')
    expect(v.sqlConfidence).toBe('')
  })

  it('masking keeps the three states apart (null / empty / bypass)', () => {
    expect(buildProvenance({}).mask).toBeNull()
    expect(buildProvenance({ masking_applied: null }).mask).toBeNull()
    expect(buildProvenance({ masking_applied: {} }).mask).toBeNull()
    const masked = buildProvenance({
      masking_applied: { fields: { phone: 'partial', id_card: 'hash' } },
    })
    expect(masked.mask).toEqual({ kind: 'masked', count: 2 })
    expect(masked.maskedFields).toEqual(['phone', 'id_card'])
    const bypass = buildProvenance({ masking_applied: { bypass: true } })
    expect(bypass.mask).toEqual({ kind: 'bypass', count: 0 })
  })

  it('kb hit views: term keeps term → mapping, template keeps raw status', () => {
    const v = buildProvenance({
      kb_hits: [
        { kind: 'term', term: '地区', mapping: 'district' },
        { kind: 'template', question: '问句模板', status: 'certified' },
        { question: '没有 kind 的示例' },
      ] as DoneSummary['kb_hits'],
    })
    expect(v.kbHits[0]).toEqual({ kind: 'term', label: '地区 → district', status: '' })
    expect(v.kbHits[1].label).toBe('问句模板')
    expect(v.kbHits[1].status).toBe('certified')
    expect(v.kbHits[2].kind).toBe('example')
  })

  it('run short = first 8 chars; as_of_basis passes through raw', () => {
    const v = buildProvenance({
      run_id: '3f9a2c1b-1111',
      execution_evidence: { data_as_of: '2026-09-30', as_of_basis: 'unknown' },
    })
    expect(v.runShort).toBe('3f9a2c1b')
    expect(v.asOf).toBe('2026-09-30')
    expect(v.asOfBasis).toBe('unknown')
  })
})

describe('buildChips', () => {
  it('no data → no chips (nothing to promise)', () => {
    expect(buildChips(buildProvenance({}))).toEqual([])
  })

  it('verdict OK is green; any other verdict is not read as passing', () => {
    const chips = buildChips(
      buildProvenance({ kb_hits: [{ kind: 'term', term: 't', mapping: 'm' }], verdict: 'OK' }),
    )
    expect(chips.map((c) => c.id)).toEqual(['kb', 'verify'])
    expect(chips[1].tone).toBe('ok')
    const other = buildChips(buildProvenance({ verdict: 'RETRY: bad sql' }))
    expect(other[0].tone).toBe('warn')
  })

  it('bypass chip never masquerades as "masked N fields"', () => {
    const masked = buildChips(buildProvenance({ masking_applied: { fields: { a: 'hash' } } }))
    expect(masked[0].id).toBe('mask')
    expect(masked[0].n).toBe(1)
    const bypass = buildChips(buildProvenance({ masking_applied: { bypass: true } }))
    expect(bypass[0].n).toBeUndefined()
    expect(bypass[0].titleKey).toBe('bypassBadgeTip')
  })
})

describe('vocabulary mappings', () => {
  it('unknown values fall through as raw (never upgraded to a nicer tier)', () => {
    expect(sourceLabelKey('compiled')).toBe('provSrcCompiled')
    expect(sourceLabelKey('reused')).toBe('provSrcReused')
    expect(sourceLabelKey('something_else')).toBe('')
    expect(statusLabelKey('certified')).toBe('provStatusCertified')
    expect(statusLabelKey('weird')).toBe('')
  })
})

describe('ProvenanceStrip', () => {
  function mountStrip(summary: DoneSummary | null, at?: string) {
    return mount(ProvenanceStrip, {
      props: { summary, at, lang: 'en' as const },
    })
  }

  it('renders nothing when there is nothing to disclose', () => {
    const w = mountStrip({})
    expect(w.find('.prov').exists()).toBe(false)
  })

  it('collapsed: one identity line + state chips, detail hidden', async () => {
    const w = mountStrip(
      {
        datasource: 'financial',
        run_id: '3f9a2c1b-1111-2222',
        model: 'mock/gen',
        verdict: 'OK',
        kb_hits: [{ kind: 'term', term: '地区', mapping: 'district' }],
        masking_applied: { fields: { phone: 'partial' } },
      },
      '2026-10-02T14:03:12',
    )
    const bar = w.find('.prov-bar')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('financial')
    expect(bar.text()).toContain('mock/gen')
    expect(bar.text()).toContain('run 3f9a2c1b')
    expect(bar.text()).toContain('KB hits 1')
    expect(bar.text()).toContain('checks OK')
    expect(bar.text()).toContain('Masked fields 1')
    expect(w.find('.prov-detail').exists()).toBe(false)
    expect(bar.attributes('aria-expanded')).toBe('false')

    await bar.trigger('click')
    expect(w.find('.prov-detail').exists()).toBe(true)
    expect(w.find('.prov-bar').attributes('aria-expanded')).toBe('true')
  })

  it('expanded detail: every row is driven by data (missing rows absent)', async () => {
    const w = mountStrip(
      {
        datasource: 'financial',
        model: 'mock/gen',
        answer_source: 'compiled',
        confidence: 0.82,
        sql_confidence: 0.88,
        confidence_evidence: [{ kind: 'result', why: '自检通过' }],
        verdict: 'OK',
        masking_applied: null,
        run_id: '3f9a2c1b-1111-2222',
      },
      '2026-10-02T14:03:12',
    )
    await w.find('.prov-bar').trigger('click')
    const text = w.find('.prov-detail').text()
    expect(text).toContain('2026-10-02 14:03:12')
    expect(text).toContain('Semantic compile（compiled）')
    expect(text).toContain('82%')
    expect(text).toContain('SQL 88%')
    expect(text).toContain('自检通过')
    // 没有脱敏报告 → 脱敏行整行不渲染(null ≠ 空报告,但两态都不出现)
    expect(text).not.toContain('Masking')
    // 没有 principal → 读取身份行不渲染
    expect(text).not.toContain('Read as')
    // 没有 execution_evidence → 数据截止行不渲染
    expect(text).not.toContain('Data as of')
  })

  it('replay button emits open-replay; evidence CTA emits open-evidence', async () => {
    const w = mountStrip({ run_id: 'abc12345-1111', datasource: 'd' })
    await w.find('.prov-bar').trigger('click')
    // run_id 行里 [复制, 回放];页脚的 CTA 也带 .prov-mini,所以只挑 run 行内的
    const minis = w.findAll('.run-cell .prov-mini')
    expect(minis).toHaveLength(2)
    await minis[1].trigger('click')
    expect(w.emitted('open-replay')).toHaveLength(1)
    await w.find('.evidence-cta').trigger('click')
    expect(w.emitted('open-evidence')).toHaveLength(1)
  })
})

describe('chat store — turn timestamp', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('stamps the turn when a plain done finalizes it', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    expect(chat.currentTurn?.at).toBeUndefined()
    chat.onEvent({ type: 'done', data: { summary: { final_response: 'a', sql: 'SELECT 1' } } })
    expect(chat.currentTurn?.status).toBe('done')
    expect(chat.currentTurn?.at).toBeTruthy()
    expect(Number.isNaN(Date.parse(chat.currentTurn!.at!))).toBe(false)
  })

  it('an intermediate batched done does not stamp the turn yet', () => {
    const chat = useChatStore()
    chat.turns.push({
      question: 'q',
      thoughts: [],
      steps: [],
      answer: '',
      summary: null,
      status: 'streaming',
    })
    chat.batchRunning = true
    chat.onEvent({ type: 'done', data: { summary: { final_response: 'chunk' } } })
    expect(chat.currentTurn?.at).toBeUndefined()
  })
})
