(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;

  function SourceEditor(props) {
    const source = props.source;
    const [form] = antd.Form.useForm();
    const [saving, setSaving] = React.useState(false);

    React.useEffect(
      function () {
        if (!source) return;
        form.setFieldsValue({
          token: "",
          api_url: source.api_url || "",
          rpm_limit: source.rpm_limit || 0,
          enabled: source.enabled,
        });
      },
      [source]
    );

    async function save() {
      try {
        const values = await form.validateFields();
        setSaving(true);
        await Y.api("/datasources/" + source.name, {
          method: "PUT",
          body: {
            token: values.token || null,
            api_url: values.api_url || "",
            rpm_limit: values.rpm_limit || 0,
            enabled: values.enabled,
          },
        });
        antd.message.success("配置已保存");
        await props.onSaved();
      } catch (error) {
        if (error && error.errorFields) return;
        Y.notifyError(error);
      } finally {
        setSaving(false);
      }
    }

    return html`
      <${antd.Modal}
        title=${"编辑数据源 · " + (source ? source.title : "")}
        open=${!!source}
        onCancel=${props.onClose}
        onOk=${save}
        confirmLoading=${saving}
        okText="保存"
        cancelText="取消"
        width=${560}
      >
        ${source
          ? html`<${antd.Form} form=${form} layout="vertical" className="tight">
              <${antd.Alert}
                type="info"
                showIcon
                style=${{ marginBottom: 16 }}
                message=${source.description}
                description=${source.homepage
                  ? html`<a href=${source.homepage} target="_blank" rel="noreferrer">打开官网 / 获取 token</a>`
                  : null}
              />
              <${antd.Form.Item}
                label="Token"
                name="token"
                rules=${source.requires_token ? (source.has_token ? [] : [{ required: true, message: "请输入 token" }]) : []}
                extra=${!source.requires_token
                  ? "该数据源免 token"
                  : source.has_token
                  ? "当前：" + source.token_masked + "（留空表示不修改）"
                  : "必填；仅保存在本地 SQLite"}
              >
                <${antd.Input.Password} placeholder="粘贴 token" autoComplete="off" />
              <//>
              <${antd.Form.Item}
                label="API 地址（可选）"
                name="api_url"
                extra="默认官方地址；仅在使用自定义网关时填写"
              >
                <${antd.Input} placeholder="https://api.tushare.pro" />
              <//>
              <${antd.Form.Item}
                label="限频（次/分钟）"
                name="rpm_limit"
                extra="0 = 跟随系统设置 fetch.rpm（默认 120）"
              >
                <${antd.InputNumber} min=${0} max=${5000} style=${{ width: "100%" }} />
              <//>
              <${antd.Form.Item} label="启用" name="enabled" valuePropName="checked">
                <${antd.Switch} />
              <//>
            <//>`
          : null}
      <//>`;
  }

  function DataSource() {
    const list = Y.useApi("/datasources");
    const [editing, setEditing] = React.useState(null);
    const [testingName, setTestingName] = React.useState("");
    const [testResult, setTestResult] = React.useState(null);

    const items = (list.data && list.data.items) || [];

    async function runTest(name) {
      try {
        setTestingName(name);
        setTestResult(null);
        const result = await Y.api("/datasources/" + name + "/test", { method: "POST" });
        setTestResult({ name: name, ...result });
        if (result.ok) antd.message.success("连接测试通过");
        else antd.message.warning("连接测试未完全通过，请查看各步骤结果");
        list.reload();
      } catch (error) {
        Y.notifyError(error);
      } finally {
        setTestingName("");
      }
    }

    async function onSaved() {
      setEditing(null);
      await list.reload();
    }

    const columns = [
      {
        title: "数据源",
        dataIndex: "title",
        render: function (value, row) {
          return html`<div>
            <b>${value}</b>
            <div className="mono" style=${{ color: "#6b7280", fontSize: 12 }}>${row.name}</div>
          </div>`;
        },
      },
      {
        title: "状态",
        dataIndex: "enabled",
        width: 90,
        render: function (value) {
          return html`<${antd.Tag} color=${value ? "green" : "default"}>${value ? "启用" : "停用"}<//>`;
        },
      },
      {
        title: "Token",
        dataIndex: "has_token",
        width: 180,
        render: function (value, row) {
          return value
            ? html`<span className="mono">${row.token_masked}<//>`
            : html`<${antd.Tag} color="red">未配置<//>`;
        },
      },
      {
        title: "API 地址",
        dataIndex: "api_url",
        render: function (value) {
          return value ? html`<span className="mono">${value}<//>` : "默认";
        },
      },
      {
        title: "限频",
        dataIndex: "rpm_limit",
        width: 110,
        render: function (value) {
          return value ? value + " 次/分" : "跟随全局";
        },
      },
      {
        title: "上次测试",
        dataIndex: "last_test_at",
        width: 220,
        render: function (value, row) {
          if (!value) return "-";
          return html`<div>${Y.fmt.time(value)}</div>
            <div style=${{ fontSize: 12 }}>
              <${antd.Tag} color=${row.last_test_ok === 1 ? "green" : "red"}>
                ${row.last_test_ok === 1 ? "通过" : "失败"}
              <//>
              <span style=${{ color: "#6b7280" }}>${row.last_test_msg || ""}</span>
            </div>`;
        },
      },
      {
        title: "操作",
        width: 190,
        render: function (_value, row) {
          return html`<${antd.Space}>
            <${antd.Button} size="small" onClick=${function () { setEditing(row); }}>编辑<//>
            <${antd.Button}
              size="small"
              loading=${testingName === row.name}
              onClick=${function () { runTest(row.name); }}
            >
              测试连接
            <//>
          <//>`;
        },
      },
    ];

    return html`
      <div>
        <${Y.PageHeader}
          title="数据源配置"
          subtitle="维护每个数据源的连接参数与启停；token 仅存本地 SQLite，接口只返回脱敏值"
        />

        <${antd.Card}>
          <${antd.Table}
            rowKey="name"
            size="middle"
            loading=${list.loading}
            columns=${columns}
            dataSource=${items}
            pagination=${false}
          />
          <div className="empty-note">当前已接入 tushare pro；新增数据源需在后台注册后出现在此列表。</div>
        <//>

        <${SourceEditor} source=${editing} onClose=${function () { setEditing(null); }} onSaved=${onSaved} />

        <${antd.Modal}
          title=${"连接测试 · " + (testResult ? testResult.name : "")}
          open=${!!testResult}
          onCancel=${function () { setTestResult(null); }}
          footer=${html`<${antd.Button} type="primary" onClick=${function () { setTestResult(null); }}>关闭<//>`}
          width=${620}
        >
          ${testResult
            ? html`<${antd.Space} direction="vertical" style=${{ width: "100%" }}>
                <${antd.Alert}
                  type=${testResult.ok ? "success" : "warning"}
                  showIcon
                  message=${testResult.ok ? "全部通过" : "部分步骤失败"}
                />
                ${(testResult.steps || []).map(function (step) {
                  return html`<div key=${step.name} style=${{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                    <${antd.Tag} color=${step.ok ? "green" : "red"}>${step.ok ? "OK" : "FAIL"}<//>
                    <div>
                      <b>${step.name}</b>
                      <div style=${{ color: "#6b7280", fontSize: 12 }}>${step.message}</div>
                    </div>
                  </div>`;
                })}
              <//>`
            : null}
        <//>
      </div>
    `;
  }

  Y.pages.data_source = DataSource;
})();
