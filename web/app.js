(function () {
  "use strict";

  const React = window.React;
  const ReactDOM = window.ReactDOM;
  const antd = window.antd;
  const icons = window.icons;
  const html = window.htm.bind(React.createElement);

  const Y = (window.YAFIP = window.YAFIP || {});
  Y.pages = Y.pages || {};
  Y.html = html;
  Y.antd = antd;
  Y.icons = icons;
  Y.version = "0.1.0";

  // ---------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------
  Y.api = async function (path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.body && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["Content-Type"] = "application/json";
    }
    const response = await fetch("/api" + path, opts);
    let payload = null;
    try {
      payload = await response.json();
    } catch (err) {
      throw new Error("响应不是合法 JSON（HTTP " + response.status + "）");
    }
    if (!response.ok || payload.code !== 0) {
      throw new Error(payload.message || "HTTP " + response.status);
    }
    return payload.data;
  };

  Y.qs = function (params) {
    const search = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value === undefined || value === null || value === "") return;
      if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
      else search.append(key, value);
    });
    const text = search.toString();
    return text ? "?" + text : "";
  };

  Y.fmt = {
    num(value) {
      if (value === null || value === undefined || value === "") return "-";
      return Number(value).toLocaleString("zh-CN");
    },
    bytes(value) {
      if (!value) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let size = Number(value);
      let i = 0;
      while (size >= 1024 && i < units.length - 1) {
        size /= 1024;
        i += 1;
      }
      return size.toFixed(i === 0 ? 0 : 2) + " " + units[i];
    },
    time(value) {
      if (!value) return "-";
      return String(value).replace("T", " ");
    },
    shortId(id) {
      return id ? String(id).slice(0, 8) : "-";
    },
    pct(value) {
      if (value === null || value === undefined) return "-";
      return Number(value).toFixed(1) + "%";
    },
    range(range) {
      if (!range || !range[0] || !range[1]) return {};
      return {
        start: range[0].format("YYYY-MM-DD"),
        end: range[1].format("YYYY-MM-DD"),
      };
    },
  };

  Y.notifyError = function (error) {
    antd.message.error(error && error.message ? error.message : String(error));
  };

  Y.confirm = function (title, content, onOk, onCancel) {
    antd.Modal.confirm({
      title: title,
      content: content,
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: onOk,
      onCancel: onCancel,
    });
  };

  // ---------------------------------------------------------------------
  // 简易数据 Hook
  // ---------------------------------------------------------------------
  Y.useApi = function (loader, deps, interval) {
    const [state, setState] = React.useState({ data: null, loading: true, error: null });
    const load = React.useCallback(
      async function () {
        try {
          const data = typeof loader === "function" ? await loader() : await Y.api(loader);
          setState({ data: data, loading: false, error: null });
        } catch (error) {
          setState({ data: null, loading: false, error: error.message });
        }
      },
      deps || []
    );
    React.useEffect(
      function () {
        load();
        if (interval) {
          const timer = setInterval(load, interval);
          return () => clearInterval(timer);
        }
      },
      [load, interval]
    );
    return { data: state.data, loading: state.loading, error: state.error, reload: load };
  };

  // ---------------------------------------------------------------------
  // 路由
  // ---------------------------------------------------------------------
  const ROUTES = {
    overview: { page: "overview", title: "系统概览", menu: "overview" },
    "data/source": { page: "data_source", title: "数据源配置", menu: "data/source" },
    "data/fetch": { page: "data_fetch", title: "任务提交", menu: "data/fetch" },
    "data/explore": { page: "data_explore", title: "数据浏览", menu: "data/explore" },
    "data/quality": { page: "data_quality", title: "覆盖与质量", menu: "data/quality" },
    "data/whitelist": { page: "data_whitelist", title: "白名单管理", menu: "data/whitelist" },
    tasks: { page: "tasks", title: "任务列表", menu: "tasks" },
    "system/settings": { page: "system_settings", title: "系统设置", menu: "system/settings" },
    "system/audit": { page: "system_audit", title: "操作日志", menu: "system/audit" },
    "system/about": { page: "system_about", title: "关于与帮助", menu: "system/about" },
  };

  function currentPath() {
    return (window.location.hash || "#/overview").replace(/^#\/?/, "") || "overview";
  }

  function resolve(path) {
    if (path.indexOf("tasks/") === 0) {
      return {
        page: "task_detail",
        title: "任务详情",
        menu: "tasks",
        params: { id: path.split("/")[1] },
      };
    }
    const route = ROUTES[path] || ROUTES.overview;
    return Object.assign({}, route, { params: {} });
  }

  const iconOf = function (Component) {
    return React.createElement(Component);
  };

  const MENU_ITEMS = [
    {
      type: "group",
      label: "总览",
      children: [{ key: "overview", icon: iconOf(icons.DashboardOutlined), label: "系统概览" }],
    },
    {
      type: "group",
      label: "数据",
      children: [
        { key: "data/source", icon: iconOf(icons.ApiOutlined), label: "数据源配置" },
        { key: "data/fetch", icon: iconOf(icons.CloudDownloadOutlined), label: "任务提交" },
        { key: "data/explore", icon: iconOf(icons.TableOutlined), label: "数据浏览" },
        { key: "data/quality", icon: iconOf(icons.LineChartOutlined), label: "覆盖与质量" },
        { key: "data/whitelist", icon: iconOf(icons.SafetyCertificateOutlined), label: "白名单管理" },
      ],
    },
    {
      type: "group",
      label: "任务中心",
      children: [{ key: "tasks", icon: iconOf(icons.UnorderedListOutlined), label: "任务列表" }],
    },
    {
      type: "group",
      label: "系统",
      children: [
        { key: "system/settings", icon: iconOf(icons.SettingOutlined), label: "系统设置" },
        { key: "system/audit", icon: iconOf(icons.FileSearchOutlined), label: "操作日志" },
        { key: "system/about", icon: iconOf(icons.InfoCircleOutlined), label: "关于与帮助" },
      ],
    },
    {
      type: "group",
      label: "后续规划",
      children: [
        { key: "strategy", icon: iconOf(icons.ExperimentOutlined), label: "策略 / 回测 / 建议", disabled: true },
      ],
    },
  ];

  // ---------------------------------------------------------------------
  // 布局
  // ---------------------------------------------------------------------
  const Layout = antd.Layout;
  const Header = Layout.Header;
  const Sider = Layout.Sider;
  const Content = Layout.Content;
  const Footer = Layout.Footer;

  function App() {
    const [path, setPath] = React.useState(currentPath());
    const [collapsed, setCollapsed] = React.useState(false);
    const [sseOk, setSseOk] = React.useState(true);
    const [running, setRunning] = React.useState(0);
    const [whitelist, setWhitelist] = React.useState(null);

    React.useEffect(function () {
      const onHash = function () {
        setPath(currentPath());
        window.scrollTo(0, 0);
      };
      window.addEventListener("hashchange", onHash);
      return () => window.removeEventListener("hashchange", onHash);
    }, []);

    React.useEffect(function () {
      const active = new Set();
      let eventSource = null;
      let closed = false;

      function connect() {
        eventSource = new EventSource("/api/tasks/stream");
        eventSource.onopen = function () {
          setSseOk(true);
        };
        eventSource.onerror = function () {
          setSseOk(false);
          // EventSource 会自动重连；这里只更新指示器
        };
        eventSource.onmessage = function (event) {
          try {
            const payload = JSON.parse(event.data);
            if (payload.type !== "task" && payload.type !== "log") return;
            if (payload.type === "task") {
              if (["PENDING", "RUNNING", "STOPPING"].indexOf(payload.status) >= 0) {
                active.add(payload.task_id);
              } else {
                active.delete(payload.task_id);
              }
              setRunning(active.size);
            }
            window.dispatchEvent(new CustomEvent("yafip-task", { detail: payload }));
          } catch (error) {
            /* ignore */
          }
        };
      }

      connect();
      const correct = setInterval(async function () {
        try {
          const health = await Y.api("/system/health");
          setRunning(
            (health.tasks.PENDING || 0) + (health.tasks.RUNNING || 0) + (health.tasks.STOPPING || 0)
          );
        } catch (error) {
          /* ignore */
        }
      }, 15000);

      Y.api("/overview")
        .then((data) => setWhitelist(data.whitelist))
        .catch(() => {});

      return function () {
        closed = true;
        clearInterval(correct);
        if (eventSource) eventSource.close();
      };
    }, []);

    const route = resolve(path);
    const PageComponent = Y.pages[route.page];
    const selected = [route.menu];
    document.title = "YaFip · " + route.title;

    return html`
      <${Layout} style=${{ minHeight: "100vh" }}>
        <${Sider} collapsible collapsed=${collapsed} onCollapse=${setCollapsed} theme="light" width=${220}>
          <div className="brand">
            <div className="brand-logo">y</div>
            ${collapsed ? null : html`<div><div className="brand-name">YaFip</div><div className="brand-sub">量化数据底座</div></div>`}
          </div>
          <${antd.Menu}
            mode="inline"
            selectedKeys=${selected}
            items=${MENU_ITEMS}
            onClick=${function (info) {
              if (info.key === "strategy") return;
              window.location.hash = "#/" + info.key;
            }}
          />
        <//>
        <${Layout}>
          <${Header} className="topbar">
            <div className="topbar-left">
              <${antd.Breadcrumb} items=${[{ title: "YaFip" }, { title: route.title }]} />
            </div>
            <div className="topbar-right">
              <${antd.Tooltip} title=${whitelist
                ? "白名单：" + Y.fmt.num(whitelist.members) + " 只" +
                  (whitelist.as_of_date ? "（市值日期 " + whitelist.as_of_date + "）" : "")
                : "白名单"}>
                <${antd.Tag} color=${whitelist && whitelist.members ? "blue" : "default"}>
                  ${whitelist ? "白名单 " + Y.fmt.num(whitelist.members) + " 只" : "白名单"}
                <//>
              <//>
              <${antd.Tag} color=${sseOk ? "green" : "orange"}>
                ${sseOk ? "实时连接" : "已降级轮询"}
              <//>
              <${antd.Badge} count=${running} size="small" showZero=${false}>
                <${antd.Button} size="small" onClick=${function () { window.location.hash = "#/tasks"; }}>
                  运行中
                <//>
              <//>
              <${antd.Button}
                type="primary"
                size="small"
                icon=${React.createElement(icons.CloudDownloadOutlined)}
                onClick=${function () { window.location.hash = "#/data/fetch"; }}
              >
                新建任务
              <//>
            </div>
          <//>
          <${Content} className="content">
            ${route.page === "task_detail"
              ? html`<${PageComponent} params=${route.params} />`
              : PageComponent
              ? html`<${PageComponent} />`
              : html`<${antd.Result} status="404" title="页面不存在" />`}
          <//>
          <${Footer} className="footer">
            YaFip v${Y.version} · 本地运行 · 数据仅供研究，不构成投资建议
          <//>
        <//>
      <//>
    `;
  }

  Y.start = function () {
    ReactDOM.createRoot(document.getElementById("root")).render(
      html`<${antd.ConfigProvider} theme=${{ token: { colorPrimary: "#1677ff", borderRadius: 6 } }}>
        <${App} />
      <//>`
    );
  };
})();
