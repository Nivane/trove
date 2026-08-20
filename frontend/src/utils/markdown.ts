// markdown-it + DOMPurify replacement for the hand-rolled renderer.
// LLM output is untrusted → sanitize after render (target HTML).
// Ports the vanilla behaviors: numeric right-align in pipe tables and
// full-width ｜ escaping for literal pipes in cells.

import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

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
  md.renderer.rules.td_open = function (tokens, idx) {
    const token = tokens[idx]
    const align = token.attrGet('align')
    const content = tokens[idx + 1]?.content ?? ''
    const cls: string[] = []
    if (align) cls.push(`align-${align}`)
    if (NUMERIC_RE.test(content.trim())) cls.push('numeric')
    return `<td${cls.length ? ` class="${cls.join(' ')}"` : ''}>`
  }
  md.renderer.rules.th_open = function (tokens, idx) {
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
      return line.replace(/`([^`]*)`/g, (_m, code: string) =>
        '`' + code.replace(/\|/g, '｜') + '`',
      )
    })
    .join('\n')

  const html = md.render(escaped)
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ['target', 'rel'],
  })
}

/** Strip the terminal ASCII chart block the output node embeds (the web
 * UI renders the real chart via ECharts instead). */
export function stripAsciiChart(src: string): string {
  // eslint-disable-next-line no-control-regex
  return src.replace(/\*\*图表\/Chart\*\*\s*```[\s\S]*?```/g, '')
}
