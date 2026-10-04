#!/usr/bin/env node
/**
 * check-font-size.mjs — 字号下限门（对话页/管理台统一 12px 起）。
 *
 * 背景：中文产品的最小可读字号是 12px（Ant Design / Arco 的 small 档都停在
 * 12），11px 及以下在 Windows / 低分屏上笔画直接发糊。字号体系已在
 * `src/assets/styles/tokens.css` 里定义（--fs-2xs: 12px 起步），本脚本防止
 * 有人再裸写小于 12px 的字号把体系撬开。
 *
 * 判定：
 *   - `font-size: <n>px`  且 n  < 12   → 违规
 *   - `font-size: <n>rem` 且 n  < 0.75 → 违规（0.75rem = 12px）
 *   - `font-size: 0`（无单位）放行 —— 图标按钮隐藏文字是既有手法
 *
 * 扫描范围 `src/` 下的 .css/.vue/.ts/.js/.mjs/.html。块注释（CSS 的
 * 星号注释、HTML 注释）先按行数等价地挖空，避免注释里的示例字号误报。
 *
 * 白名单：确需保留的更小字号必须在此登记并写明理由；登记的条目若一条都没
 * 命中，脚本会当作「过期白名单」报错 —— 防止修好了却把豁免留在原地。
 *
 * 用法：node scripts/check-font-size.mjs [扫描目录]
 * 退出码：0 = 干净；1 = 有违规或有过期白名单条目。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC_DIR = process.argv[2]
  ? resolve(process.argv[2])
  : resolve(HERE, '../src')

/** 12px 是中文情境下的最小可读字号。 */
const MIN_PX = 12

/** 豁免清单：{ file: 'src 下的路径', match: '命中行内的片段', reason: '为什么' } */
const WHITELIST = [
  // 例：{ file: 'src/…/Foo.vue', match: 'font-size: 9px', reason: '…' }
]

const EXTS = ['.css', '.vue', '.ts', '.js', '.mjs', '.html']
const PATTERN = /font-size\s*:\s*([0-9]*\.?[0-9]+)(px|rem|em)\b/g

/** 按行数等价地挖空注释：注释里的字面量不再参与匹配，行号保持真实。 */
function blankComments(text) {
  const keepLines = (m) => m.replace(/[^\n]/g, ' ')
  return text
    .replace(/\/\*[\s\S]*?\*\//g, keepLines)
    .replace(/<!--[\s\S]*?-->/g, keepLines)
}

/** 递归收集待扫描文件（相对 SRC_DIR 的路径按 posix 分隔符输出）。 */
function collect(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) out.push(...collect(full))
    else if (EXTS.some((e) => name.endsWith(e))) out.push(full)
  }
  return out
}

function isViolation(value, unit) {
  if (value === 0) return false // font-size: 0 无单位，这里是对 `0px` 的兜底
  if (unit === 'px') return value < MIN_PX
  return value < MIN_PX / 16 // rem / em：0.75rem = 12px
}

const hits = []
for (const file of collect(SRC_DIR)) {
  const rel = `src/${relative(SRC_DIR, file).split('\\').join('/')}`
  const lines = blankComments(readFileSync(file, 'utf8')).split('\n')
  lines.forEach((line, i) => {
    PATTERN.lastIndex = 0
    let m
    while ((m = PATTERN.exec(line))) {
      const value = Number.parseFloat(m[1])
      if (!isViolation(value, m[2])) continue
      const hit = { file: rel, line: i + 1, text: m[0] }
      const waived = WHITELIST.find(
        (w) => w.file === rel && (!w.match || line.includes(w.match)),
      )
      if (waived) waived.used = true
      else hits.push(hit)
    }
  })
}

const staleWhitelist = WHITELIST.filter((w) => !w.used)

for (const hit of hits) {
  console.error(
    `  ${hit.file}:${hit.line}  ${hit.text}` +
      `  → 低于 ${MIN_PX}px，改用 var(--fs-2xs)（12px）`,
  )
}
for (const w of staleWhitelist) {
  console.error(`  [过期白名单] ${w.file} 未命中任何字号 — 删掉该条豁免`)
}

if (hits.length || staleWhitelist.length) {
  console.error(
    `\n✗ 字号下限门未通过：${hits.length} 处 <${MIN_PX}px` +
      (staleWhitelist.length ? `，${staleWhitelist.length} 条过期白名单` : ''),
  )
  process.exit(1)
}

console.log(`✓ 字号下限门通过：${SRC_DIR} 下无小于 ${MIN_PX}px 的字号`)
