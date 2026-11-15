(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;
  const fs = Y.fmt;

  Y.PageHeader = function (props) {
    return html`
      <div className="page-head">
        <div>
          <h1 className="page-title">${props.title}</h1>
          ${props.subtitle ? html`<div className="page-sub">${props.subtitle}</div>` : null}
        </div>
        <div>${props.extra || null}</div>
      </div>
    `;
  };

  Y.EmptyNote = function (props) {
    return html`<div className="empty-note">${props.children || "暂无数据"}</div>`;
  };

  const STATUS = {
    PENDING: { color: "default", text: "排队中" },
    RUNNING: { color: "processing", text: "运行中" },
    STOPPING: { color: "warning", text: "停止中" },
    SUCCESS: { color: "success", text: "成功" },
    FAILED: { color: "error", text: "失败" },
    KILLED: { color: "orange", text: "已杀死" },
  };

  Y.TaskStatusTag = function (props) {
    const meta = STATUS[props.status] || { color: "default", text: props.status };
    return html`<${antd.Tag} color=${meta.color}>${meta.text}<//>`;
  };

  Y.SourceTag = function (props) {
    const map = {
      manual: { color: "blue", text: "手动" },
      schedule: { color: "purple", text: "例行" },
      system: { color: "cyan", text: "系统" },
    };
    const meta = map[props.source] || { color: "default", text: props.source };
    return html`<${antd.Tag} color=${meta.color}>${meta.text}<//>`;
  };

  Y.ProgressCell = function (props) {
    const status = props.status || "";
    const percent = Math.round(Number(props.progress || 0));
    const state = status === "FAILED" ? "exception" : status === "SUCCESS" ? "success" : "active";
    return html`
      <div style=${{ minWidth: 120 }}>
        <${antd.Progress}
          percent=${percent}
          size="small"
          status=${status === "RUNNING" || status === "STOPPING" || status === "PENDING" ? state : state}
        />
        ${props.stage ? html`<div style=${{ fontSize: 11, color: "#6b7280" }}>${props.stage}</div>` : null}
      </div>
    `;
  };

  Y.JsonViewer = function (props) {
    let text = props.value;
    if (typeof text !== "string") {
      try {
        text = JSON.stringify(text, null, 2);
      } catch (error) {
        text = String(text);
      }
    }
    return html`<pre className="code-block">${text || "-"}</pre>`;
  };

  Y.LogViewer = function (props) {
    const taskId = props.taskId;
    const live = props.live !== false;
    const [logs, setLogs] = React.useState([]);
    const [paused, setPaused] = React.useState(false);
    const sinceRef = React.useRef(0);
    const boxRef = React.useRef(null);

    const load = React.useCallback(
      async function (force) {
        // 暂停时自动拉取停掉，但手动点「刷新」应能强制拉一次（force=true）
        if (!taskId || (paused && !force)) return;
        try {
          const data = await Y.api(
            "/tasks/" + taskId + "/logs?since_id=" + sinceRef.current + "&limit=1000"
          );
          if (data.items && data.items.length) {
            sinceRef.current = data.items[data.items.length - 1].id;
            setLogs((previous) => previous.concat(data.items).slice(-5000));
          }
        } catch (error) {
          /* 日志失败不打断页面 */
        }
      },
      [taskId, paused]
    );

    React.useEffect(
      function () {
        setLogs([]);
        sinceRef.current = 0;
        load();
      },
      [taskId]
    );

    React.useEffect(
      function () {
        if (!live || paused) return undefined;
        const timer = setInterval(load, 2000);
        const onTask = function (event) {
          if (event.detail && event.detail.task_id === taskId) load();
        };
        window.addEventListener("yafip-task", onTask);
        return function () {
          clearInterval(timer);
          window.removeEventListener("yafip-task", onTask);
        };
      },
      [live, paused, load]
    );

    React.useEffect(
      function () {
        if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
      },
      [logs]
    );

    function download() {
      const text = logs
        .map(function (line) {
          return "[" + fs.time(line.ts) + "] " + line.level + " " + line.message;
        })
        .join("\n");
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "task-" + taskId + ".log";
      link.click();
      URL.revokeObjectURL(url);
    }

    return html`
      <div>
        <div style=${{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
          <${antd.Space}>
            <${antd.Tag} color=${paused ? "orange" : "green"}>${paused ? "已暂停" : "实时"}</${antd.Tag}>
            <span style=${{ color: "#6b7280", fontSize: 12 }}>共 ${logs.length} 行</span>
          <//>
          <${antd.Space}>
            <${antd.Button} size="small" onClick=${function () { setPaused(!paused); }}>
              ${paused ? "继续" : "暂停"}
            <//>
            <${antd.Button} size="small" onClick=${function () { load(true); }}>刷新<//>
            <${antd.Button} size="small" onClick=${download}>下载<//>
          <//>
        </div>
        <div className="log-viewer" ref=${boxRef}>
          ${logs.length
            ? logs.map(function (line) {
                return html`<span key=${line.id} className=${"log-line lvl-" + line.level}>
                  [${fs.time(line.ts)}] ${line.level} ${line.message}
                </span>`;
              })
            : html`<span style=${{ color: "#64748b" }}>暂无日志</span>`}
        </div>
      </div>
    `;
  };
})();
