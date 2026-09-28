/* Trove 文档站导航 —— 零构建：每页只放一个占位元素，导航由这里注入。
 *
 * 为什么用注入而不是把 sidebar 复制到每页：23 页各抄一份 HTML，改一次导航
 * 要改 23 处，漏掉的那页就开始骗人。这里只有一份真源。
 *
 * 路径全部相对：站点发布在 https://nivane.github.io/trove/ 这样的子路径下，
 * 写死 /guide/... 会在本地和子路径部署两种情况下都指错。脚本从自己的 src
 * 反推站点根，再据此解析所有链接。
 *
 * 用法（页面里放这两行即可，src 按页面深度调整）：
 *   <script src="../assets/nav.js" defer></script>
 *   <aside class="doc-nav" data-nav></aside>      ← 站点导航
 *   <div class="doc-pager" data-pager></div>       ← 上下页（可选）
 *   <nav class="doc-toc" data-toc></nav>           ← 本页目录（可选）
 */
(function () {
  "use strict";

  // ── 站点结构：唯一真源，改这里就是改全站导航 ──────────────
  var NAV = [
    {
      group: "入门",
      items: [
        { href: "guide/quickstart.html", text: "快速上手" },
        { href: "guide/concepts.html", text: "产品概念" },
        { href: "guide/deploy.html", text: "安装部署" },
        { href: "guide/datasource.html", text: "接入数据源" }
      ]
    },
    {
      group: "架构",
      items: [
        { href: "architecture/overview.html", text: "系统架构" },
        { href: "architecture/workflow.html", text: "查询工作流" }
      ]
    },
    {
      group: "数据能力",
      items: [
        { href: "capabilities/data.html", text: "数据能力总览" },
        { href: "capabilities/semantic.html", text: "语义层" },
        { href: "capabilities/kb.html", text: "知识库" },
        { href: "capabilities/retrieval.html", text: "混合检索" }
      ]
    },
    {
      group: "Agent 能力",
      items: [
        { href: "capabilities/agent.html", text: "Agent 能力总览" },
        { href: "capabilities/memory.html", text: "记忆子系统" },
        { href: "capabilities/skills.html", text: "Skills" },
        { href: "capabilities/llm-gateway.html", text: "LLM 网关" }
      ]
    },
    {
      group: "运维",
      items: [
        { href: "ops/admin.html", text: "运维管理" },
        { href: "ops/observability.html", text: "可观测性" },
        { href: "ops/security.html", text: "安全边界" },
        { href: "ops/drift.html", text: "数据漂移治理" },
        { href: "ops/eval.html", text: "评测与回归门" }
      ]
    },
    {
      group: "参考",
      items: [
        { href: "reference/config.html", text: "配置参考" },
        { href: "reference/cli.html", text: "CLI 参考" },
        { href: "reference/api.html", text: "API 参考" },
        { href: "reference/mcp.html", text: "MCP 集成" }
      ]
    }
  ];
  var HOME = { href: "index.html", text: "能力地图（首页）" };

  // ── 站点根：从本脚本的 src 反推 ──────────────────────────
  var self =
    document.currentScript ||
    (function () {
      var s = document.getElementsByTagName("script");
      for (var i = s.length - 1; i >= 0; i--) {
        if (/assets\/nav\.js(\?|$)/.test(s[i].src)) return s[i];
      }
      return null;
    })();

  var ROOT = self ? self.src.replace(/assets\/nav\.js(\?.*)?$/, "") : "/";

  function url(href) {
    return ROOT + href;
  }

  // 归一化 pathname 以便比较（目录 → index.html；忽略末尾斜杠）
  function norm(p) {
    p = p.replace(/index\.html$/, "");
    return p.replace(/\/+$/, "");
  }

  var here = norm(location.pathname);
  function isCurrent(href) {
    try {
      return norm(new URL(href, ROOT).pathname) === here;
    } catch (e) {
      return false;
    }
  }

  // ── 站点导航 ─────────────────────────────────────────────
  var host = document.querySelector("[data-nav]");
  if (host) {
    // 23 条链接在小屏上会把正文推到屏幕外，所以窄屏折进 <details>。
    // 桌面端由 CSS 隐藏 summary、并由下面 openNav() 强制展开——不能只靠 CSS，
    // 因为闭合的 <details> 内容在多数浏览器里无法用样式重新显示。
    var html = [
      '<a class="doc-brand" href="' +
        url(HOME.href) +
        '">Trove<small>文档</small></a>',
      '<details class="nav-fold"><summary>文档目录</summary>',
      "<nav>"
    ];
    // 首页单独置顶，然后按分组铺开
    if (isCurrent(HOME.href)) {
      html.push(
        '<a class="is-current" href="' + url(HOME.href) + '">' + HOME.text + "</a>"
      );
    } else {
      html.push('<a href="' + url(HOME.href) + '">' + HOME.text + "</a>");
    }
    NAV.forEach(function (g) {
      html.push('<span class="nav-group">' + g.group + "</span>");
      g.items.forEach(function (it) {
        var cur = isCurrent(it.href);
        html.push(
          '<a' +
            (cur ? ' class="is-current" aria-current="page"' : "") +
            ' href="' +
            url(it.href) +
            '">' +
            it.text +
            "</a>"
        );
      });
    });
    html.push("</nav></details>");
    host.innerHTML = html.join("");

    var fold = host.querySelector(".nav-fold");
    var wide = window.matchMedia("(min-width: 1000px)");
    function syncFold() {
      if (wide.matches) fold.open = true;
    }
    syncFold();
    // 从窄屏转到宽屏时补开；宽屏转窄屏交给用户自己折叠，不强关
    if (wide.addEventListener) wide.addEventListener("change", syncFold);
    else if (wide.addListener) wide.addListener(syncFold);
  }

  // ── 上下页 ───────────────────────────────────────────────
  var pager = document.querySelector("[data-pager]");
  if (pager) {
    var flat = NAV.reduce(function (acc, g) {
      return acc.concat(g.items);
    }, []);
    var idx = -1;
    for (var i = 0; i < flat.length; i++) {
      if (isCurrent(flat[i].href)) {
        idx = i;
        break;
      }
    }
    if (idx >= 0) {
      var parts = [];
      if (idx > 0) {
        parts.push(
          '<a href="' +
            url(flat[idx - 1].href) +
            '"><span class="dir">上一页</span><span class="to">' +
            flat[idx - 1].text +
            "</span></a>"
        );
      }
      if (idx < flat.length - 1) {
        parts.push(
          '<a class="is-next" href="' +
            url(flat[idx + 1].href) +
            '"><span class="dir">下一页</span><span class="to">' +
            flat[idx + 1].text +
            "</span></a>"
        );
      }
      pager.innerHTML = parts.join("");
    } else {
      pager.remove();
    }
  }

  // ── 本页目录 ─────────────────────────────────────────────
  // 从正文里的 h2/h3 生成；没有 id 的补一个。少于 4 条不显示——
  // 短页面加目录只是噪音。
  var toc = document.querySelector("[data-toc]");
  var col = document.querySelector(".doc-col");
  if (toc && col) {
    var used = Object.create(null);
    function slug(text, n) {
      var s = text
        .trim()
        .toLowerCase()
        .replace(/[\s　]+/g, "-")
        .replace(/[^\w一-龥-]/g, "")
        .replace(/-+/g, "-")
        .replace(/^-|-$/g, "");
      if (!s) s = "section-" + n;
      var base = s;
      var k = 2;
      while (used[s]) s = base + "-" + k++;
      used[s] = 1;
      return s;
    }
    var heads = col.querySelectorAll("h2, h3");
    var links = [];
    Array.prototype.forEach.call(heads, function (h, n) {
      // 跳过正文之外的（比如被 .doc-head 包住的 h1 不在此列）
      if (h.closest(".doc-pager, .doc-foot")) return;
      if (!h.id) h.id = slug(h.textContent, n + 1);
      var depth = h.tagName === "H3" ? " toc-3" : "";
      links.push(
        '<a class="' +
          depth.trim() +
          '" href="#' +
          encodeURIComponent(h.id) +
          '">' +
          h.textContent +
          "</a>"
      );
    });
    if (links.length >= 4) {
      toc.innerHTML =
        '<span class="toc-label">本页目录</span>' + links.join("");
    } else {
      toc.remove();
    }
  }
})();
