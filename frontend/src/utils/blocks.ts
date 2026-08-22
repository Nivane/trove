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
export interface MdBlock {
  type: 'md'
  text: string
}
export type Block = MdBlock | TableBlock | SqlBlock

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
      blocks.push({ type: 'md', text: md.join('\n') })
      md.length = 0
    }
  }

  let i = 0
  while (i < lines.length) {
    const line = lines[i]
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
