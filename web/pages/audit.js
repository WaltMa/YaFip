(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;

  function Audit() {
    const [page, setPage] = React.useState(1);
    const [pageSize, setPageSize] = React.useState(50);
    const [detail, setDetail] = React.useState(null);
    const list = Y.useApi(
      function () {
        return Y.api("/system/audit?page_number=" + page + "&page_size=" + pageSize);
      },
      [page, pageSize]
    );

    const columns = [
      { title: "时间", dataIndex: "ts", width: 170, render: Y.fmt.time },
      { title: "操作者", dataIndex: "actor", width: 100 },
      { title: "动作", dataIndex: "action", width: 190, render: (v) => html`<${antd.Tag}>${v}<//>` },
      { title: "对象", dataIndex: "target", width: 220, render: (v) => html`<span className="mono">${v || "-"}</span>` },
      { title: "详情", dataIndex: "detail", ellipsis: true },
    ];

    return html`
      <div>
        <${Y.PageHeader} title="操作日志" subtitle="配置修改、任务杀死/重跑/删除、白名单切换、服务维护均会留痕" />
        <${antd.Card}>
          <${antd.Table}
            rowKey="id"
            size="small"
            loading=${list.loading}
            dataSource=${(list.data && list.data.items) || []}
            columns=${columns}
            onRow=${function (record) {
              return { onClick: function () { setDetail(record); }, className: "clickable-row" };
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
        <${antd.Drawer} width=${520} open=${!!detail} onClose=${function () { setDetail(null); }} title="审计详情">
          ${detail
            ? html`<${antd.Descriptions} column=${1} bordered size="small">
                <${antd.Descriptions.Item} label="ID">${detail.id}<//>
                <${antd.Descriptions.Item} label="时间">${Y.fmt.time(detail.ts)}<//>
                <${antd.Descriptions.Item} label="操作者">${detail.actor}<//>
                <${antd.Descriptions.Item} label="动作">${detail.action}<//>
                <${antd.Descriptions.Item} label="对象">${detail.target || "-"}<//>
                <${antd.Descriptions.Item} label="详情">${html`<pre className="code-block">${detail.detail || "-"}</pre>`}<//>
              <//>`
            : null}
        <//>
      </div>
    `;
  }

  Y.pages.system_audit = Audit;
})();
