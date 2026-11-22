(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;

  const MARKETS = [
    { value: "CN_A", label: "A股" },
    { value: "HK", label: "港股" },
    { value: "US", label: "美股" },
  ];

  function Whitelist() {
    const [market, setMarket] = React.useState("CN_A");
    const summary = Y.useApi(
      function () { return Y.api("/whitelist/config" + Y.qs({ market: market })); },
      [market],
      15000
    );
    const [ruleForm] = antd.Form.useForm();
    const [memberForm] = antd.Form.useForm();
    const [saving, setSaving] = React.useState(false);
    const [running, setRunning] = React.useState(false);

    const [page, setPage] = React.useState(1);
    const [keyword, setKeyword] = React.useState("");
    const [memberData, setMemberData] = React.useState({ items: [], total: 0 });
    const [memberLoading, setMemberLoading] = React.useState(false);
    const [memberModal, setMemberModal] = React.useState(null);
    const [memberSaving, setMemberSaving] = React.useState(false);

    const cfg = summary.data || {};

    React.useEffect(
      function () {
        if (!summary.data) return;
        ruleForm.setFieldsValue({ value: summary.data.threshold_yi });
      },
      [summary.data]
    );

    async function loadMembers(nextPage, nextKeyword) {
      setMemberLoading(true);
      try {
        const data = await Y.api(
          "/whitelist/members" +
            Y.qs({
              market: market,
              page_number: nextPage,
              page_size: 20,
              keyword: nextKeyword || "",
            })
        );
        setMemberData(data);
      } catch (error) {
        Y.notifyError(error);
      } finally {
        setMemberLoading(false);
      }
    }

    React.useEffect(
      function () {
        setPage(1);
        loadMembers(1, "");
      },
      [market]
    );

    async function saveRule() {
      try {
        const values = await ruleForm.validateFields();
        setSaving(true);
        // 规则键沿用 threshold：按亿元输入，内部转万元
        const threshold_wan = Number(values.value) * 10000;
        await Y.api("/whitelist/config", {
          method: "PUT",
          body: { threshold_wan: threshold_wan },
        });
        antd.message.success("白名单门槛已保存");
        summary.reload();
      } catch (error) {
        if (error && error.errorFields) return;
        Y.notifyError(error);
      } finally {
        setSaving(false);
      }
    }

    function updateWhitelist() {
      if (market !== "CN_A") {
        antd.message.info("港美股无市值数据源，白名单需手工维护");
        return;
      }
      const threshold_wan = Number((ruleForm.getFieldValue && ruleForm.getFieldValue("value")) || 50) * 10000;
      Y.confirm(
        "更新白名单",
        "将读取「" + market + "」最新市值指标（total_mv），按门槛 > " + Y.fmt.num(threshold_wan) +
          " 万元只进不出更新明细；不删除任何条目。确认运行？",
        async function () {
          try {
            setRunning(true);
            const data = await Y.api("/tasks", {
              method: "POST",
              body: {
                task_type: "whitelist.update",
                executor: "tushare.whitelist.update",
                market: market,
                params: { metric: "total_mv", op: ">", value: threshold_wan },
              },
            });
            antd.Modal.success({
              title: "白名单任务已提交",
              content: "任务 ID：" + data.task_id + "。规则只新增/更新，不删除条目；删除只能人工。",
              okText: "查看任务",
              onOk: function () {
                window.location.hash = "#/tasks/" + data.task_id;
              },
            });
            setTimeout(function () {
              summary.reload();
              loadMembers(page, keyword);
            }, 4000);
          } catch (error) {
            Y.notifyError(error);
          } finally {
            setRunning(false);
          }
        }
      );
    }

    function search() {
      setPage(1);
      loadMembers(1, keyword);
    }

    function resetSearch() {
      setKeyword("");
      setPage(1);
      loadMembers(1, "");
    }

    function openAddMember() {
      setMemberModal({ mode: "add" });
      memberForm.resetFields();
      memberForm.setFieldsValue({ market: market, symbol: "", reason: "" });
    }

    function openEditMember(row) {
      setMemberModal({ mode: "edit", row: row });
      memberForm.setFieldsValue({ market: row.market, symbol: row.symbol, reason: row.reason || "" });
    }

    async function saveMember() {
      if (!memberModal) return;
      try {
        const values = await memberForm.validateFields();
        setMemberSaving(true);
        if (memberModal.mode === "edit") {
          await Y.api(
            "/whitelist/members/" + encodeURIComponent(memberModal.row.symbol) + Y.qs({ market: values.market }),
            { method: "PUT", body: { market: values.market, reason: values.reason || "" } }
          );
        } else {
          await Y.api("/whitelist/members", {
            method: "POST",
            body: { market: values.market, symbol: values.symbol, reason: values.reason || "" },
          });
        }
        antd.message.success(memberModal.mode === "edit" ? "标的已更新" : "标的已新增");
        setMemberModal(null);
        summary.reload();
        loadMembers(memberModal.mode === "add" ? 1 : page, keyword);
      } catch (error) {
        if (error && error.errorFields) return;
        Y.notifyError(error);
      } finally {
        setMemberSaving(false);
      }
    }

    function removeMember(row) {
      Y.confirm(
        "删除标的（只进不出）",
        "将从「" + row.market + "」白名单删除 " + row.symbol + "（" + (row.name || "-") +
          "），并写入审计日志。规则任务不会重新加入它；如需再入名单请手工新增。确认删除？",
        async function () {
          try {
            await Y.api(
              "/whitelist/members/" + encodeURIComponent(row.symbol) + Y.qs({ market: row.market }),
              { method: "DELETE" }
            );
            antd.message.success("已删除（已记审计）");
            const nextPage = (memberData.items || []).length === 1 && page > 1 ? page - 1 : page;
            setPage(nextPage);
            summary.reload();
            loadMembers(nextPage, keyword);
          } catch (error) {
            Y.notifyError(error);
          }
        }
      );
    }

    const columns = [
      { title: "市场", dataIndex: "market", width: 70 },
      { title: "标的", dataIndex: "symbol", width: 130, render: (v) => html`<span className="mono">${v}</span>` },
      { title: "名称", dataIndex: "name", width: 140, render: (v) => v || "-" },
      { title: "规则", dataIndex: "rule_key", width: 170, render: (v) => v || "手工" },
      { title: "市值（亿元）", dataIndex: "metric_yi", width: 120, render: (v) => (v === null || v === undefined ? "-" : Number(v).toFixed(2)) },
      { title: "市值日期", dataIndex: "as_of_date", width: 110, render: (v) => v || "-" },
      { title: "来源", dataIndex: "source", width: 80, render: (v) => html`<${Y.SourceTag} source=${v} />` },
      { title: "说明", dataIndex: "reason", render: (v) => v || "-" },
      {
        title: "操作",
        width: 110,
        render: function (_value, row) {
          return html`<${antd.Space} size="small">
            <${antd.Button} size="small" type="link" onClick=${function () { openEditMember(row); }}>编辑<//>
            <${antd.Button} size="small" type="link" danger onClick=${function () { removeMember(row); }}>删除<//>
          <//>`;
        },
      },
    ];

    return html`
      <div>
        <${Y.PageHeader}
          title="白名单管理"
          subtitle="日 K 存储范围（只进不出）；规则任务按市值指标读表更新，删除只能人工"
          extra=${html`<${antd.Space}>
            <${antd.Select}
              style=${{ width: 120 }}
              value=${market}
              onChange=${function (value) { setMarket(value); }}
              options=${MARKETS}
            />
            <${antd.Button}
              type="primary"
              loading=${running}
              disabled=${market !== "CN_A"}
              title=${market !== "CN_A" ? "港美股无市值数据源，白名单需手工维护" : ""}
              onClick=${updateWhitelist}
            >
              更新白名单
            <//>
            <${antd.Button}
              onClick=${function () {
                summary.reload();
                loadMembers(page, keyword);
              }}
            >
              刷新
            <//>
          <//>`}
        />

        <${antd.Alert}
          type="warning"
          showIcon
          style=${{ marginBottom: 16 }}
          message="白名单只进不出：规则任务仅新增/更新，不删除条目；删除只有人工一条路径并记审计"
          description="用当前市值决定历史入库范围会带来幸存者偏差；白名单仅用于存储范围，未来回测必须按当时市值做 point-in-time 重筛。"
        />

        <div className="stat-grid">
          <${antd.Card} size="small">
            <${antd.Statistic} title="明细 / 标的" value=${Y.fmt.num(cfg.members || 0)} />
          <//>
          <${antd.Card} size="small">
            <${antd.Statistic} title="门槛" value=${cfg.threshold_yi !== undefined ? Number(cfg.threshold_yi).toFixed(2) + " 亿" : "-"} />
          <//>
          <${antd.Card} size="small">
            <${antd.Statistic} title="市值日期" value=${cfg.as_of_date || "-"} valueStyle=${{ fontSize: 16 }} />
          <//>
          <${antd.Card} size="small">
            <${antd.Statistic} title="最近计算" value=${cfg.computed_at ? Y.fmt.time(cfg.computed_at) : "-"} valueStyle=${{ fontSize: 14 }} />
          <//>
        </div>

        <${antd.Card} title="规则与门槛" style=${{ marginBottom: 16 }}>
          <${antd.Form} form=${ruleForm} layout="inline" className="tight">
            <${antd.Form.Item} label="口径">
              <${antd.Tag} color="blue">${cfg.metric_label || "最新交易日总市值（万元）"}<//>
            <//>
            <${antd.Form.Item} label="门槛（亿元）" name="value" rules=${[{ required: true, message: "请输入门槛" }]}>
              <${antd.InputNumber} min=${0.01} step=${10} style=${{ width: 140 }} />
            <//>
            <${antd.Button} type="primary" loading=${saving} onClick=${saveRule}>保存门槛<//>
          <//>
          <div className="empty-note">市值来自 instrument_metric（先运行「任务提交 → 元数据全量同步」）；无指标时白名单任务会提示先同步元数据。更新白名单前请先保存门槛。</div>
        <//>

        <${antd.Card} title="白名单明细">
          <${antd.Space} style=${{ marginBottom: 12 }} wrap>
            <${antd.Input}
              placeholder="标的 / 名称"
              value=${keyword}
              onChange=${function (event) { setKeyword(event.target.value); }}
              onPressEnter=${search}
              allowClear
              style=${{ width: 220 }}
            />
            <${antd.Button} onClick=${search}>查询<//>
            <${antd.Button} onClick=${resetSearch}>重置<//>
            <${antd.Button} type="primary" onClick=${openAddMember}>新增标的<//>
          <//>
          <${antd.Table}
            rowKey=${function (row) { return row.market + ":" + row.symbol; }}
            size="middle"
            loading=${memberLoading}
            dataSource=${memberData.items || []}
            columns=${columns}
            scroll=${{ x: 1000 }}
            pagination=${{
              current: page,
              pageSize: 20,
              total: memberData.total || 0,
              onChange: function (nextPage) {
                setPage(nextPage);
                loadMembers(nextPage, keyword);
              },
            }}
          />
        <//>

        <${antd.Modal}
          title=${memberModal ? (memberModal.mode === "edit" ? "编辑标的" : "新增标的") : "标的"}
          open=${!!memberModal}
          onCancel=${function () { setMemberModal(null); }}
          onOk=${saveMember}
          confirmLoading=${memberSaving}
          okText="保存"
          cancelText="取消"
          width=${520}
        >
          <${antd.Form} form=${memberForm} layout="vertical" className="tight">
            <${antd.Form.Item} label="市场" name="market" rules=${[{ required: true, message: "请选择市场" }]}>
              <${antd.Select} options=${MARKETS} disabled=${!!memberModal && memberModal.mode === "edit"} />
            <//>
            <${antd.Form.Item}
              label="标的"
              name="symbol"
              rules=${[{ required: true, message: "请输入标的" }]}
              extra="A股 600519.SH / 港股 00700.HK / 美股 AAPL"
            >
              <${antd.Input} placeholder="600519.SH" disabled=${!!memberModal && memberModal.mode === "edit"} />
            <//>
            <${antd.Form.Item} label="说明" name="reason">
              <${antd.Input} placeholder="可选，例如：手动维护" />
            <//>
          <//>
        <//>
      </div>
    `;
  }

  Y.pages.data_whitelist = Whitelist;
})();
