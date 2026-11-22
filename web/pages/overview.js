(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const fs = Y.fmt;

  function Overview() {
    const { data, loading, error } = Y.useApi("/overview", [], 10000);
    const health = Y.useApi("/system/health", [], 15000);

    if (error) return html`<${antd.Alert} type="error" showIcon message="加载概览失败" description=${error} />`;
    const s = data || {};
    const wl = s.whitelist || {};
    const tasks = s.tasks || {};
    const datasource = s.datasource || {};

    const columns = [
      { title: "任务", dataIndex: "id", width: 110, render: (v) => html`<span className="mono">${fs.shortId(v)}</span>` },
      { title: "任务类型", dataIndex: "task_type", width: 150 },
      { title: "执行器", dataIndex: "executor", width: 150 },
      { title: "来源", dataIndex: "source", width: 90, render: (v) => html`<${Y.SourceTag} source=${v} />` },
      { title: "状态", dataIndex: "status", width: 100, render: (v) => html`<${Y.TaskStatusTag} status=${v} />` },
      { title: "进度", dataIndex: "progress", render: (v, r) => html`<${Y.ProgressCell} progress=${v} status=${r.status} stage=${r.stage} />` },
      { title: "创建时间", dataIndex: "created_at", width: 160, render: fs.time },
    ];

    return html`
      <div>
        <${Y.PageHeader}
          title="系统概览"
          subtitle="数据覆盖度、任务运行、白名单与健康状态（10 秒自动刷新）"
          extra=${html`<${antd.Button} type="primary" onClick=${function () { window.location.hash = "#/data/fetch"; }}>新建抓取<//>`}
        />

        ${!datasource.has_token
          ? html`<${antd.Alert}
              type="warning"
              showIcon
              style=${{ marginBottom: 16 }}
              message="尚未配置 tushare token"
              description="请先在「数据源配置」录入 token 并测试连接，否则所有抓取都会失败。"
              action=${html`<${antd.Button} size="small" onClick=${function () { window.location.hash = "#/data/source"; }}>去配置<//>`}
            />`
          : null}

        <div className="stat-grid">
          <${antd.Card} size="small"><${antd.Statistic} title="股票列表" value=${s.instruments || 0} /><//>
          <${antd.Card} size="small"><${antd.Statistic} title="日 K 行数" value=${s.bar_rows || 0} formatter=${fs.num} /><//>
          <${antd.Card} size="small"><${antd.Statistic} title="覆盖交易日" value=${s.bar_dates || 0} /><//>
          <${antd.Card} size="small">
            <${antd.Statistic} title="白名单明细" value=${wl.members || 0} valueStyle=${{ color: "#1677ff" }} />
            <div className="empty-note">${wl.as_of_date ? "市值日期 " + wl.as_of_date : "暂无市值日期"}</div>
          <//>
          <${antd.Card} size="small"><${antd.Statistic} title="数据库体积" value=${fs.bytes(s.db_bytes)} /><//>
          <${antd.Card} size="small">
            <${antd.Statistic} title="未处理质量问题" value=${s.quality_open || 0} valueStyle=${{ color: (s.quality_open || 0) > 0 ? "#fa541c" : undefined }} />
          <//>
        </div>

        <${antd.Card} size="small" title="数据状态" style=${{ marginBottom: 16 }} loading=${loading}>
          <${antd.Descriptions} column=${{ xs: 1, sm: 2, md: 3 }} size="small">
            <${antd.Descriptions.Item} label="最新数据日">${s.latest_bar_date || "-"}<//>
            <${antd.Descriptions.Item} label="增量水位">${s.watermark || "-"}<//>
            <${antd.Descriptions.Item} label="复权因子行">${s.adj_rows || 0}<//>
            <${antd.Descriptions.Item} label="任务">${"运行 " + ((tasks.RUNNING || 0) + (tasks.PENDING || 0) + (tasks.STOPPING || 0)) + " / 成功 " + (tasks.SUCCESS || 0) + " / 失败 " + (tasks.FAILED || 0)}<//>
            <${antd.Descriptions.Item} label="数据源">${datasource.has_token ? (datasource.last_test_ok === 1 ? "连接正常" : "已配置，未验证") : "未配置"}<//>
            <${antd.Descriptions.Item} label="服务健康">${health.data ? (health.data.db_ok ? "正常" : "数据库异常") : "-"}<//>
            <${antd.Descriptions.Item} label="SQLite 完整性">${health.data ? (health.data.db_ok ? "可读" : "异常") : "-"}<//>
            <${antd.Descriptions.Item} label="SSE 订阅">${health.data ? health.data.sse_subscribers : "-"}<//>
          <//>
        <//>

        <${antd.Alert}
          type="info"
          showIcon
          style=${{ marginBottom: 16 }}
          message="白名单是「存储范围」，不是「回测股票池」"
          description="用最新市值决定历史入库范围会带来幸存者偏差；回测必须按当时市值做 point-in-time 重筛。详见「关于与帮助」。"
        />

        <${antd.Card} size="small" title="最近任务" extra=${html`<${antd.Button} type="link" size="small" onClick=${function () { window.location.hash = "#/tasks"; }}>全部任务<//>`}>
          <${antd.Table}
            size="small"
            rowKey="id"
            loading=${loading}
            dataSource=${s.recent_tasks || []}
            columns=${columns}
            pagination=${false}
            onRow=${function (record) {
              return { onClick: function () { window.location.hash = "#/tasks/" + record.id; } };
            }}
          />
        <//>
      </div>
    `;
  }

  Y.pages.overview = Overview;
})();
