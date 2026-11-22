/*
 * 前端冒烟测试：加载真实 app.js/components/ui.js + 全部页面，
 * 用后端形状 fixture 驱动渲染，捕获模板/字段/单元错误（无 DOM，antd 用 Proxy 桩 + props 捕获）。
 *
 * 运行：node tests/frontend_smoke.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");

// ---------------------------------------------------------------------
// antd Proxy 桩：组件经 React.createElement 构造（无 DOM 不会真正调用），
// 因此在 createElement 处捕获（name + props），供契约断言
// ---------------------------------------------------------------------
const rendered = [];
const SMOKE_MARKER = "__smoke_name__";

function makeComponent(name) {
  const fn = function () {
    return null;
  };
  return new Proxy(fn, {
    get: function (_target, prop) {
      if (prop === "then") return undefined;
      if (prop === SMOKE_MARKER) return name;
      if (prop === "useForm") {
        return function () {
          return [
            {
              setFieldsValue: function () {},
              setFieldValue: function () {},
              getFieldValue: function () {},
              resetFields: function () {},
              validateFields: async function () {
                return {};
              },
            },
          ];
        };
      }
      return makeComponent(name + "." + String(prop));
    },
    apply: function (_target, _thisArg, args) {
      if (args && args[0] && typeof args[0] === "object") {
        rendered.push({ name: name, props: args[0] });
      }
      return null;
    },
  });
}

const antd = makeComponent("antd");
const icons = makeComponent("icons");

const sandbox = {
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  URLSearchParams: URLSearchParams,
  Blob: function () {},
  URL: { createObjectURL: function () { return ""; }, revokeObjectURL: function () {} },
  setTimeoutRef: null,
};

sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.antd = antd;
sandbox.icons = icons;

const React = {
  createElement: function (type, props) {
    if (type && typeof type === "function" && type[SMOKE_MARKER]) {
      rendered.push({
        name: type[SMOKE_MARKER],
        props: props || {},
        children: Array.prototype.slice.call(arguments, 2),
      });
    }
    return { type: type, props: props, children: Array.prototype.slice.call(arguments, 2) };
  },
  useState: function (initial) {
    return [typeof initial === "function" ? initial() : initial, function () {}];
  },
  useEffect: function () {},
  useCallback: function (fn) {
    return fn;
  },
  useRef: function (value) {
    return { current: value };
  },
  useMemo: function (fn) {
    return fn();
  },
  Fragment: "Fragment",
};
sandbox.React = React;
sandbox.ReactDOM = { createRoot: function () { return { render: function () {} }; } };

function fakeDayjs() {
  return {
    format: function () { return "2025-01-01"; },
    subtract: function () { return fakeDayjs(); },
    add: function () { return fakeDayjs(); },
    startOf: function () { return fakeDayjs(); },
    endOf: function () { return fakeDayjs(); },
    toISOString: function () { return "2025-01-01T00:00:00"; },
  };
}
sandbox.dayjs = fakeDayjs;

sandbox.location = { hash: "#/overview" };
sandbox.addEventListener = function () {};
sandbox.removeEventListener = function () {};
sandbox.dispatchEvent = function () {};
sandbox.scrollTo = function () {};
sandbox.document = { title: "" };
sandbox.CustomEvent = function (name, options) { return { type: name, detail: options && options.detail }; };
sandbox.EventSource = function () { return { close: function () {} }; };
sandbox.fetch = async function () { return { ok: true, json: async function () { return { code: 0, data: {} }; } }; };

// htm UMD
vm.runInNewContext(fs.readFileSync(path.join(ROOT, "web/vendor/htm.js"), "utf8"), sandbox, {
  filename: "htm.js",
});

// 先加载真实 app.js（Y.api/Y.qs/Y.fmt/Y.useApi/路由）与 components/ui.js（共享组件），
// 再加载页面——修复前 smoke 只加载 pages，app.js/ui.js 的绑定错误全部测不到。
vm.runInNewContext(fs.readFileSync(path.join(ROOT, "web/app.js"), "utf8"), sandbox, {
  filename: "app.js",
});
vm.runInNewContext(fs.readFileSync(path.join(ROOT, "web/components/ui.js"), "utf8"), sandbox, {
  filename: "ui.js",
});

// ---------------------------------------------------------------------
// 后端形状 fixture + 同步 API 桩（真实 Y.api 走 fetch，无 DOM 环境不可用）
// ---------------------------------------------------------------------
const FIXTURES = [
  ["/overview", {
    instruments: 5236,
    bar_rows: 1234567,
    bar_dates: 3000,
    adj_rows: 2900,
    latest_bar_date: "2026-11-20",
    db_bytes: 104857600,
    quality_open: 3,
    watermark: "2026-11-19",
    whitelist: { members: 88, as_of_date: "2026-11-19", computed_at: "2026-11-20T10:00:00" },
    tasks: { PENDING: 1, RUNNING: 0, STOPPING: 0, SUCCESS: 100, FAILED: 5, KILLED: 1 },
    datasource: { name: "tushare", has_token: true, enabled: true, last_test_at: null, last_test_ok: null, last_test_msg: "" },
    recent_tasks: [
      { id: "abc1234567890abcdef", task_type: "bars.incremental", executor: "tushare.bars.incremental",
        source: "manual", status: "SUCCESS", progress: 100, stage: "完成", created_at: "2026-11-20T09:00:00" },
    ],
  }],
  ["/system/health", {
    version: "0.1.0", db_ok: true, db_bytes: 104857600, stopping: false, sse_subscribers: 2, active_tasks: 0,
    tasks: { PENDING: 0, RUNNING: 0, STOPPING: 0, SUCCESS: 100, FAILED: 5, KILLED: 1 },
    whitelist: { members: 88, as_of_date: "2026-11-19", computed_at: "2026-11-20T10:00:00",
                 threshold_wan: 500000, threshold_yi: 50, metric_label: "最新交易日总市值（万元）" },
  }],
  ["/datasources", { items: [
    { name: "tushare", title: "tushare pro", description: "A 股日 K", homepage: "https://tushare.pro",
      requires_token: true, supports_test: true, implemented: true, token_masked: "123****cdef",
      has_token: true, api_url: "https://api.tushare.pro", rpm_limit: 0, enabled: true,
      last_test_at: null, last_test_ok: null, last_test_msg: "" },
    { name: "akshare", title: "akshare", description: "港美股日 K", homepage: "https://akshare.akfamily.xyz",
      requires_token: false, supports_test: true, implemented: true, token_masked: "",
      has_token: false, api_url: "", rpm_limit: 0, enabled: true, last_test_at: null, last_test_ok: null, last_test_msg: "" },
  ] }],
  ["/whitelist/config", { members: 88, as_of_date: "2026-11-19", computed_at: "2026-11-20T10:00:00",
                           threshold_wan: 500000, threshold_yi: 50, metric: "total_mv",
                           metric_label: "最新交易日总市值（万元）" }],
  ["/data/quality-issues", { items: [], total: 0 }],
  ["/data/coverage", { items: [] }],
  ["/tasks/", {
    id: "abc1234567890abcdef", task_type: "bars.incremental", executor: "tushare.bars.incremental",
    market: "CN_A", source: "manual", status: "SUCCESS", progress: 100, stage: "完成",
    lock_key: "bars.incremental:CN_A", retry_of: null, created_at: "2026-11-20T09:00:00",
    started_at: "2026-11-20T09:00:01", finished_at: "2026-11-20T09:00:10",
    params: {}, error: null, message: null,
    result: { dates_total: 3, dates_done: 3, dates_failed: 0, dates_reused: 2,
              rows_daily: 9, filtered_rows: 0, hit_count: 3, elapsed_sec: 2.1 },
  }],
  ["/tasks", { items: [], total: 0, page: 1, page_size: 20 }],
  ["/config", { "server.port": "8203", "task.max_concurrency": "10", "fetch.rpm": "120",
                "data.start_date": "2010-01-01", "sse.heartbeat_sec": "15" }],
  ["/system/audit", { items: [], total: 0, page: 1, page_size: 50 }],
];

function lookupFixture(pathname) {
  let best = null;
  let bestLen = -1;
  for (const [prefix, data] of FIXTURES) {
    if (pathname.indexOf(prefix) === 0 && prefix.length > bestLen) {
      best = data;
      bestLen = prefix.length;
    }
  }
  return best;
}

const Y = sandbox.YAFIP;
Y.api = function (pathname) {
  const data = lookupFixture(String(pathname));
  return data === null ? {} : data;
};
Y.useApi = function (loader) {
  let data = null;
  let error = null;
  try {
    if (typeof loader === "string") data = lookupFixture(loader);
    else if (typeof loader === "function") data = loader(); // 同步桩：loader 调 Y.api 直接返回值
  } catch (exc) {
    error = exc && exc.message ? exc.message : String(exc);
  }
  return { data: data, loading: false, error: error, reload: function () {} };
};

// ---------------------------------------------------------------------
// 加载全部页面并渲染
// ---------------------------------------------------------------------
const pageFiles = [
  "overview",
  "datasource",
  "fetch",
  "explore",
  "quality",
  "whitelist",
  "tasks",
  "task_detail",
  "settings",
  "audit",
  "about",
];

for (const file of pageFiles) {
  const source = fs.readFileSync(path.join(ROOT, "web/pages", file + ".js"), "utf8");
  vm.runInNewContext(source, sandbox, { filename: file + ".js" });
}

let failures = 0;
function fail(message) {
  failures += 1;
  console.error("FAIL " + message);
}

for (const key of Object.keys(Y.pages)) {
  const Component = Y.pages[key];
  try {
    Component({ params: { id: "abc1234567890abcdef" } });
    console.log("OK   " + key);
  } catch (error) {
    failures += 1;
    console.error("FAIL " + key + ": " + (error && error.stack ? error.stack : error));
  }
}

// ---------------------------------------------------------------------
// 契约断言：修复过的字段/单位/展示必须与后端形状一致
// ---------------------------------------------------------------------
function statValue(title) {
  const hit = rendered.find((r) => r.name === "antd.Statistic" && r.props && r.props.title === title);
  return hit ? hit.props.value : undefined;
}

function overviewTable() {
  return rendered.find(
    (r) => r.name === "antd.Table" && Array.isArray(r.props.dataSource) &&
      r.props.dataSource.length && r.props.dataSource[0].id === "abc1234567890abcdef"
  );
}

// F2 overview：统计值来自后端字段
if (statValue("股票列表") !== 5236) fail("overview 股票列表应读 instruments（fixture=5236），实际 " + statValue("股票列表"));
if (statValue("日 K 行数") !== 1234567) fail("overview 日 K 行数应读 bar_rows");
if (statValue("覆盖交易日") !== 3000) fail("overview 覆盖交易日应读 bar_dates");
if (statValue("白名单明细") !== 88) fail("overview 白名单明细应读 whitelist.members");

// F2 overview 最近任务列：task_type/executor，不含已下线的 business/action
const table = overviewTable();
if (table) {
  const indexes = (table.props.columns || []).map((c) => c.dataIndex);
  if (indexes.indexOf("task_type") < 0 || indexes.indexOf("executor") < 0) {
    fail("overview 最近任务列应含 task_type/executor：" + indexes.join(","));
  }
  if (indexes.indexOf("business") >= 0 || indexes.indexOf("action") >= 0) {
    fail("overview 最近任务列不应含已下线的 business/action");
  }
} else {
  fail("overview 未渲染最近任务表");
}

// F7 task_detail：复用卡片（dates_reused），不是 dates_skipped
if (statValue("复用") !== 2) fail("task_detail 应展示 dates_reused=2 的「复用」卡片");

// F6 settings：白名单展示 members（不再读 enabled/included）
const wlItem = rendered.find((r) => r.name === "antd.Descriptions.Item" && r.props && r.props.label === "白名单");
const wlChildren = wlItem ? (Array.isArray(wlItem.children) ? wlItem.children.join("") : String(wlItem.children || "")) : "";
if (!wlItem || wlChildren.indexOf("88") < 0) {
  fail("settings 白名单项应展示明细数量（fixture members=88），实际：" + wlChildren);
}

// ---------------------------------------------------------------------
// 源码级回归守卫：修复过的前端字段引用不允许回退
// ---------------------------------------------------------------------
function readPage(name) {
  return fs.readFileSync(path.join(ROOT, "web/pages", name + ".js"), "utf8");
}

const overviewSrc = readPage("overview");
for (const bad of ["s.stocks", "s.daily_rows", "s.daily_dates", "s.next_pending_date", "business", "wl.included", "wl.enabled"]) {
  if (overviewSrc.indexOf(bad) >= 0) fail("overview.js 不应再引用已下线字段：" + bad);
}

const detailSrc = readPage("task_detail");
if (detailSrc.indexOf("dates_skipped") >= 0) fail("task_detail.js 不应再读 dates_skipped");

const exploreSrc = readPage("explore");
if (exploreSrc.indexOf("成交量(手)") >= 0) fail("explore.js 不应再硬编码「成交量(手)」");
if (exploreSrc.indexOf("港元") < 0 || exploreSrc.indexOf("美元") < 0) {
  fail("explore.js 应按市场映射单位（股/港元、股/美元）");
}

const datasourceSrc = readPage("datasource");
if (datasourceSrc.indexOf("requires_token") < 0) fail("datasource.js 应使用 requires_token 判定 Token 必填");

const qualitySrc = readPage("quality");
if (qualitySrc.indexOf("akshare.bars.range") < 0) fail("quality.js 一键补缺应按市场选 akshare.bars.range");

const whitelistSrc = readPage("whitelist");
if (whitelistSrc.indexOf("disabled=${market !== \"CN_A\"}") < 0) fail("whitelist.js 更新白名单按钮应在港美股禁用");

const fetchSrc = readPage("fetch");
if (fetchSrc.indexOf("props.onChange") < 0) fail("fetch.js FieldControl 应透传 value/onChange");

if (failures) {
  process.exit(1);
}
console.log("frontend smoke ok: " + Object.keys(Y.pages).length + " pages + contract assertions");
