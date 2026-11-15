(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;
  const dayjs = window.dayjs;

  function Quality() {
    const [market, setMarket] = React.useState("CN_A");
    const [page, setPage] = React.useState(1);
    const [pageSize, setPageSize] = React.useState(20);
    const [checkType, setCheckType] = React.useState("");
    const [range, setRange] = React.useState([dayjs().subtract(1, "year"), dayjs()]);
    const [scanning, setScanning] = React.useState(false);
    const [repairing, setRepairing] = React.useState(false);

    const issues = Y.useApi(
      function () {
        return Y.api(
          "/data/quality-issues" +
            Y.qs({ market: market, check_type: checkType, resolved: 0, page_number: page, page_size: pageSize })
        );
      },
      [market, checkType, page, pageSize]
    );
    const coverage = Y.useApi(
      function () { return Y.api("/data/coverage" + Y.qs({ market: market, granularity: "year" })); },
      [market]
    );

    async function scan() {
      try {
        setScanning(true);
        const dates = Y.fmt.range(range);
        const data = await Y.api("/data/quality-scan", {
          method: "POST",
          body: { market: market, start: dates.start, end: dates.end },
        });
        antd.message.success("质量扫描任务已提交");
        setTimeout(issues.reload, 1500);
        window.location.hash = "#/tasks/" + data.task_id;
      } catch (error) {
        Y.notifyError(error);
      } finally {
        setScanning(false);
      }
    }

    async function repairAll() {
      const items = (issues.data && issues.data.items) || [];
      const symbols = Array.from(
        new Set(
          items
            .map(function (item) { return item.symbol; })
            .filter(function (symbol) { return !!symbol; })
        )
      );
      if (!symbols.length) {
        antd.message.info("当前页没有可补缺的标的");
        return;
      }
      const dates = Y.fmt.range(range);
      Y.confirm(
        "提交补缺任务",
        "将对 " + symbols.length + " 只标的在 " + dates.start + " ~ " + dates.end + " 区间重抓（幂等覆盖，不动水位）。可能包含停牌日，补缺后仍无数据属正常。",
        async function () {
          try {
            setRepairing(true);
            const data = await Y.api("/tasks", {
              method: "POST",
              body: {
                task_type: "bars.range",
                executor: "tushare.bars.range",
                market: market,
                params: { start: dates.start, end: dates.end, symbols: symbols },
              },
            });
            antd.message.success("补缺任务已提交");
            window.location.hash = "#/tasks/" + data.task_id;
          } catch (error) {
            Y.notifyError(error);
          } finally {
            setRepairing(false);
          }
        }
      );
    }

    async function resolve(id) {
      try {
        await Y.api("/data/quality-issues/resolve", { method: "POST", body: { ids: [id] } });
        antd.message.success("已标记为已解决");
        issues.reload();
      } catch (error) {
        Y.notifyError(error);
      }
    }

    const issueColumns = [
      { title: "市场", dataIndex: "market", width: 70, render: (v) => v || "-" },
      { title: "类型", dataIndex: "check_type", width: 150 },
      { title: "标的", dataIndex: "symbol", width: 120, render: (v) => v || "-" },
      { title: "日期", dataIndex: "trade_date", width: 120, render: (v) => v || "-" },
      { title: "详情", dataIndex: "detail" },
      { title: "发现时间", dataIndex: "detected_at", width: 160, render: Y.fmt.time },
      {
        title: "操作",
        width: 100,
        render: (_, r) => html`<${antd.Button} size="small" type="link" onClick=${function () { resolve(r.id); }}>已解决<//>`,
      },
    ];

    const coverageColumns = [
      { title: "年份", dataIndex: "bucket", width: 100 },
      { title: "行数", dataIndex: "rows", render: Y.fmt.num },
      { title: "股票数", dataIndex: "stocks", render: Y.fmt.num },
      { title: "首个交易日", dataIndex: "first_date" },
      { title: "最后交易日", dataIndex: "last_date" },
    ];

    return html`
      <div>
        <${Y.PageHeader}
          title="覆盖与质量"
          subtitle="异常值、复权因子缺失、缺失交易日、单日行数异常（按市场）"
          extra=${html`<${antd.Space}>
            <${antd.Select}
              style=${{ width: 110 }}
              value=${market}
              onChange=${function (value) { setMarket(value); setPage(1); }}
              options=${[
                { value: "CN_A", label: "A股" },
                { value: "HK", label: "港股" },
                { value: "US", label: "美股" },
              ]}
            />
            <${antd.DatePicker.RangePicker} value=${range} onChange=${setRange} />
            <${antd.Button} type="primary" loading=${scanning} onClick=${scan}>扫描<//>
            <${antd.Button} loading=${repairing} onClick=${repairAll}>一键补缺（当前页）<//>
          <//>`}
        />

        <${antd.Alert}
          style=${{ marginBottom: 16 }}
          type="info"
          showIcon
          message="缺失交易日可能包含停牌"
          description="本期默认不抓 suspend_d，停牌日没有日 K 行，因此会被计入缺失。判定区间已按 list_date / delist_date 收敛，不会把上市前/退市后算作缺失。"
        />

        <${antd.Card} title="年度覆盖" size="small" style=${{ marginBottom: 16 }} loading=${coverage.loading}>
          <${antd.Table}
            rowKey="bucket"
            size="small"
            dataSource=${coverage.data || []}
            columns=${coverageColumns}
            pagination=${{ pageSize: 10 }}
          />
        <//>

        <${antd.Card}
          title="未解决质量问题"
          size="small"
          extra=${html`<${antd.Select}
            allowClear
            placeholder="全部类型"
            style=${{ width: 200 }}
            value=${checkType || undefined}
            onChange=${function (value) { setCheckType(value || ""); setPage(1); }}
            options=${[
              { value: "invalid_ohlc", label: "异常行情" },
              { value: "adj_missing", label: "复权因子缺失" },
              { value: "missing_days", label: "缺失交易日" },
              { value: "row_count_anomaly", label: "单日行数异常" },
            ]}
          />`}
        >
          <${antd.Table}
            rowKey="id"
            size="small"
            loading=${issues.loading}
            dataSource=${(issues.data && issues.data.items) || []}
            columns=${issueColumns}
            pagination=${{
              current: page,
              pageSize: pageSize,
              total: (issues.data && issues.data.total) || 0,
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

  Y.pages.data_quality = Quality;
})();
