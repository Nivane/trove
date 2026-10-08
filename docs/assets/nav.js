/* Trove 文档站导航 —— 零构建：每页只放一个占位元素，导航由这里注入。
 *
 * 为什么用注入而不是把 sidebar 复制到每页：三十余页各抄一份 HTML，改一次导航
 * 要改三十余处，漏掉的那页就开始骗人。这里只有一份真源。
 *
 * 路径全部相对：站点发布在 https://nivane.github.io/trove/ 这样的子路径下，
 * 写死 /guide/... 会在本地和子路径部署两种情况下都指错。脚本从自己的 src
 * 反推站点根，再据此解析所有链接。
 *
 * 用法（页面里放这两行即可，src 按页面深度调整）：
 *   <script src="../assets/nav.js" defer></script>
 *   <aside class="doc-nav" data-nav></aside>      ← 站点导航
 *   <div class="doc-pager" data-pager></div>       ← 上下页（可选）
 *   <nav class="doc-toc" data-toc></nav>           ← 本页目录（可选；宽屏自动移入右栏）
 */
(function () {
  "use strict";

  // ── 站点结构：唯一真源，改这里就是改全站导航 ──────────────
  // 分组顺序与首页「从这里开始」的三柱卡片一致：架构 → 数据 / Agent /
  // 决策与行动 → 设计深潜（深潜沉底）。改顺序时 index.html 的 .cards
  // 区要同看，否则两个入口给出的阅读推进方向相反。
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
      group: "用户指南",
      items: [
        { href: "user/ui-tour.html", text: "界面导览" },
        { href: "user/ask.html", text: "提问与读懂答案" },
        { href: "user/sessions.html", text: "会话管理" },
        { href: "user/feedback.html", text: "反馈与知识沉淀" }
      ]
    },
    {
      group: "管理指南",
      items: [
        { href: "admin/console.html", text: "管理台总览" },
        { href: "admin/datasources.html", text: "数据源接入" },
        { href: "admin/kb.html", text: "知识库运营" },
        { href: "admin/semantic.html", text: "语义层治理" },
        { href: "admin/automation.html", text: "自动化与治理" },
        { href: "admin/access.html", text: "用户·权限·安全" }
      ]
    },
    {
      group: "架构",
      items: [
        { href: "architecture/overview.html", text: "系统架构" },
        { href: "architecture/functional.html", text: "功能架构" },
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
      group: "决策与行动",
      items: [
        { href: "capabilities/decisions.html", text: "判定规则" },
        { href: "capabilities/actions.html", text: "行动与审批" }
      ]
    },
    {
      group: "设计深潜",
      items: [
        { href: "engineering/semantic-compiler.html", text: "语义编译边界" },
        { href: "engineering/decomposition.html", text: "分解数学与驱动器树" },
        { href: "engineering/statistics.html", text: "统计与噪声带" },
        { href: "engineering/decision-engine.html", text: "判定内核" },
        { href: "engineering/causal-whatif.html", text: "因果与模拟" },
        { href: "engineering/closed-loop.html", text: "闭环与契约" },
        { href: "engineering/action-safety.html", text: "行动安全架构" },
        { href: "engineering/extension-governance.html", text: "扩展与治理" }
      ]
    },
    {
      group: "运维",
      items: [
        { href: "ops/admin.html", text: "运维管理" },
        { href: "ops/deployment.html", text: "部署与运维实操" },
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
  var HOME = { href: "index.html", text: "首页" };

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
    // 三十余条链接在小屏上会把正文推到屏幕外，所以窄屏折进 <details>。
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

    // 左栏是独立滚动容器：切页后新页面的左栏从顶部开始，当前项（可能在
    // 列表很下方，比如「LLM 网关」）落在可视区外，读者得先自己往下滚才
    // 找得到自己在哪。载入时把它滚到视野中央（宽屏才有滚动几何；窄屏
    // 折叠态 rect 判定自然跳过）。用 rect 而不是 offsetTop：窄屏下
    // .doc-nav 不是定位祖先，offsetTop 会相对 body。
    var cur = host.querySelector("a.is-current");
    if (cur) {
      var hRect = host.getBoundingClientRect();
      var cRect = cur.getBoundingClientRect();
      if (cRect.top < hRect.top + 8 || cRect.bottom > hRect.bottom - 8) {
        host.scrollTop +=
          cRect.top - hRect.top - host.clientHeight / 2 + cRect.height / 2;
      }
    }
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
  // 两种形态是同一个节点，由 place() 在 1280px 断点（与 docs.css 的三栏
  // 断点一致）搬运：宽屏挪进 .doc-shell 第三栏（sticky 常驻 + 滚动高亮
  // 当前小节）；窄屏挪回正文开头的两列卡片。原位置留一个注释锚点，
  // 断点来回穿越时据此还原。
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
    var targets = [];
    Array.prototype.forEach.call(heads, function (h, n) {
      // 跳过正文之外的（比如被 .doc-head 包住的 h1 不在此列）
      if (h.closest(".doc-pager, .doc-foot")) return;
      if (!h.id) h.id = slug(h.textContent, n + 1);
      targets.push(h);
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
      toc.setAttribute("aria-label", "本页目录");

      var linkEls = toc.querySelectorAll("a");
      var shell = col.closest(".doc-shell");
      var railQ = window.matchMedia("(min-width: 1280px)");
      var home = document.createComment("toc-home");
      toc.parentNode.insertBefore(home, toc);

      function place() {
        if (shell && railQ.matches) shell.appendChild(toc);
        else home.parentNode.insertBefore(toc, home);
      }

      // 滚动高亮：越过视口上方 80px 线的最后一个标题 = 当前小节
      var active = -1;
      function activate(i) {
        if (i === active) return;
        if (active >= 0) linkEls[active].classList.remove("is-active");
        active = i;
        if (i < 0) return;
        var link = linkEls[i];
        link.classList.add("is-active");
        // 目录比可视高度长时，把当前项滚进视野（只滚目录，不动页面）
        if (toc.scrollHeight > toc.clientHeight + 4) {
          if (
            link.offsetTop < toc.scrollTop + 8 ||
            link.offsetTop + link.offsetHeight >
              toc.scrollTop + toc.clientHeight - 8
          ) {
            toc.scrollTop = link.offsetTop - toc.clientHeight / 2;
          }
        }
      }
      var ticking = false;
      function spy() {
        ticking = false;
        if (!railQ.matches) return;
        var i = -1;
        for (var k = 0; k < targets.length; k++) {
          if (targets[k].getBoundingClientRect().top <= 80) i = k;
          else break;
        }
        activate(i);
      }
      function onRailChange() {
        place();
        activate(-1);
        spy();
      }
      window.addEventListener(
        "scroll",
        function () {
          if (ticking) return;
          ticking = true;
          window.requestAnimationFrame(spy);
        },
        { passive: true }
      );
      if (railQ.addEventListener) railQ.addEventListener("change", onRailChange);
      else if (railQ.addListener) railQ.addListener(onRailChange);

      place();
      spy();
    } else {
      toc.remove();
    }
  }
})();
