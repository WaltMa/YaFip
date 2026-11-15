(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;

  function Tasks() {
    const [status, setStatus] = React.useState([]);
    const [source, setSource] = React.useState("");
    const [taskType, setTaskType] = React.useState("");
    const [keyword, setKeyword] = React.useState("");
    const [page, setPage] = React.useState(1);
    const [pageSize, setPageSize] = React.useState(20);
    const [tab, setTab] = React.useState("all");
    const timerRef = React.useRef(null);
    const [busyId, setBusyId] = React.useState(""); // 防重复点击：重跑/杀死只发一次请求

    const list = Y.useApi(
      function () {
        return Y.api(
          "/tasks" +
            Y.qs({
              status: status.join(","),
              source: source,
              task_type: taskType,
              keyword: keyword,
              page_number: page,
              page_size: pageSize,
            })
        );
      },
      [status.join(","), source, taskType, keyword, page, pageSize],
      3000
    );

    React.useEffect(
      function () {
        const onTask = function () {
          if (timerRef.current) return;
          timerRef.current = setTimeout(function () {
            timerRef.current = null;
            list.reload();
          }, 600);
        };
        window.addEventListener("yafip-task", onTask);
        return function () {
          window.removeEventListener("yafip-task", onTask);
          if (timerRef.current) {
            clearTimeout(timerRef.current);
            timerRef.current = null; // 不置空的话重建 effect 后 onTask 首行恒为真，SSE 刷新永久失效
          }
        };
      },
      [list.reload]
    );

    function applyTab(key) {
      setTab(key);
      setPage(1);
      if (key === "running") {
        setStatus(["PENDING", "RUNNING", "STOPPING"]);
        setSource("");
      } else if (key === "failed") {
        setStatus(["FAILED"]);
        setSource("");
      } else {
        setStatus([]);
        setSource("");
      }
    }

    async function kill(row) {
      if (busyId) return;
      setBusyId(row.id);
      Y.confirm("杀死任务", "将请求停止任务；已入库的交易日不会回滚，任务会在当前交易单元结束后退出。", async function () {
        try {
          await Y.api("/tasks/" + row.id + "/kill", { method: "POST" });
          antd.message.warning("已请求停止");
          list.reload();
        } catch (error) {
          Y.notifyError(error);
        } finally {
          setBusyId("");
        }
      }, function () { setBusyId(""); });
    }

    async function retry(row) {
      if (busyId) return;
      setBusyId(row.id);
      try {
        const data = await Y.api("/tasks/" + row.id + "/retry", { method: "POST" });
        antd.message.success("已重跑，新任务 " + data.task_id);
        window.location.hash = "#/tasks/" + data.task_id;
      } catch (error) {
        Y.notifyError(error);
      } finally {
        setBusyId("");
      }
    }

    function remove(row) {
      if (busyId) return;
      setBusyId(row.id);
      Y.confirm("删除任务", "将删除任务记录与日志（不删除已入库数据），不可恢复。", async function () {
        try {
          await Y.api("/tasks/" + row.id, { method: "DELETE" });
          antd.message.success("已删除");
          list.reload();
        } catch (error) {
          Y.notifyError(error);
        } finally {
          setBusyId("");
        }
      }, function () { setBusyId(""); });
    }

    const columns = [
      { title: "ID", dataIndex: "id", width: 100, render: (v) => html`<span className="mono">${Y.fmt.shortId(v)}</span>` },
      { title: "任务类型", dataIndex: "task_type", width: 170, render: (v) => v || "-" },
      { title: "执行器", dataIndex: "executor", width: 200, render: (v) => v || "-" },
      { title: "市场", dataIndex: "market", width: 70, render: (v) => v || "-" },
      { title: "来源", dataIndex: "source", width: 80, render: (v) => html`<${Y.SourceTag} source=${v} />` },
      { title: "状态", dataIndex: "status", width: 100, render: (v) => html`<${Y.TaskStatusTag} status=${v} />` },
      {
        title: "进度",
        dataIndex: "progress",
        render: (v, r) => html`<${Y.ProgressCell} progress=${v} status=${r.status} stage=${r.stage || r.message} />`,
      },
      { title: "创建时间", dataIndex: "created_at", width: 150, render: Y.fmt.time },
      {
        title: "耗时",
        width: 80,
        render: (_, r) => {
          if (!r.started_at) return "-";
          const start = new Date(r.started_at);
          const end = r.finished_at ? new Date(r.finished_at) : new Date();
          return Math.max(0, Math.round((end - start) / 1000)) + "s";
        },
      },
      {
        title: "操作",
        width: 170,
        render: (_, r) => html`<${antd.Space} size="small" onClick=${function (event) { event.stopPropagation(); }}>
          <${antd.Button} size="small" type="link" onClick=${function () { window.location.hash = "#/tasks/" + r.id; }}>详情<//>
          ${["PENDING", "RUNNING", "STOPPING"].indexOf(r.status) >= 0
            ? html`<${antd.Button} size="small" type="link" danger disabled=${!!busyId} onClick=${function () { kill(r); }}>杀死<//>`
            : html`<${antd.Button} size="small" type="link" disabled=${!!busyId} onClick=${function () { retry(r); }}>重跑<//>`}
          ${["SUCCESS", "FAILED", "KILLED"].indexOf(r.status) >= 0
            ? html`<${antd.Button} size="small" type="link" danger disabled=${!!busyId} onClick=${function () { remove(r); }}>删除<//>`
            : null}
        <//>`,
      },
    ];

    const TASK_TYPE_OPTIONS = [
      { value: "meta.sync_all", label: "元数据全量同步" },
      { value: "meta.sync_symbols", label: "指定标的元数据" },
      { value: "whitelist.update", label: "白名单更新" },
      { value: "bars.incremental", label: "日K增量同步" },
      { value: "bars.range", label: "日K区间同步" },
      { value: "quality.scan", label: "质量扫描" },
    ];

    return html`
      <div>
        <${Y.PageHeader} title="任务列表" subtitle="元数据 / 白名单 / 日K / 质量扫描 全部在此（实时刷新）" />

        <${antd.Tabs}
          activeKey=${tab}
          onChange=${applyTab}
          items=${[
            { key: "all", label: "全部" },
            { key: "running", label: "运行中" },
            { key: "failed", label: "失败" },
          ]}
        />

        <${antd.Card} size="small" style=${{ marginBottom: 16 }}>
          <${antd.Space} wrap>
            <${antd.Select}
              mode="multiple"
              allowClear
              placeholder="状态"
              style=${{ minWidth: 200 }}
              value=${status}
              onChange=${function (value) { setStatus(value); setPage(1); }}
              options=${[
                { value: "PENDING", label: "排队中" },
                { value: "RUNNING", label: "运行中" },
                { value: "STOPPING", label: "停止中" },
                { value: "SUCCESS", label: "成功" },
                { value: "FAILED", label: "失败" },
                { value: "KILLED", label: "已杀死" },
              ]}
            />
            <${antd.Select}
              allowClear
              placeholder="来源"
              style=${{ width: 130 }}
              value=${source || undefined}
              onChange=${function (value) { setSource(value || ""); setPage(1); }}
              options=${[
                { value: "manual", label: "手动" },
                { value: "system", label: "系统" },
              ]}
            />
            <${antd.Select}
              allowClear
              placeholder="任务类型"
              style=${{ width: 190 }}
              value=${taskType || undefined}
              onChange=${function (value) { setTaskType(value || ""); setPage(1); }}
              options=${TASK_TYPE_OPTIONS}
            />
            <${antd.Input.Search}
              placeholder="搜索 ID / 阶段 / 类型"
              style=${{ width: 240 }}
              allowClear
              onSearch=${function (value) { setKeyword(value); setPage(1); }}
            />
            <${antd.Button} onClick=${list.reload}>刷新<//>
          <//>
        <//>

        <${antd.Card}>
          <${antd.Table}
            rowKey="id"
            size="middle"
            loading=${list.loading}
            dataSource=${(list.data && list.data.items) || []}
            columns=${columns}
            scroll=${{ x: 1050 }}
            onRow=${function (record) {
              return {
                onClick: function () { window.location.hash = "#/tasks/" + record.id; },
                className: "clickable-row",
              };
            }}
            pagination=${{
              current: page,
              pageSize: pageSize,
              total: (list.data && list.data.total) || 0,
              showSizeChanger: true,
              onChange: function (nextPage, nextSize) {
                setPage(nextPage);
                setPageSize(nextSize);
              },
            }}
          />
        <//>
      </div>
    `;
  }

  Y.pages.tasks = Tasks;
})();
