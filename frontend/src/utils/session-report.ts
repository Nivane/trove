// 会话 HTML 报告(补丁 2)—— 自包含单文件:答案 markdown / 结构化分析
// (瀑布 SVG + 证据查询)/ SQL / 结果表全部内联,零外部依赖,离线可开、
// 可直接转发。形态学 Datus 的单文件报告对位。
//
// 与 ① 导出 Markdown 同口径:纯函数(不碰 DOM)、忠实渲染已落盘轮次、
// 截断必写明;老会话无 summary/analysis 时逐项降级跳过。答案里的 ASCII
// 图表块由内联 SVG 顶替 —— 能画才换,画不了原样保留,不静默丢图。

import type { AnalysisPayload, ChartSpec } from '../api/types'
import {
  EXPORT_MAX_COLS,
  EXPORT_MAX_QUERIES,
  EXPORT_MAX_ROWS,
  sessionFileBase,
} from '../stores/chat'
import type { SessionAnalysisLabels, Turn } from '../stores/chat'
import { fmtDateTime, fmtVal } from './format'
import { renderMarkdown } from './markdown'

/** HTML 报告文案(由调用方按 ui.lang 从 i18n 取;analysis 与 md 导出共用)。 */
export interface SessionReportLabels {
  generatedAt: string
  rounds: string
  results: string
  rows: string
  cols: string
  /** 图表 figcaption 兜底(图表自带 title 时用 title)。 */
  chart: string
  analysis: SessionAnalysisLabels
}

export interface SessionReportOpts {
  title?: string
  sessionId: string
  labels: SessionReportLabels
  lang?: string
  now?: Date
}

// ── 基础工具 ────────────────────────────────────────────────────────

function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 多行文本压成一行(heading/摘要位置不能有换行)。 */
function oneLine(s: string): string {
  return s.replace(/\s*\r?\n\s*/g, ' ').trim()
}

/** 与 markdown.ts 的 td_open 同一套:数字样单元格右对齐。 */
const NUMERIC_RE = /^-?\d[\d,]*\.?\d*%?$/

/** 结果单元格 → `<td>`(null 空串,对象 JSON,一律转义)。 */
function cell(v: unknown): string {
  if (v === null || v === undefined) return '<td></td>'
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  const cls = NUMERIC_RE.test(s.trim()) ? ' class="numeric"' : ''
  return `<td${cls}>${esc(s)}</td>`
}

// ── 内联 SVG 图表 ──────────────────────────────────────────────────
//
// 报告要离线打开,不能引 ECharts;这里手绘最小 SVG。只画「单序列
// bar/line」(与 spark.py 的 ASCII 覆盖面一致)——多序列/饼图两边都不画,
// 结果表兜底。调色板与前端主题固定对齐(报告是浅色主题的单文件)。

const SVG_W = 680
const SVG_H = 264
const PAD_L = 58
const PAD_R = 14
const PAD_T = 16
const PAD_B = 56
const PLOT_W = SVG_W - PAD_L - PAD_R
const PLOT_H = SVG_H - PAD_T - PAD_B
const C_ACCENT = '#4f46e5'
const C_OK = '#15803d'
const C_DANGER = '#dc2626'
const C_BORDER = '#e4e4e7'
const C_GRID_TEXT = '#71717a'

/** 单序列数据(base/deltas/current 之外的通用图):列名 + 数值。 */
function singleSeries(
  spec: ChartSpec | null | undefined,
): { cats: string[]; vals: number[] } | null {
  const series = spec?.series ?? []
  if (series.length !== 1) return null
  const raw = series[0].data ?? []
  const vals: number[] = []
  for (const v of raw) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return null
    vals.push(v)
  }
  const cats = (spec?.categories ?? []).map(String)
  const n = Math.min(cats.length, vals.length)
  if (!n) return null
  return { cats: cats.slice(0, n), vals: vals.slice(0, n) }
}

/** 「好看」的刻度步长(1/2/5 × 10^n),不让刻度标签出现 7,534 这种数。 */
function niceStep(span: number, target: number): number {
  const raw = span > 0 ? span / target : 1
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const norm = raw / mag
  const step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1
  return step * mag
}

function fmtTick(v: number): string {
  return fmtVal(Math.abs(v) >= 10 ? Math.round(v) : Number(v.toFixed(2)))
}

/** 坐标框:网格线 + 刻度 + 零线;返回 y 映射。 */
function chartFrame(tops: number[], bots: number[]): {
  y: (v: number) => number
  grid: string[]
} {
  let hi = Math.max(0, ...tops)
  let lo = Math.min(0, ...bots)
  if (hi === lo) {
    hi += 1
    lo -= 1
  }
  const pad = (hi - lo) * 0.08
  hi += pad
  lo -= pad
  const y = (v: number) => PAD_T + ((hi - v) / (hi - lo)) * PLOT_H
  const grid: string[] = []
  const step = niceStep(hi - lo, 4)
  const start = Math.ceil(lo / step) * step
  for (let v = start; v <= hi + 1e-9; v += step) {
    const yy = y(v)
    grid.push(
      `<line x1="${PAD_L}" x2="${SVG_W - PAD_R}" y1="${yy}" y2="${yy}" stroke="${C_BORDER}"/>`,
      `<text x="${PAD_L - 8}" y="${yy + 4}" text-anchor="end" font-size="11" fill="${C_GRID_TEXT}">${esc(fmtTick(v))}</text>`,
    )
    if (grid.length > 40) break // 防御:极端步长下的栅格爆炸
  }
  if (lo < 0) {
    const zy = y(0)
    grid.push(
      `<line x1="${PAD_L}" x2="${SVG_W - PAD_R}" y1="${zy}" y2="${zy}" stroke="#a1a1aa"/>`,
    )
  }
  return { y, grid }
}

/** 类目轴标签(长标签旋转 30°;超长截断,全文进 <title>)。 */
function catLabels(cats: string[], cx: (i: number) => number): string[] {
  const rotate = cats.length > 6 || cats.some((c) => c.length > 6)
  const baseY = PAD_T + PLOT_H + 18
  return cats.map((c, i) => {
    const shown = c.length > 14 ? `${c.slice(0, 13)}…` : c
    const full = `<title>${esc(c)}</title>`
    const x = cx(i)
    return rotate
      ? `<text transform="translate(${x} ${baseY}) rotate(-30)" text-anchor="end" font-size="11" fill="${C_GRID_TEXT}">${full}${esc(shown)}</text>`
      : `<text x="${x}" y="${baseY + 4}" text-anchor="middle" font-size="11" fill="${C_GRID_TEXT}">${full}${esc(shown)}</text>`
  })
}

interface SvgBar {
  /** 条从 base 画到 base + v(瀑布的浮条 = base 非零)。 */
  base: number
  v: number
  color: string
  /** 值标签(带符号由调用方定),空串 = 不标。 */
  label: string
}

/** 条形/瀑布共用的柱图(坐标值域含 0;负值条画在零线下方)。 */
function barsChart(cats: string[], bars: SvgBar[], title?: string): string {
  const n = bars.length
  if (!n || n !== cats.length) return ''
  const tops = bars.map((b) => Math.max(b.base, b.base + b.v))
  const bots = bars.map((b) => Math.min(b.base, b.base + b.v))
  const { y, grid } = chartFrame(tops, bots)
  const slot = PLOT_W / n
  const barW = Math.min(56, slot * 0.62)
  const cx = (i: number) => PAD_L + slot * (i + 0.5)
  const out: string[] = []
  bars.forEach((b, i) => {
    const top = Math.max(b.base, b.base + b.v)
    const bot = Math.min(b.base, b.base + b.v)
    const yy = y(top)
    const hh = Math.max(1, y(bot) - y(top))
    out.push(
      `<rect x="${cx(i) - barW / 2}" y="${yy}" width="${barW}" height="${hh}" rx="3" fill="${b.color}">` +
        `<title>${esc(cats[i])}: ${esc(b.label || fmtVal(b.v))}</title></rect>`,
    )
    if (b.label) {
      const ly = b.v >= 0 ? yy - 5 : y(bot) + 14
      out.push(
        `<text x="${cx(i)}" y="${ly}" text-anchor="middle" font-size="11" fill="#52525b">${esc(b.label)}</text>`,
      )
    }
  })
  const sub = title ? `<title>${esc(title)}</title>` : ''
  return (
    `<svg viewBox="0 0 ${SVG_W} ${SVG_H}" role="img" aria-label="${esc(title || 'chart')}" xmlns="http://www.w3.org/2000/svg">` +
    sub +
    grid.join('') +
    out.join('') +
    catLabels(cats, cx).join('') +
    '</svg>'
  )
}

/** 折线图(单序列;点 + 值标签)。 */
function lineChart(cats: string[], vals: number[], title?: string): string {
  const n = vals.length
  if (!n || n !== cats.length) return ''
  const { y, grid } = chartFrame(vals, vals)
  const slot = PLOT_W / n
  const cx = (i: number) => PAD_L + slot * (i + 0.5)
  const pts = vals.map((v, i) => `${cx(i)},${y(v)}`).join(' ')
  const out: string[] = [
    `<polyline points="${pts}" fill="none" stroke="${C_ACCENT}" stroke-width="2"/>`,
  ]
  vals.forEach((v, i) => {
    out.push(`<circle cx="${cx(i)}" cy="${y(v)}" r="3" fill="${C_ACCENT}"><title>${esc(cats[i])}: ${esc(fmtVal(v))}</title></circle>`)
    out.push(
      `<text x="${cx(i)}" y="${v >= 0 ? y(v) - 7 : y(v) + 16}" text-anchor="middle" font-size="11" fill="#52525b">${esc(fmtVal(v))}</text>`,
    )
  })
  const sub = title ? `<title>${esc(title)}</title>` : ''
  return (
    `<svg viewBox="0 0 ${SVG_W} ${SVG_H}" role="img" aria-label="${esc(title || 'chart')}" xmlns="http://www.w3.org/2000/svg">` +
    sub +
    grid.join('') +
    out.join('') +
    catLabels(cats, cx).join('') +
    '</svg>'
  )
}

/** 主结果图(bar/line 单序列)—— 与 ASCII 覆盖面一致;画不了返回 ''。 */
function svgBarLine(spec: ChartSpec | null | undefined): string {
  if (!spec || (spec.type !== 'bar' && spec.type !== 'line')) return ''
  const s = singleSeries(spec)
  if (!s) return ''
  if (spec.type === 'line') return lineChart(s.cats, s.vals, spec.title)
  return barsChart(
    s.cats,
    s.vals.map((v) => ({ base: 0, v, color: C_ACCENT, label: fmtVal(v) })),
    spec.title,
  )
}

/** 归因瀑布(几何与前端 buildWaterfallOption 一致:基期/当前整条,
 *  Δ 浮条;涨绿跌红)。非 waterfall / 残缺数据 → ''(ASCII 原样保留)。 */
function svgWaterfall(spec: ChartSpec | null | undefined): string {
  if (!spec || spec.type !== 'waterfall') return ''
  const s = singleSeries(spec)
  if (!s || s.vals.length < 2) return ''
  const { cats, vals } = s
  const n = vals.length
  let run = vals[0]
  const bars: SvgBar[] = vals.map((v, i) => {
    if (i === 0 || i === n - 1) {
      return { base: 0, v, color: C_ACCENT, label: fmtVal(v) }
    }
    const bar: SvgBar = {
      base: Math.min(run, run + v),
      v,
      color: v >= 0 ? C_OK : C_DANGER,
      label: `${v >= 0 ? '+' : ''}${fmtVal(v)}`,
    }
    run += v
    return bar
  })
  return barsChart(cats, bars, spec.title)
}

function figure(svg: string, caption: string): string {
  if (!svg) return ''
  return (
    '<figure class="report-chart">' +
    svg +
    (caption ? `<figcaption>${esc(caption)}</figcaption>` : '') +
    '</figure>'
  )
}

/** 回答里的 ASCII 图表块 → 占位符(渲染前),渲染后再把占位符回填成
 *  内联 SVG。**不能把 HTML 直接塞进 markdown 源** —— renderMarkdown 走
 *  markdown-it(html:false),裸 HTML 会被整段转义成文本(实测踩过)。
 *
 *  与 markdown.ts::stripAsciiChart 同一正则(块 = `**图表**: 标题` +
 *  围栏)。位置映射成立的前提:output 节点的区块顺序是「主图在前、
 *  归因瀑布在后」,且两侧「能否画出」的条件一致(单序列 bar/line 对
 *  ASCII 与 SVG 同真同假)—— 不成立的极端形状下宁可不换,ASCII 原样
 *  保留,不静默丢图。`used[i]` 报告第 i 个候选是否真的替换掉了某个块,
 *  调用方据此决定「未消费的图」是否另行兜底渲染(避免重复或丢失)。 */
const ASCII_CHART_RE = /^(\*\*(?:图表|Chart)\*{0,1}[^:\n]*:?[^\n]*)\n```[\s\S]*?```/gm

function chartSlot(i: number): string {
  return `TROVE-CHART-SLOT-${i}`
}

function stubAsciiCharts(
  src: string,
  replacements: (string | undefined)[],
): { text: string; used: boolean[] } {
  const used = replacements.map(() => false)
  ASCII_CHART_RE.lastIndex = 0
  let i = 0
  const text = src.replace(ASCII_CHART_RE, (m) => {
    const idx = i++
    if (idx >= replacements.length || !replacements[idx]) return m
    used[idx] = true
    return chartSlot(idx)
  })
  return { text, used }
}

/** 渲染后的 HTML 里把占位段落回填成图(markdown-it 会把独立占位行包成
 *  `<p>…</p>`;未被包住的兜底按裸串替换)。 */
function fillChartSlots(
  html: string,
  replacements: (string | undefined)[],
): string {
  let out = html
  replacements.forEach((rep, i) => {
    if (!rep) return
    const mark = chartSlot(i)
    out = out.split(`<p>${mark}</p>`).join(rep)
    if (out.includes(mark)) out = out.split(mark).join(rep)
  })
  return out
}

// ── 轮次渲染 ────────────────────────────────────────────────────────

function resultsFigure(turn: Turn, labels: SessionReportLabels): string {
  const summary = turn.summary
  const columns = (summary?.columns ?? []).map(String)
  const all = (summary?.rows?.length
    ? summary.rows
    : summary?.rows_preview ?? []) as unknown[][]
  if (!columns.length || !all.length) return ''
  const shownCols = columns.slice(0, EXPORT_MAX_COLS)
  const shownRows = all.slice(0, EXPORT_MAX_ROWS)
  const truncated =
    all.length > shownRows.length || columns.length > shownCols.length
  const dims = `${all.length} ${labels.rows} × ${columns.length} ${labels.cols}`
  const shownDims =
    `${shownRows.length} ${labels.rows} × ${shownCols.length} ${labels.cols}`
  const out: string[] = [
    '<figure class="report-results">',
    `<figcaption>${esc(labels.results)} (${esc(truncated ? `${shownDims} / ${dims}` : dims)})</figcaption>`,
    '<div class="table-wrap"><table><thead><tr>',
    ...shownCols.map((c) => `<th>${esc(c)}</th>`),
    '</tr></thead><tbody>',
  ]
  for (const row of shownRows) {
    out.push(`<tr>${shownCols.map((_, j) => cell(row[j])).join('')}</tr>`)
  }
  out.push('</tbody></table></div>', '</figure>')
  return out.join('')
}

function analysisBlock(
  a: AnalysisPayload | null,
  wfFigure: string,
  labels: SessionReportLabels,
): string[] {
  if (!a) return []
  const al = labels.analysis
  const queries = a.evidence?.queries ?? []
  if (!wfFigure && !a.partial && !queries.length) return []
  const out: string[] = ['<section class="report-ana">', `<h3>${esc(al.title)}</h3>`]
  if (a.partial) {
    out.push(`<p class="notice">${esc(al.partial)}: ${esc(al.partialHint)}</p>`)
  }
  if (wfFigure) out.push(wfFigure)
  if (queries.length) {
    const shown = queries.slice(0, EXPORT_MAX_QUERIES)
    out.push(
      `<details class="report-evid"><summary>${esc(al.evidence)} (${queries.length})</summary>`,
    )
    shown.forEach((q, i) => {
      const purpose =
        al.purposes[String(q.purpose ?? '')] ?? String(q.purpose ?? '')
      const meta = [purpose, q.period, q.filter]
        .filter(Boolean)
        .map(String)
        .join(' · ')
      out.push(`<div class="q"><div class="q-head">${i + 1}. ${esc(meta)}</div>`)
      const sql = (q.sql ?? '').trim()
      if (sql) out.push(`<pre>${esc(sql)}</pre>`)
      if (q.truncated) out.push(`<div class="q-note">${esc(al.truncated)}</div>`)
      out.push('</div>')
    })
    if (queries.length > shown.length) {
      out.push(
        `<div class="q-note">${esc(al.truncated)} (${shown.length}/${queries.length})</div>`,
      )
    }
    out.push('</details>')
  }
  out.push('</section>')
  return out
}

function renderTurn(turn: Turn, idx: number, labels: SessionReportLabels): string {
  const summary = turn.summary
  const analysis = summary?.analysis ?? null
  // 能画的图先建好,再决定 ASCII 块换不换(主图在前、瀑布在后)。
  const mainFigure = figure(
    svgBarLine(summary?.chart),
    summary?.chart?.title || labels.chart,
  )
  const wfFigureAll = analysis
    ? figure(
        svgWaterfall(analysis.charts?.[0]),
        analysis.charts?.[0]?.title || labels.chart,
      )
    : ''
  const out: string[] = [
    '<section class="turn">',
    `<h2><span class="idx">${idx}</span>${esc(oneLine(turn.question || ''))}</h2>`,
  ]
  const answer = (turn.answer || turn.synthesis || '').trim()
  let wfFigure = wfFigureAll
  if (answer) {
    const repl = [mainFigure || undefined, wfFigureAll || undefined]
    const { text, used } = stubAsciiCharts(answer, repl)
    let html = fillChartSlots(renderMarkdown(text), repl)
    // 图能画、答案里却没有对应的 ASCII 块(极端形状)→ 直接附图,不丢图。
    if (mainFigure && !used[0]) html += mainFigure
    wfFigure = used[1] ? '' : wfFigureAll // 已替换进答案 → 分析块不重复
    out.push(`<div class="answer">${html}</div>`)
  } else if (turn.error) {
    out.push(`<p class="turn-error">${esc(oneLine(turn.error))}</p>`)
  }
  const sql = (summary?.sql || '').trim()
  if (sql) {
    out.push('<details class="report-sql"><summary>SQL</summary>')
    out.push(`<pre>${esc(sql)}</pre>`)
    out.push('</details>')
  }
  out.push(...analysisBlock(analysis, wfFigure, labels))
  out.push(resultsFigure(turn, labels))
  out.push('</section>')
  return out.join('\n')
}

// ── 报告骨架 ────────────────────────────────────────────────────────

const REPORT_CSS = `
*{box-sizing:border-box}
body{margin:0;background:#f6f6f7;color:#18181b;font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.report{max-width:880px;margin:0 auto;padding:44px 36px 64px;background:#fff;min-height:100vh;box-shadow:0 0 0 1px #ececee}
.report-head{border-bottom:2px solid #18181b;padding-bottom:16px;margin-bottom:8px}
.brand{font-size:12px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:#4f46e5}
.report-head h1{font-size:23px;margin:6px 0 8px}
.meta{font-size:12.5px;color:#71717a}
.turn{padding:26px 0 8px;border-top:1px solid #e4e4e7;margin-top:26px}
.turn h2{font-size:18px;margin:0 0 12px;line-height:1.45}
.turn h2 .idx{display:inline-block;min-width:26px;margin-right:8px;color:#a1a1aa;font-weight:600}
.turn h3{font-size:15px;margin:16px 0 8px}
.answer{overflow-wrap:anywhere}
.answer ul,.answer ol{padding-left:22px}
.answer blockquote{margin:10px 0;padding:4px 14px;border-left:3px solid #d4d4d8;color:#52525b;background:#fafafa}
.table-wrap{overflow-x:auto;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border:1px solid #e4e4e7;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#fafafa;font-weight:600;white-space:nowrap}
td.numeric{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{background:#f4f4f5;padding:1px 5px;border-radius:4px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.9em}
pre{background:#f8fafc;border:1px solid #e4e4e7;border-radius:8px;padding:10px 12px;overflow-x:auto;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.55}
pre code{background:none;padding:0}
details.report-sql,details.report-evid{margin:12px 0}
details summary{cursor:pointer;font-weight:600;font-size:13.5px;color:#3f3f46}
details summary:hover{color:#4f46e5}
.notice{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;padding:8px 12px;font-size:13px;margin:8px 0}
.turn-error{color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:8px 12px}
figure{margin:14px 0}
figcaption{font-size:12.5px;color:#71717a;margin-top:6px}
svg{max-width:100%;height:auto;display:block}
svg text{font-family:inherit}
.report-ana{background:#fafafa;border:1px solid #e4e4e7;border-radius:10px;padding:14px 18px;margin:14px 0}
.report-ana h3{margin:2px 0 8px}
.report-evid .q{margin:10px 0}
.report-evid .q-head{font-size:13px;color:#3f3f46;font-weight:600;margin-bottom:6px}
.q-note{font-size:12.5px;color:#a16207}
.report-results figcaption{font-size:13px;color:#3f3f46;font-weight:600;margin:0 0 6px}
.report-foot{margin-top:40px;padding-top:14px;border-top:1px solid #e4e4e7;font-size:12px;color:#a1a1aa}
@media print{body{background:#fff}.report{box-shadow:none;max-width:none;padding:0}.turn{break-inside:avoid-page}}
`

/** 整段会话 → 一个自包含 HTML 文档(单文件,无外部资源)。 */
export function buildSessionHtml(turns: Turn[], opts: SessionReportOpts): string {
  const { labels } = opts
  const now = opts.now ?? new Date()
  const sid = opts.sessionId || ''
  const head = (opts.title ?? '').trim() || sid.slice(0, 8) || 'session'
  const lang = opts.lang === 'en' ? 'en' : 'zh-CN'
  return [
    '<!doctype html>',
    `<html lang="${lang}">`,
    '<head>',
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${esc(head)}</title>`,
    `<style>${REPORT_CSS}</style>`,
    '</head>',
    '<body>',
    '<div class="report">',
    '<header class="report-head">',
    '<div class="brand">Trove</div>',
    `<h1>${esc(head)}</h1>`,
    `<div class="meta">${esc(labels.generatedAt)}: ${fmtDateTime(now.toISOString())} · ` +
      `${esc(labels.rounds)}: ${turns.length} · session:${esc(sid.slice(0, 8))}</div>`,
    '</header>',
    '<main>',
    ...turns.map((turn, i) => renderTurn(turn, i + 1, labels)),
    '</main>',
    '<footer class="report-foot">Trove</footer>',
    '</div>',
    '</body>',
    '</html>',
    '',
  ].join('\n')
}

/** 下载文件名:`<标题或首问截断>-<YYYYMMDD>.html`(与 md 导出同一清洗)。 */
export function sessionReportFilename(
  title: string | undefined,
  sessionId: string,
  now: Date = new Date(),
): string {
  return `${sessionFileBase(title, sessionId, now)}.html`
}
