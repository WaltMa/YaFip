(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;

  const FIELDS = [
    { key: "server.port", label: "服务端口", type: "number", min: 1, max: 65535, extra: "默认 8203；修改后需重启服务生效，环境变量 YAFIP_PORT 优先" },
    { key: "server.stop_wait_sec", label: "停止等待上限（秒）", type: "number", min: 0, max: 86400, extra: "平滑停止（/system/stop）：0 = 一直等待运行中的任务结束，大于 0 超时强制退出；进程退出（Ctrl+C/重启）最多等待 5 秒兜底，超时未完成任务标记失败" },
    { key: "task.max_concurrency", label: "并发任务数", type: "number", min: 1, max: 64, extra: "全局并发上限（信号量）；修改后需重启服务生效" },
    { key: "task.cpu_ratio", label: "单任务 CPU 占空比", type: "number", min: 0.01, max: 1, extra: "每工作单元按 CPU 计量让出份额（IO 等待不计入）" },
    { key: "task.cpu_budget_ratio", label: "全局 CPU 预算占比", type: "number", min: 0.01, max: 1, extra: "并发任务分摊预算的上限比例" },
    { key: "fetch.rpm", label: "tushare 数据接口限频（次/分钟）", type: "number", min: 1, max: 100000, extra: "daily / adj_factor / daily_basic 用；数据源配置里的 rpm_limit 优先" },
    { key: "fetch.meta_rpm", label: "元数据接口限频（次/分钟）", type: "number", min: 1, max: 100000, extra: "trade_cal / stock_basic 用；部分账号仅 1 次/分钟，超过会自动等待" },
    { key: "fetch.timeout_sec", label: "单次请求超时（秒）", type: "number", min: 1, max: 600 },
    { key: "fetch.retry_max", label: "失败重试次数", type: "number", min: 0, max: 20 },
    { key: "data.start_date", label: "历史起点", type: "text", pattern: "^\\d{4}-\\d{2}-\\d{2}$", patternMessage: "格式必须为 YYYY-MM-DD", extra: "格式 YYYY-MM-DD，全量抓取从这里开始" },
    { key: "task.retention_days", label: "任务记录保留天数", type: "number", min: 1, max: 3650 },
    { key: "sse.heartbeat_sec", label: "SSE 心跳（秒）", type: "number", min: 1, max: 3600, extra: "下限 1 秒；写 0 会让心跳立即超时、SSE 无限空转" },
  ];

  function Settings() {
    const [form] = antd.Form.useForm();
    const config = Y.useApi("/config");
    const health = Y.useApi("/system/health", [], 15000);
    const [saving, setSaving] = React.useState(false);
    const [busy, setBusy] = React.useState("");

    React.useEffect(
      function () {
        if (!config.data) return;
        const values = {};
        FIELDS.forEach(function (field) {
          const raw = config.data[field.key];
          if (field.type === "number") values[field.key] = Number(raw);
          else if (field.type === "switch") values[field.key] = raw === "1";
          else values[field.key] = raw;
        });
        form.setFieldsValue(values);
      },
      [config.data]
    );

    async function save() {
      try {
        const values = await form.validateFields();
        const payload = {};
        FIELDS.forEach(function (field) {
          if (field.type === "switch") payload[field.key] = values[field.key] ? "1" : "0";
          else payload[field.key] = String(values[field.key]);
        });
        setSaving(true);
        await Y.api("/config", { method: "PUT", body: { values: payload } });
        antd.message.success("配置已保存并热加载");
        config.reload();
        health.reload();
      } catch (error) {
        if (error && error.errorFields) return;
        Y.notifyError(error);
      } finally {
        setSaving(false);
      }
    }

    async function run(name, path, confirmText) {
      Y.confirm(name, confirmText, async function () {
        try {
          setBusy(name);
          const data = await Y.api(path, { method: "POST" });
          antd.message.success(name + "：" + JSON.stringify(data));
          health.reload();
        } catch (error) {
          Y.notifyError(error);
        } finally {
          setBusy("");
        }
      });
    }

    return html`
      <div>
        <${Y.PageHeader} title="系统设置" subtitle="所有运行时配置存 SQLite，保存即生效（端口除外）" />

        <${antd.Row} gutter=${16}>
          <${antd.Col} xs=${24} lg=${15}>
            <${antd.Card} title="运行配置" loading=${config.loading}>
              <${antd.Form} form=${form} layout="vertical" className="tight">
                ${FIELDS.map(function (field) {
                  const rules = field.type === "number"
                    ? [{ type: "number", required: true, min: field.min, max: field.max, message: field.min !== undefined ? "请输入 " + field.min + " ~ " + field.max + " 之间的整数" : "请输入整数" }]
                    : field.pattern
                    ? [{ pattern: new RegExp(field.pattern), message: field.patternMessage || "格式不正确" }]
                    : [];
                  return html`<${antd.Form.Item} key=${field.key} label=${field.label} name=${field.key} extra=${field.extra} rules=${rules}>
                    ${field.type === "number"
                      ? html`<${antd.InputNumber} style=${{ width: "100%" }} min=${field.min} max=${field.max} />`
                      : field.type === "switch"
                      ? html`<${antd.Switch} />`
                      : html`<${antd.Input} />`}
                  <//>`;
                })}
                <${antd.Button} type="primary" loading=${saving} onClick=${save}>保存配置<//>
              <//>
            <//>
          <//>

          <${antd.Col} xs=${24} lg=${9}>
            <${antd.Card} title="运行状态" style=${{ marginBottom: 16 }}>
              <${antd.Descriptions} column=${1} size="small">
                <${antd.Descriptions.Item} label="版本">${health.data ? health.data.version : "-"}<//>
                <${antd.Descriptions.Item} label="数据库">${health.data ? (health.data.db_ok ? "正常" : "异常") : "-"}<//>
                <${antd.Descriptions.Item} label="库体积">${health.data ? Y.fmt.bytes(health.data.db_bytes) : "-"}<//>
                <${antd.Descriptions.Item} label="SSE 订阅">${health.data ? health.data.sse_subscribers : "-"}<//>
                <${antd.Descriptions.Item} label="白名单">
                  ${health.data && health.data.whitelist
                    ? "明细 " + Y.fmt.num(health.data.whitelist.members || 0) + " 只" +
                      (health.data.whitelist.as_of_date ? " · 市值日期 " + health.data.whitelist.as_of_date : "")
                    : "-"}
                <//>
              <//>
            <//>

            <${antd.Card} title="维护操作">
              <${antd.Space} direction="vertical" style=${{ width: "100%" }}>
                <${antd.Button}
                  block
                  loading=${busy === "清理历史记录"}
                  onClick=${function () {
                    run(
                      "清理历史记录",
                      "/system/cleanup",
                      "将按 task.retention_days 删除终态任务与日志，并清理已处置质量问题和旧白名单 scope，不影响行情数据。"
                    );
                  }}
                >
                  清理历史记录
                <//>
                <${antd.Button}
                  block
                  loading=${busy === "VACUUM"}
                  onClick=${function () {
                    run("VACUUM", "/system/vacuum", "将重建数据库文件回收空间，库较大时可能耗时数十秒。");
                  }}
                >
                  VACUUM 回收空间
                <//>
                <${antd.Button}
                  block
                  danger
                  loading=${busy === "停止服务"}
                  onClick=${function () {
                    run("停止服务", "/system/stop", "将停止接收新任务并等待运行中的任务执行完成后退出。重新启动请运行 start_yafip 启动脚本。");
                  }}
                >
                  停止服务
                <//>
              <//>
              <div className="empty-note">停止后需通过 start_yafip 启动脚本重新启动服务。</div>
            <//>
          <//>
        <//>
      </div>
    `;
  }

  Y.pages.system_settings = Settings;
})();
