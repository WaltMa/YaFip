(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;
  const dayjs = window.dayjs;

  function Explore() {
    const [market, setMarket] = React.useState("CN_A");
    const [options, setOptions] = React.useState([]);
    const [tsCode, setTsCode] = React.useState(null);
    const [range, setRange] = React.useState([dayjs().subtract(1, "year"), dayjs()]);
    const [priceType, setPriceType] = React.useState("raw");
    const [rows, setRows] = React.useState([]);
    const [loading, setLoading] = React.useState(false);

    async function search(keyword) {
      if (!keyword) return;
      try {
        const data = await Y.api(
          "/data/instruments" + Y.qs({ market: market, page_size: 20, keyword: keyword })
        );
        setOptions(
          (data.items || []).map(function (item) {
            return { value: item.symbol, label: item.market + " " + item.symbol + " " + (item.name || "") };
          })
        );
      } catch (error) {
        /* 搜索失败静默 */
      }
    }

    React.useEffect(
      function () {
        if (!tsCode) {
          setRows([]);
          return;
        }
        const dates = Y.fmt.range(range);
        setLoading(true);
        Y.api(
          "/data/daily" +
            Y.qs({
              market: market,
              symbol: tsCode,
              start: dates.start,
              end: dates.end,
              price_type: priceType,
              limit: 5000,
            })
        )
          .then(function (data) {
            setRows(data.items || []);
          })
          .catch(Y.notifyError)
          .finally(function () {
            setLoading(false);
          });
      },
      [market, tsCode, range, priceType]
    );

    function num(value) {
      if (value === null || value === undefined) return "-";
      return Number(value).toFixed(2);
    }

    const columns = [
      { title: "日期", dataIndex: "trade_date", width: 110 },
      { title: "开", dataIndex: "open", render: num },
      { title: "高", dataIndex: "high", render: num },
      { title: "低", dataIndex: "low", render: num },
      { title: "收", dataIndex: "close", render: num },
      { title: "前收", dataIndex: "pre_close", render: num },
      { title: "涨跌", dataIndex: "change", render: num },
      { title: "涨跌幅%", dataIndex: "pct_chg", render: num },
      { title: "成交量(手)", dataIndex: "vol", render: Y.fmt.num },
      { title: "成交额(千元)", dataIndex: "amount", render: Y.fmt.num },
      { title: "复权因子", dataIndex: "adj_factor", render: (v) => (v === null || v === undefined ? "-" : Number(v).toFixed(4)) },
    ];

    return html`
      <div>
        <${Y.PageHeader}
          title="数据浏览"
          subtitle="原始 / 前复权 / 后复权三种口径；复权价查询时计算，前复权按当前最新因子"
        />
        <${antd.Card} style=${{ marginBottom: 16 }}>
          <${antd.Space} wrap>
            <${antd.Select}
              style=${{ width: 110 }}
              value=${market}
              onChange=${function (value) { setMarket(value); setTsCode(null); }}
              options=${[
                { value: "CN_A", label: "A股" },
                { value: "HK", label: "港股" },
                { value: "US", label: "美股" },
              ]}
            />
            <${antd.Select}
              showSearch
              value=${tsCode}
              placeholder="输入代码或名称搜索，如 600519"
              style=${{ width: 320 }}
              filterOption=${false}
              onSearch=${search}
              onChange=${setTsCode}
              options=${options}
              notFoundContent="输入关键字搜索"
            />
            <${antd.DatePicker.RangePicker} value=${range} onChange=${setRange} />
            <${antd.Segmented}
              value=${priceType}
              onChange=${setPriceType}
              options=${[
                { label: "原始", value: "raw" },
                { label: "前复权", value: "qfq" },
                { label: "后复权", value: "hfq" },
              ]}
            />
          <//>
        <//>

        <${antd.Alert}
          style=${{ marginBottom: 16 }}
          type="warning"
          showIcon
          message="前复权价格会随新的除权除息变化"
          description="前复权 = 原始价 × 当日因子 ÷ 当前最新因子，因此每次新的分红/送股都会改变全历史前复权价；后复权价不随未来事件变化，适合回测。复权价均按需查询时计算。"
        />

        <${antd.Card}>
          ${tsCode
            ? html`<${antd.Table}
                rowKey="trade_date"
                size="small"
                loading=${loading}
                dataSource=${rows}
                columns=${columns}
                pagination=${{ pageSize: 50, showSizeChanger: true }}
                scroll=${{ x: 1100 }}
              />`
            : html`<${Y.EmptyNote}>请选择一只股票<//>`}
        <//>
      </div>
    `;
  }

  Y.pages.data_explore = Explore;
})();
