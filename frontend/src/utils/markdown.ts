// markdown-it + DOMPurify replacement for the hand-rolled renderer.
// LLM output is untrusted → sanitize after render (target HTML).
// Ports the vanilla behaviors: numeric right-align in pipe tables and
// full-width ｜ escaping for literal pipes in cells.

import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import type Token from 'markdown-it/lib/token.mjs'

// Numeric-looking cells get right alignment (vanilla NUMERIC_RE behavior)
const NUMERIC_RE = /^-?\d[\d,]*\.?\d*%?$/

export function renderMarkdown(src: string): string {
  const md = new MarkdownIt({
    html: false,
    linkify: true,
    breaks: true,
  })
  md.renderer.rules.table_open = function () {
    return '<div class="table-wrap"><table>'
  }
  md.renderer.rules.table_close = function () {
    return '</table></div>'
  }
  md.renderer.rules.td_open = function (tokens: Token[], idx: number) {
    const token = tokens[idx]
    const align = token.attrGet('align')
    const content = tokens[idx + 1]?.content ?? ''
    const cls: string[] = []
    if (align) cls.push(`align-${align}`)
    if (NUMERIC_RE.test(content.trim())) cls.push('numeric')
    return `<td${cls.length ? ` class="${cls.join(' ')}"` : ''}>`
  }
  md.renderer.rules.th_open = function (tokens: Token[], idx: number) {
    const token = tokens[idx]
    const align = token.attrGet('align')
    return `<th${align ? ` class="align-${align}"` : ''}>`
  }

  // mdCell() port: literal pipes inside cells become full-width ｜ so the
  // markdown-it table splitter keeps row shape.
  const escaped = src
    .split('\n')
    .map((line) => {
      if (!line.trim().startsWith('|')) return line
      return line.replace(
        /`([^`]*)`/g,
        (_m, code: string) => '`' + code.replace(/\|/g, '｜') + '`',
      )
    })
    .join('\n')

  const html = md.render(escaped)
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ['target', 'rel'],
  })
}

/** 结论当主角（答案卡视觉层）：给 `### 结论 / ### Conclusion` 标题挂
 *  `concl-label`、其后的首段挂 `concl`，并把段落里第一个数字包进
 *  `hero-num`（品牌数字字号）。输入是 sanitize 之后的 HTML，这里只加
 *  class 与一个 span（不引入任何来自源的标记）；认不出结论标题就原样
 *  返回 —— 宁可不放大，也不把别的段落误当结论。 */
export function enhanceConclusionHtml(html: string): string {
  if (typeof DOMParser === 'undefined') return html
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const headings = doc.body.querySelectorAll('h3')
  let heading: Element | null = null
  for (const h of headings) {
    const text = (h.textContent ?? '').trim()
    if (text === '结论' || text === 'Conclusion') {
      heading = h
      break
    }
  }
  if (!heading) return html
  heading.classList.add('concl-label')
  const para = heading.nextElementSibling
  if (!para || para.tagName !== 'P') return doc.body.innerHTML
  para.classList.add('concl')
  wrapFirstNumber(para)
  return doc.body.innerHTML
}

// 金额/占比一类：货币符号 + 千分位 + 小数 + 百分号/中文量级。
const HERO_NUM_RE = /[$¥€]?\d[\d,]*(?:\.\d+)?(?:%|亿|万|千)?/
// 中文夹单字数字（第3名 / 共5个）不是主角数字，跳过。
const CJK_RE = /[一-鿿]/

function wrapFirstNumber(para: Element): void {
  const walker = para.ownerDocument.createTreeWalker(para, NodeFilter.SHOW_TEXT)
  let node: Text | null
  while ((node = walker.nextNode() as Text | null)) {
    const text = node.nodeValue ?? ''
    const m = HERO_NUM_RE.exec(text)
    if (!m) continue
    const before = text[m.index - 1]
    const after = text[m.index + m[0].length]
    if (before && after && CJK_RE.test(before) && CJK_RE.test(after)) continue
    const span = para.ownerDocument.createElement('span')
    span.className = 'hero-num'
    span.textContent = m[0]
    const rest = node.splitText(m.index)
    rest.nodeValue = rest.nodeValue!.slice(m[0].length)
    node.parentNode?.insertBefore(span, rest)
    return
  }
}

/** Strip the terminal ASCII chart block the output node embeds (the web
 * UI renders the real chart via ECharts instead). The backend emits:
 *   **Chart**: title / **图表: title**  followed by a ``` ``` fenced block. */
export function stripAsciiChart(src: string): string {
  return src.replace(
    /^(\*\*(?:图表|Chart)\*{0,1}[^:\n]*:?[^\n]*)\n```[\s\S]*?```/gm,
    '',
  )
}
