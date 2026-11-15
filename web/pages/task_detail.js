(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;

  function TaskDetail(props) {
    const id = props.params && props.params.id;
    const task = Y.useApi("/tasks/" + id, [id], 3000);
    const record = task.data;

    React.useEffect(
      function () {
        const onTask = function (event) {
          if (event.detail && event.detail.task_id === id) task.reload();
        };
        window.addEventListener("yafip-task", onTask);
        return () => window.removeEventListener("yafip-task", onTask);
      },
      [id, task.reload]
    );

    async function kill() {
      Y.confirm("杀死任务", "请求停止；已入库单元不回滚，将在当前交易单元结束后退出。", async function () {
        try {
          await Y.api("/tasks/" + id + "/kill", { method: "POST" });
          antd.message.warning("已请求停止");
          task.reload();
        } catch (error) {
          Y.notifyError(error);
        }
      });
    }

    async function retry() {
      try {
        const data = await Y.api("/tasks/" + id + "/retry", { method: "POST" });
        antd.message.success("已重跑");
        window.location.hash = "#/tasks/" + data.task_id;
      } catch (error) {
        Y.notifyError(error);
      }
    }

    function remove() {
      Y.confirm("删除任务", "删除任务记录与日志（不删除已入库数据），不可恢复。", async function () {
        try {
          await Y.api("/tasks/" + id, { method: "DELETE" });
          antd.message.success("已删除");
          window.location.hash = "#/tasks";
        } catch (error) {
          Y.notifyError(error);
        }
      });
    }

    if (task.error) {
      return html`<${antd.Alert} type="error" showIcon message="加载任务失败" description=${task.error} />`;
    }
    if (!record) return html`<${antd.Spin} size="large" />`;

    const active = ["PENDING", "RUNNING", "STOPPING"].indexOf(record.status) >= 0;
    const step = record.status === "PENDING" ? 1 : record.status === "SUCCESS" ? 4 : active ? 2 : 2;
    const result = record.result;
    const failedUnits = (result && result.failed_units) || [];

    const resultColumns = [
      { title: "交易日", dataIndex: "trade_date", width: 130 },
      { title: "错误", dataIndex: "error" },
    ];

    return html`
      <div>
        <${Y.PageHeader}
          title=${"任务 " + Y.fmt.shortId(record.id)}
          subtitle=${record.task_type + " · " + (record.executor || "-") + " · " + (record.market || "跨市场") + " · " + record.source}
          extra=${html`<${antd.Space}>
            <${antd.Button} onClick=${function () { window.location.hash = "#/tasks"; }}>返回列表<//>
            ${active
              ? html`<${antd.Button} danger onClick=${kill}>杀死<//>`
              : html`<${antd.Button} type="primary" onClick=${retry}>重跑<//>`}
            ${!active ? html`<${antd.Button} danger onClick=${remove}>删除<//>` : null}
          <//>`}
        />

        ${record.error
          ? html`<${antd.Alert}
              type="error"
              showIcon
              style=${{ marginBottom: 16 }}
              message="任务失败"
              description=${html`<pre className="code-block" style=${{ maxHeight: 240 }}>${record.error}</pre>`}
            />`
          : null}

        <${antd.Card} style=${{ marginBottom: 16 }}>
          <div style=${{ display: "flex", alignItems: "center", gap: 16, marginBottom: 12 }}>
            <${Y.TaskStatusTag} status=${record.status} />
            <${antd.Tag}>${record.stage || "-"}</${antd.Tag}>
            ${record.message ? html`<span style=${{ color: "#6b7280" }}>${record.message}</span>` : null}
          </div>
          <${antd.Progress}
            percent=${Math.round(Number(record.progress || 0))}
            status=${record.status === "FAILED" ? "exception" : record.status === "SUCCESS" ? "success" : "active"}
          />
          <${antd.Steps}
            style=${{ marginTop: 20 }}
            size="small"
            current=${step}
            items=${[
              { title: "创建" },
              { title: "排队" },
              { title: "抓取中" },
              { title: "复权计算" },
              { title: "完成" },
            ]}
          />
        <//>

        <${antd.Tabs}
          items=${[
            {
              key: "overview",
              label: "概览",
              children: html`<div>
                <${antd.Descriptions} bordered column=${{ xs: 1, md: 2 }} size="small" style=${{ marginBottom: 16 }}>
                  <${antd.Descriptions.Item} label="任务 ID"><span className="mono">${record.id}</span><//>
                  <${antd.Descriptions.Item} label="状态">${record.status}<//>
                  <${antd.Descriptions.Item} label="任务类型">${record.task_type}<//>
                  <${antd.Descriptions.Item} label="执行器">${record.executor}<//>
                  <${antd.Descriptions.Item} label="市场">${record.market || "跨市场"}<//>
                  <${antd.Descriptions.Item} label="来源">${record.source}<//>
                  <${antd.Descriptions.Item} label="锁">${record.lock_key}<//>
                  <${antd.Descriptions.Item} label="重跑自">${record.retry_of || "-"}<//>
                  <${antd.Descriptions.Item} label="创建">${Y.fmt.time(record.created_at)}<//>
                  <${antd.Descriptions.Item} label="开始">${Y.fmt.time(record.started_at)}<//>
                  <${antd.Descriptions.Item} label="结束">${Y.fmt.time(record.finished_at)}<//>
                  <${antd.Descriptions.Item} label="进度">${Math.round(record.progress)}%<//>
                <//>

                ${result
                  ? html`<div>
                      <div className="stat-grid">
                        ${result.dates_total !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="交易日" value=${result.dates_total} /><//>`
                          : null}
                        ${result.dates_done !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="完成" value=${result.dates_done} /><//>`
                          : null}
                        ${result.dates_skipped !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="跳过" value=${result.dates_skipped} /><//>`
                          : null}
                        ${result.dates_failed !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="失败" value=${result.dates_failed} valueStyle=${{ color: result.dates_failed ? "#cf1322" : undefined }} /><//>`
                          : null}
                        ${result.rows_daily !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="日K写入" value=${result.rows_daily} formatter=${Y.fmt.num} /><//>`
                          : null}
                        ${result.filtered_rows !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="白名单过滤" value=${result.filtered_rows} formatter=${Y.fmt.num} /><//>`
                          : null}
                        ${result.hit_count !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="白名单入选" value=${result.hit_count} /><//>`
                          : null}
                        ${result.elapsed_sec !== undefined
                          ? html`<${antd.Card} size="small"><${antd.Statistic} title="耗时(秒)" value=${result.elapsed_sec} /><//>`
                          : null}
                      </div>
                      ${failedUnits.length
                        ? html`<${antd.Card} size="small" title="失败单元" style=${{ marginBottom: 16 }}>
                            <${antd.Table} rowKey="trade_date" size="small" pagination=${{ pageSize: 10 }} dataSource=${failedUnits} columns=${resultColumns} />
                          <//>`
                        : null}
                      <${antd.Card} size="small" title="结果 JSON">
                        <${Y.JsonViewer} value=${result} />
                      <//>
                    </div>`
                  : html`<${Y.EmptyNote}>任务尚未产生结果<//>`}
              </div>`,
            },
            {
              key: "logs",
              label: "实时日志",
              children: html`<${Y.LogViewer} taskId=${record.id} live=${active} />`,
            },
            {
              key: "params",
              label: "参数快照",
              children: html`<${Y.JsonViewer} value=${record.params} />`,
            },
          ]}
        />
      </div>
    `;
  }

  Y.pages.task_detail = TaskDetail;
})();
