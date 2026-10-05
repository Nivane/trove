/**
 * Capture the screenshots used by the docs site pages under docs/user|admin
 * (npm run shots). Drives the locally installed Google Chrome through
 * playwright-core — no browser download, works offline.
 *
 * Which instance to capture is configurable so the same script serves both a
 * throwaway capture instance (see docs) and a normal local stack:
 *
 *   TROVE_SHOT_BASE        frontend origin        (default http://localhost:5173)
 *   TROVE_SHOT_USER        user-side login        (default admin)
 *   TROVE_SHOT_PASS        user-side password     (no default — required)
 *   TROVE_SHOT_ADMIN_USER  admin-side login       (default: same as USER)
 *   TROVE_SHOT_ADMIN_PASS  admin-side password    (default: same as PASS)
 *   TROVE_SHOT_OUT         output dir             (default ../docs/assets/shots)
 *   TROVE_SHOT_ONLY        comma-separated shot names to run (dev iteration)
 *   TROVE_SHOT_W           downsample width       (default 0 = keep @2x, HD)
 *
 * User-side shots are taken as a non-admin account (the seeded sessions and
 * their feedback live there); admin shots log in as an admin. Every run
 * rewrites _manifest.json next to the images: capture time, the frontend
 * commit the UI came from, and each file's pixel size. Bitmaps go stale
 * silently, the manifest is what makes that visible.
 */
import { execSync } from 'node:child_process'
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'

const HERE = path.dirname(fileURLToPath(import.meta.url))

const BASE = process.env.TROVE_SHOT_BASE || 'http://localhost:5173'
const USER = process.env.TROVE_SHOT_USER || 'admin'
const PASS = process.env.TROVE_SHOT_PASS
const ADMIN_USER = process.env.TROVE_SHOT_ADMIN_USER || USER
const ADMIN_PASS = process.env.TROVE_SHOT_ADMIN_PASS || PASS
const OUT = path.resolve(
  process.cwd(),
  process.env.TROVE_SHOT_OUT || path.join(HERE, '../../docs/assets/shots'),
)
const ONLY = (process.env.TROVE_SHOT_ONLY || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)

if (!PASS) {
  console.error('TROVE_SHOT_PASS is required (no password is baked into the repo).')
  process.exit(2)
}

const VIEWPORT = { width: 1440, height: 900 }
const SCALE = 2
// HD by default: keep the native @2x capture (2880×1800) — 3.5× the pixels of
// the docs page's 812px reading column at DPR 2, so zooming into UI text
// stays legible (the earlier 1624px downsample was exactly DPR2-at-812: sharp
// at rest, soft the moment anyone leans in). Set TROVE_SHOT_W=1624 to get the
// smaller bitmaps back if the repo weight ever matters more than the detail.
const RESIZE_W = Number(process.env.TROVE_SHOT_W ?? 0)

// ── Interaction helpers ────────────────────────────────────────────────────
// Sessions seeded for the docs capture: the flagship one is the Chinese
// metric question; the page opens whichever session is newest, so every shot
// that needs a specific conversation clicks it by title.
const HERO = '贷款金额最高'

async function openSession(page, titlePart) {
  // `.last()` is deliberate: the sidebar lists newest-first, so if a live-run
  // shot has (temporarily) left a session with the same-title question behind,
  // the seeded session — the oldest match — is still the one we want.
  await page.locator(`.session-item:has-text("${titlePart}")`).last().click()
}

/** Scroll a turn's question bubble to the top of the chat scroll container
 *  (chat panes scroll an inner div, not the window — locator.scrollIntoViewIfNeeded
 *  would only guarantee "some part visible"). The question is the element right
 *  before the `.assistant-turn` card. */
async function scrollTurnStart(page, turnLocator) {
  await turnLocator.evaluate((el) => {
    const prev = el.previousElementSibling
    ;(prev || el).scrollIntoView({ block: 'start' })
  })
}

// ── Shot registry ──────────────────────────────────────────────────────────
// `before(page)` runs right after navigation (Playwright auto-waits inside),
// then `wait` proves the final state rendered — screenshotting on a timer is
// how you get half-loaded images in docs.
const SHOTS = [
  // ── 用户端（用户指南）──
  {
    name: 'user-login',
    anon: true,
    path: '/login',
    wait: '.login-card',
  },
  {
    name: 'user-chat-empty',
    path: '/',
    before: async (page) => {
      // Seeded sessions exist, so the app may auto-open the newest one; the
      // empty state is what this shot documents.
      await page.click('.new-session-btn:has-text("新建会话")')
    },
    wait: '.empty-center',
  },
  {
    // 主题域选择器:只在当前源声明了域时才出现(financial 有四个域)。
    // 展开下拉是这张图的主题——域 + 描述 + 范围表数是一等清单信息。
    name: 'user-topic-select',
    path: '/',
    before: async (page) => {
      await page.click('.chat-topic-select')
    },
    wait: '.el-select-dropdown__item .topic-opt-scope',
    settle: 600,
  },
  {
    name: 'user-chat-answer',
    path: '/',
    before: async (page) => {
      await openSession(page, HERO)
      await scrollTurnStart(page, page.locator('.assistant-turn').first())
    },
    wait: '.assistant-turn .answer',
  },
  {
    name: 'user-chat-chart',
    path: '/',
    before: async (page) => {
      await openSession(page, HERO)
      await page.locator('.chart-wrap').last().scrollIntoViewIfNeeded()
    },
    wait: '.chart-wrap .chart-card',
  },
  {
    name: 'user-chat-evidence',
    path: '/',
    before: async (page) => {
      await openSession(page, HERO)
      const cta = page.locator('.rate-btn.evidence-cta').first()
      await cta.scrollIntoViewIfNeeded()
      await cta.click()
    },
    wait: '.drawer-panel .ev-title',
  },
  {
    name: 'user-chat-analysis',
    path: '/',
    // The panel documents the *live* run (a restored session shows none of
    // its steps), so this shot asks the flagship question in a fresh session
    // and waits for the run to finish with its timeline still on screen.
    before: async (page) => {
      await page.click('.new-session-btn:has-text("新建会话")')
      await page.fill('.composer-input', '贷款金额最高是多少?')
      await page.click('.send-btn')
      // The panel toggle only renders once the session has turns (v-if on
      // chat.turns.length), and the panel is default-closed — so send first,
      // then open it, or the run's step timeline is never on screen.
      await page.locator('.analysis-toggle').waitFor()
      if ((await page.locator('.analysis-panel.open').count()) === 0) {
        await page.click('.analysis-toggle')
      }
    },
    wait: '.analysis-status.status-done',
    timeout: 180000,
    // The live run just created a session titled after the same flagship
    // question the seeded HERO session carries. Left behind, it sorts to the
    // top of the sidebar (visible in every later user shot) and openSession
    // would have to keep dodging it — delete it so a re-run is idempotent.
    postshot: async (page) => {
      await page.evaluate(async () => {
        const sid = localStorage.getItem('trove_ui_session')
        const token = localStorage.getItem('trove_auth_token')
        if (!sid) return
        await fetch(`/v1/sessions/${sid}`, {
          method: 'DELETE',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
      })
    },
  },
  {
    // 归因问题走「意图 → 归因计划 → 多跳下钻」,分析卡在跑完之后才渲染。
    // 问句刻意对准 sketch 大纲里列出的已声明度量(max_loan_amount)——
    // 归因节点按「精确度量名」解析,口径名对不上会静默跳过分析(无卡片)。
    name: 'user-chat-attribution',
    path: '/',
    before: async (page) => {
      await page.click('.new-session-btn:has-text("新建会话")')
      await page.fill('.composer-input', '为什么不同贷款状态的最大贷款金额差异这么大?')
      await page.click('.send-btn')
    },
    wait: '.assistant-turn .ana-card',
    timeout: 180000,
    // 卡片在答案正文下方,聊天区内层滚动 —— 把卡片顶滚到视野顶部。
    after: async (page) => {
      await page
        .locator('.assistant-turn .ana-card')
        .evaluate((el) => el.scrollIntoView({ block: 'start' }))
    },
    // 瀑布图有入场动画,等它画完再拍(与 user-chat-analysis 同一套路)。
    settle: 1200,
    postshot: async (page) => {
      await page.evaluate(async () => {
        const sid = localStorage.getItem('trove_ui_session')
        const token = localStorage.getItem('trove_auth_token')
        if (!sid) return
        await fetch(`/v1/sessions/${sid}`, {
          method: 'DELETE',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
      })
    },
  },
  {
    name: 'user-chat-feedback',
    path: '/',
    before: async (page) => {
      await openSession(page, HERO)
      const row = page.locator('.rating-row').last()
      const down = row.locator('.rate-btn').last()
      await down.scrollIntoViewIfNeeded()
      // Stop at the reason chips: a downvote never submits on its own
      // (ChatView.rate returns early for vote === -1), and that mandatory
      // "pick a reason" step is exactly what this shot documents. The chip
      // block renders at the bottom of the turn and the chat pane scrolls an
      // inner div, so bring it fully into view.
      await down.click()
      await page.locator('.rating-reasons').scrollIntoViewIfNeeded()
    },
    wait: '.rating-reasons',
  },
  {
    name: 'user-sessions-search',
    path: '/',
    before: async (page) => {
      await page.click('.new-session-btn:has-text("查询")')
      // Search matches session *titles* only (Sidebar.vue filteredSessions) —
      // "How many" is the phrasing three seeded sessions share.
      await page.fill('.search-dialog-input', 'How many')
    },
    wait: '.search-dialog',
    settle: 1000,
  },
  {
    name: 'user-session-menu',
    path: '/',
    before: async (page) => {
      await page.click('.session-item >> nth=0', { button: 'right' })
    },
    wait: '.session-menu',
  },

  // ── 管理台（管理指南）──
  {
    name: 'admin-overview',
    role: 'admin',
    path: '/admin',
    wait: '.overview-page .kpi-row',
  },
  {
    name: 'admin-todos',
    role: 'admin',
    path: '/admin',
    before: async (page) => {
      await page.locator('#todos').scrollIntoViewIfNeeded()
    },
    wait: '#todos .todo-queue',
  },
  {
    name: 'admin-datasources',
    role: 'admin',
    path: '/admin/datasources',
    wait: '.admin-table .el-table__row',
  },
  {
    name: 'admin-ds-create',
    role: 'admin',
    path: '/admin/datasources',
    before: async (page) => {
      await page.click('.el-button.add')
      // Type defaults to Demo, which hides the URL field entirely — pick
      // MySQL first, then fill. Nothing is submitted: the dialog (with its
      // probe hint) is the subject, and registering would mutate state other
      // shots already captured.
      await page.click('.ds-dialog .ds-type-select')
      await page
        .locator('.el-select-dropdown__item:has-text("MySQL")')
        .last()
        .click()
      await page.fill('.ds-dialog .ds-url-input input', 'mysql://user:pass@localhost:3306/sales')
      await page.fill('.ds-dialog .ds-name-field input', 'sales')
    },
    wait: '.ds-dialog',
  },
  {
    name: 'admin-kb-pending',
    role: 'admin',
    path: '/admin/kb?tab=pending&ds=financial',
    wait: '.queue-list .diff-card',
  },
  {
    name: 'admin-kb-assets',
    role: 'admin',
    path: '/admin/kb?tab=assets&ds=financial',
    wait: '.kb-assets .dt-row',
  },
  {
    name: 'admin-semantic',
    role: 'admin',
    path: '/admin/semantic',
    wait: '.sem-page .dt-row',
  },
  {
    name: 'admin-semantic-detail',
    role: 'admin',
    path: '/admin/semantic',
    before: async (page) => {
      await page.locator('.sem-page .dt-row').first().click()
    },
    wait: '.drawer-panel .sem-drawer',
  },
  {
    name: 'admin-semantic-draft',
    role: 'admin',
    path: '/admin/semantic',
    before: async (page) => {
      await page.locator('.sem-page .dt-row').first().click()
      await page.locator('.sem-drawer .sem-block h4 .link-btn').first().click()
      // Full flow up to the last step: 校验 enables 创建草稿 (disabled until
      // editorCheck.ok), 试跑 fills the preview — so the shot shows the state
      // right before a draft is created, not a half-configured editor.
      await page.locator('.sem-editor-actions .el-button:has-text("校验")').click()
      await page.locator('.sem-editor-actions .el-button:has-text("试跑")').click()
    },
    wait: '.sem-preview',
  },
  {
    name: 'admin-jobs',
    role: 'admin',
    path: '/admin/jobs',
    wait: '.admin-table .el-table__row',
  },
  {
    name: 'admin-jobs-history',
    role: 'admin',
    path: '/admin/jobs',
    before: async (page) => {
      await page
        .locator('.el-table__row .el-button:has-text("运行历史")')
        .first()
        .click()
    },
    wait: '.job-dialog .el-table__row',
  },
  {
    // 订阅抽屉(决策任务):订阅者 × 模式(总是/仅提醒)× 频道 + 投递记录。
    // 两张表两次请求 —— 先等订阅行,再由 wait 证明投递记录也已落地。
    name: 'admin-subs',
    role: 'admin',
    path: '/admin/jobs',
    before: async (page) => {
      await page
        .locator('.el-table__row:has-text("贷款总额基线巡检") .el-button:has-text("订阅")')
        .first()
        .click()
      await page
        .locator('.drawer-panel .subs-add ~ .el-table .el-table__row')
        .first()
        .waitFor()
    },
    wait: '.drawer-panel .subs-sec + .el-table .el-table__row',
    settle: 600,
  },
  {
    name: 'admin-decisions',
    role: 'admin',
    path: '/admin/decisions',
    before: async (page) => {
      // The demo datasource is the one with a rule file; switching reloads.
      await page.click('.el-select.ds-select')
      await page.locator('.el-select-dropdown__item:has-text("demo")').last().click()
      // header actions moved into PageHeader's .ph-actions slot (the old
      // .view-actions wrapper is gone); primary button there = 编辑规则文件
      await page.locator('.ph-actions .el-button--primary').click()
    },
    wait: '.el-textarea.decisions-yaml',
    settle: 800,
  },
  {
    // 行动审批台:提案列表(默认筛选「待处理」= pending ∪ approved ∪ failed)。
    // 播种的待批提案来自 demo/loan-high 的触发性判定。
    name: 'admin-actions',
    role: 'admin',
    path: '/admin/actions?tab=proposals',
    wait: '.admin-table .el-table__row',
    settle: 600,
  },
  {
    // 判定历史抽屉:行内唯一动作(固定最右列)。展开最新一条 → 证据段
    // (SQL + 判定行 + 判定卡)随之渲染;diff chip 显示与上一条之间的变化。
    name: 'admin-verdict-history',
    role: 'admin',
    path: '/admin/decisions',
    before: async (page) => {
      await page.click('.el-select.ds-select')
      await page.locator('.el-select-dropdown__item:has-text("demo")').last().click()
      await page
        .locator('.el-table__row:has-text("loan-high") .el-button:has-text("历史")')
        .first()
        .click()
      await page.locator('.drawer-panel .vh-row').first().click()
    },
    wait: '.drawer-panel .vh-detail .vh-sql',
    settle: 600,
  },
  {
    // Plain list first: the preview drawer (next shot) covers the 层级/状态
    // columns, and those two chips are the point of the skills page.
    name: 'admin-skills-table',
    role: 'admin',
    path: '/admin/skills',
    // The org skills sit below the three built-ins; center the last row so
    // the confirmed *and* pending gate states are both on screen.
    before: async (page) => {
      await page
        .locator('.admin-table .el-table__row')
        .last()
        .evaluate((el) => el.scrollIntoView({ block: 'center' }))
    },
    wait: '.admin-table .el-table__row',
  },
  {
    name: 'admin-skills',
    role: 'admin',
    path: '/admin/skills',
    before: async (page) => {
      await page
        .locator('.el-table__row:has-text("answer-structure-zh") .el-button:has-text("预览")')
        .first()
        .click()
    },
    wait: '.el-drawer .skill-preview',
  },
  {
    name: 'admin-users',
    role: 'admin',
    path: '/admin/users',
    wait: '.users-page .dt-row',
  },
  {
    name: 'admin-user-drawer',
    role: 'admin',
    path: '/admin/users',
    before: async (page) => {
      await page.locator('.dt-row:has-text("陈晓")').first().click()
    },
    wait: '.drawer-panel .dsec',
  },
  {
    name: 'admin-audit',
    role: 'admin',
    path: '/admin/audit',
    wait: '.admin-table .el-table__row',
  },
  {
    name: 'admin-settings',
    role: 'admin',
    path: '/admin/settings',
    wait: '.settings-stack .settings-card',
  },
  {
    name: 'admin-model',
    role: 'admin',
    path: '/admin/model-config',
    wait: '.providers-block',
  },
  {
    name: 'admin-ops',
    role: 'admin',
    path: '/admin/ops',
    wait: '.quality-panel .ops-card',
    settle: 800,
  },
]

// ── Plumbing ───────────────────────────────────────────────────────────────
async function login(username, password) {
  const r = await fetch(new URL('/v1/auth/login', BASE), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!r.ok) {
    throw new Error(`login ${username} failed: HTTP ${r.status} ${await r.text()}`)
  }
  return (await r.json()).token
}

function frontendCommit() {
  try {
    return execSync('git rev-parse --short HEAD', { cwd: HERE }).toString().trim()
  } catch {
    return 'unknown'
  }
}

/**
 * Downsample a PNG in the browser (canvas) — reuses the Chrome we already
 * drive, so the script needs no image-processing dependency. No-op when the
 * shot is already at or below the target width. Returns the final pixel size.
 */
async function resizeShot(page, file, targetWidth) {
  const b64 = readFileSync(file).toString('base64')
  const out = await page.evaluate(
    async ([data, target]) => {
      const img = new Image()
      img.src = 'data:image/png;base64,' + data
      await img.decode()
      if (img.naturalWidth <= target) return null
      const c = document.createElement('canvas')
      c.width = target
      c.height = Math.round((img.naturalHeight * target) / img.naturalWidth)
      const ctx = c.getContext('2d')
      ctx.imageSmoothingEnabled = true
      ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(img, 0, 0, c.width, c.height)
      return { url: c.toDataURL('image/png'), width: c.width, height: c.height }
    },
    [b64, targetWidth],
  )
  if (!out) return null
  writeFileSync(file, Buffer.from(out.url.split(',')[1], 'base64'))
  return { width: out.width, height: out.height }
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const tokens = { user: await login(USER, PASS) }
  tokens.admin =
    ADMIN_USER === USER && ADMIN_PASS === PASS
      ? tokens.user
      : await login(ADMIN_USER, ADMIN_PASS)

  const browser = await chromium.launch({ channel: 'chrome' })
  const manifest = {
    generatedAt: new Date().toISOString(),
    frontendCommit: frontendCommit(),
    baseUrl: BASE,
    viewport: VIEWPORT,
    deviceScaleFactor: SCALE,
    resizeWidth: RESIZE_W || null,
    shots: [],
  }

  const wanted = ONLY.length ? SHOTS.filter((s) => ONLY.includes(s.name)) : SHOTS
  if (!wanted.length) {
    console.error(`no shots match TROVE_SHOT_ONLY=${ONLY.join(',')}`)
    process.exit(2)
  }

  for (const shot of wanted) {
    const ctx = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: SCALE,
      locale: 'zh-CN',
      colorScheme: 'light',
    })
    if (!shot.anon) {
      // Seed the auth token before any app code runs — the router guard reads
      // it on boot, so navigating first would bounce to /login.
      await ctx.addInitScript(
        ([key, value]) => localStorage.setItem(key, value),
        ['trove_auth_token', tokens[shot.role === 'admin' ? 'admin' : 'user']],
      )
    }
    const page = await ctx.newPage()
    await page.goto(new URL(shot.path, BASE).href, { waitUntil: 'domcontentloaded' })
    if (shot.before) await shot.before(page)
    // Live-run shots (asking a real question) need a wider window than the
    // render-the-view default.
    await page.waitForSelector(shot.wait, {
      state: 'visible',
      timeout: shot.timeout ?? 20000,
    })
    if (shot.after) await shot.after(page)
    // Entrance animations (e.g. the login brand mark's `brand-in` fade) are
    // still running the moment the wait selector appears — shooting now
    // catches them half-transparent.
    await page.waitForTimeout(shot.settle ?? 450)

    const file = path.join(OUT, `${shot.name}.png`)
    await page.screenshot({ path: file, fullPage: !!shot.fullPage })
    // postshot runs after the pixels exist: cleanup that must not be visible
    // in the image (e.g. discarding the session a live-run shot created), so
    // re-runs start from the same state the seed left behind.
    if (shot.postshot) await shot.postshot(page)
    const beforeBytes = statSync(file).size
    const captured = { width: VIEWPORT.width * SCALE, height: VIEWPORT.height * SCALE }
    let size = captured
    if (RESIZE_W) {
      size = (await resizeShot(page, file, RESIZE_W)) || captured
    }
    const bytes = statSync(file).size
    manifest.shots.push({
      name: shot.name,
      file: `${shot.name}.png`,
      captured: { width: captured.width, height: captured.height },
      width: size.width,
      height: size.height,
      bytes,
    })
    console.log(
      `✓ ${shot.name}  ${size.width}×${size.height}  ` +
        `${(beforeBytes / 1024).toFixed(0)}→${(bytes / 1024).toFixed(0)} KB`,
    )
    await ctx.close()
  }

  await browser.close()
  writeFileSync(
    path.join(OUT, '_manifest.json'),
    JSON.stringify(manifest, null, 2) + '\n',
  )
  const total = manifest.shots.reduce((n, s) => n + s.bytes, 0)
  console.log(`\n${manifest.shots.length} shots → ${OUT}  (${(total / 1048576).toFixed(1)} MB)`)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
