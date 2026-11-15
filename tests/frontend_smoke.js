/*
 * 前端模板冒烟测试：用 Node 执行每个页面的顶层渲染，捕获 htm 模板的标签/插值错误。
 * 不加载真实 antd（不需要 DOM），所有 antd 组件用 Proxy 桩替代。
 *
 * 运行：node tests/frontend_smoke.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");

function makeComponent(name) {
  const fn = function () {
    return null;
  };
  return new Proxy(fn, {
    get: function (_target, prop) {
      if (prop === "then") return undefined;
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
    apply: function () {
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

const React = {
  createElement: function (type, props) {
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

// htm UMD
vm.runInNewContext(fs.readFileSync(path.join(ROOT, "web/vendor/htm.js"), "utf8"), sandbox, {
  filename: "htm.js",
});

const Y = {
  pages: {},
  antd: antd,
  icons: icons,
  version: "0.1.0",
  html: sandbox.htm.bind(React.createElement),
  api: async function () { return {}; },
  qs: function () { return ""; },
  useApi: function () {
    return { data: null, loading: false, error: null, reload: function () {} };
  },
  notifyError: function () {},
  confirm: function () {},
  fmt: {
    num: function (v) { return String(v == null ? "-" : v); },
    bytes: function (v) { return String(v); },
    time: function (v) { return String(v || "-"); },
    shortId: function (v) { return String(v || "").slice(0, 8); },
    pct: function (v) { return String(v); },
    range: function () { return { start: "2025-01-01", end: "2025-12-31" }; },
  },
  PageHeader: function () { return null; },
  EmptyNote: function () { return null; },
  TaskStatusTag: function () { return null; },
  SourceTag: function () { return null; },
  ProgressCell: function () { return null; },
  JsonViewer: function () { return null; },
  LogViewer: function () { return null; },
};
sandbox.YAFIP = Y;
sandbox.location = { hash: "#/overview" };
sandbox.addEventListener = function () {};
sandbox.removeEventListener = function () {};
sandbox.dispatchEvent = function () {};
sandbox.scrollTo = function () {};
sandbox.document = { title: "" };
sandbox.CustomEvent = function (name, options) { return { type: name, detail: options && options.detail }; };
sandbox.EventSource = function () { return { close: function () {} }; };
sandbox.fetch = async function () { return { ok: true, json: async function () { return { code: 0, data: {} }; } }; };

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
for (const key of Object.keys(Y.pages)) {
  const Component = Y.pages[key];
  try {
    Component({ params: { id: "test-task-id" } });
    console.log("OK   " + key);
  } catch (error) {
    failures += 1;
    console.error("FAIL " + key + ": " + (error && error.stack ? error.stack : error));
  }
}

if (failures) {
  process.exit(1);
}
console.log("frontend smoke ok: " + Object.keys(Y.pages).length + " pages");
