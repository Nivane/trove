// Tokenize LLM markdown output into a render plan. Tables become interactive
// DataTable components, fenced ```sql blocks become SqlBlock components, and
// everything else stays as markdown (sanitized before render). Only top-level
// blocks are extracted (code fences / indented lists are respected).

export interface TableBlock {
  type: 'table'
  headers: string[]
  rows: string[][]
}
export interface SqlBlock {
  type: 'sql'
  code: string
}
export interface DetailsBlock {
  type: 'details'
  summary: string
  blocks: Block[]
}
export interface MdBlock {
  type: 'md'
  text: string
}
export type Block = MdBlock | TableBlock | SqlBlock | DetailsBlock

function isSeparatorRow(line: string): boolean {
  return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes('-')
}
function isPipeRow(line: string): boolean {
  return line.trim().startsWith('|') && line.trim().endsWith('|')
}

function parsePipeRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim().replace(/｜/g, '|'))
}

/** True when a line opens a fenced code block (``` or ~~~). Returns fence char. */
function fenceChar(line: string): string | null {
  const m = line.trim().match(/^(`{3,}|~{3,})/)
  return m ? m[1][0] : null
}

const DETAILS_OPEN_RE = /^<details>\s*$/i
const DETAILS_CLOSE_RE = /^<\/details>\s*$/i
const DETAILS_SUMMARY_RE = /<summary>(.*?)<\/summary>/is

/** True when indent is 0 (top-level, not inside a list/blockquote). */
function isTopLevel(line: string): boolean {
  return !/^\s/.test(line) || /^\s*$/.test(line)
}

/**
 * Split markdown source into an ordered list of blocks. Fenced sql blocks and
 * pipe tables are extracted; all other content accumulates into md segments.
 */
export function tokenize(src: string): Block[] {
  const lines = src.split('\n')
  const blocks: Block[] = []
  const md: string[] = []

  const flushMd = () => {
    if (md.length) {
      const text = md.join('\n')
      // Whitespace-only md segments (blank separators between extracted
      // blocks) are layout noise — drop them.
      if (text.trim() !== '') {
        blocks.push({ type: 'md', text })
      }
      md.length = 0
    }
  }

  let i = 0
  while (i < lines.length) {
    const line = lines[i]

    // Collapsible detail section emitted by the backend output node
    // (<details><summary>…</summary>…markdown…</details>). Rendered as a
    // native <details> in the web UI; inner content is tokenized recursively
    // (nested details, tables and sql fences all work inside).
    if (DETAILS_OPEN_RE.test(line) && isTopLevel(line)) {
      const raw: string[] = []
      i++
      while (i < lines.length && !DETAILS_CLOSE_RE.test(lines[i])) {
        raw.push(lines[i])
        i++
      }
      i++ // skip </details>
      const inner = raw.join('\n')
      const sm = inner.match(DETAILS_SUMMARY_RE)
      const summary = sm ? sm[1].trim() : ''
      flushMd()
      blocks.push({
        type: 'details',
        summary,
        blocks: tokenize(inner.replace(DETAILS_SUMMARY_RE, '')),
      })
      continue
    }

    const fence = fenceChar(line)

    if (fence && isTopLevel(line)) {
      // fenced code block
      const lang = line
        .trim()
        .replace(/^[`~]+/, '')
        .trim()
      const code: string[] = []
      i++
      while (i < lines.length && !fenceChar(lines[i])) {
        code.push(lines[i])
        i++
      }
      i++ // skip closing fence
      if (lang.toLowerCase() === 'sql') {
        flushMd()
        blocks.push({ type: 'sql', code: code.join('\n').trim() })
      } else {
        md.push('```' + lang + '\n' + code.join('\n') + '\n```')
      }
      continue
    }

    if (
      isPipeRow(line) &&
      isSeparatorRow(lines[i + 1] ?? '') &&
      isTopLevel(line)
    ) {
      const block: string[] = []
      let j = i
      while (
        j < lines.length &&
        (isPipeRow(lines[j]) || isSeparatorRow(lines[j]))
      ) {
        block.push(lines[j])
        j++
      }
      flushMd()
      blocks.push({
        type: 'table',
        headers: parsePipeRow(block[0]),
        rows: block.slice(2).map(parsePipeRow),
      })
      i = j
      continue
    }

    md.push(line)
    i++
  }
  flushMd()
  return blocks
}
