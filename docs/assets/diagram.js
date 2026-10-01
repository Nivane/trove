/* 架构图渲染 —— 只有含图的页面才引入这个模块（type="module"，动态 import
 * mermaid）。没写这个 <script> 的页面保持零额外 JS；写了的一页可以有多张
 * 图，逐张渲染、逐张降级。
 *
 * 五个刻意的选择：
 *   1. 先 parse 再 run。parse 不过时 DOM 原封不动，<pre> 里的源码还留着，
 *      读者至少能读到图的内容；直接 run 会把一句「Syntax error」画进页面，
 *      图没了，源码也没了。
 *   2. 主题色从站点自己的 CSS token 读，不写死 mermaid 默认主题——否则这
 *      会是全站唯一一块不属于这套配色的区域。（token 取不到时给兜底值，
 *      变量改名不至于把图变成一片透明。）
 *   3. 深浅色切换要重渲染：mermaid 把颜色烘进 SVG 里，不重跑就留在旧主题。
 *      源码在第一次渲染前存进 Map——run 之后 <pre> 已经被换成 <svg>，重渲染
 *      只能从 Map 还原，不能再去 DOM 里找。
 *   4. 任何一步失败都只是该图 data-diagram 没变成 rendered，CSS 据此把说明
 *      切成「以上为图源码」——页面不会出现一块白板，也不假装图还在。
 *   5. 一页多图互不牵连：逐张 try/catch，一张图的语法错不会把同页其他图
 *      也拖成源码形态。
 */
const sources = new Map();
for (const fig of document.querySelectorAll("figure.diagram")) {
  const pre = fig.querySelector(".diagram-canvas pre.mermaid");
  if (pre) sources.set(fig, pre.textContent);
}

if (sources.size) {
  const token = (name, fallback) => {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return v || fallback;
  };

  async function drawAll() {
    const { default: mermaid } = await import(
      "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
    );
    mermaid.initialize({
      startOnLoad: false,
      theme: "base",
      fontFamily: getComputedStyle(document.body).fontFamily,
      themeVariables: {
        background: token("--raised", "#ffffff"),
        primaryColor: token("--muted", "#f4f4f5"),
        primaryTextColor: token("--text-primary", "#18181b"),
        primaryBorderColor: token("--border-default", "#d4d4d8"),
        lineColor: token("--text-tertiary", "#8b8b94"),
        clusterBkg: token("--canvas", "#fafafa"),
        clusterBorder: token("--border-subtle", "#e4e4e7"),
        titleColor: token("--text-secondary", "#52525b"),
        edgeLabelBackground: token("--raised", "#ffffff"),
        fontSize: "16px",
      },
      /* 间距收得比 mermaid 默认（50/50）紧：图的天然宽度决定它会被缩多小，
         而这里的图多是「带」的形状，不需要默认那么松。 */
      flowchart: {
        htmlLabels: true,
        curve: "basis",
        /* 允许按容器宽度缩，但 CSS 给 SVG 设了 min-width 下限（见 docs.css，
          compact 图可用 data-size="compact" 放行原尺寸）：窄屏宁可横向滚动，
         也不把字缩到读不了。宽视窗下卡片够宽，图按天然尺寸显示，等于没缩。 */
        useMaxWidth: true,
        nodeSpacing: 30,
        rankSpacing: 40,
      },
    });

    for (const [fig, source] of sources) {
      const canvas = fig.querySelector(".diagram-canvas");
      try {
        await mermaid.parse(source);
      } catch {
        continue; // 语法不过：保持源码形态
      }
      try {
        const pre = document.createElement("pre");
        pre.className = "mermaid";
        pre.textContent = source;
        canvas.replaceChildren(pre);
        await mermaid.run({ nodes: [pre] });
        fig.dataset.diagram = "rendered";
      } catch {
        // run 失败：把源码放回去（此时 <pre> 可能已被 mermaid 换掉一半）
        if (!canvas.querySelector("pre.mermaid")) {
          const pre = document.createElement("pre");
          pre.className = "mermaid";
          pre.textContent = source;
          canvas.replaceChildren(pre);
        }
      }
    }
  }

  const redraw = () => drawAll().catch(() => {});
  redraw();
  window
    .matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", redraw);
}
