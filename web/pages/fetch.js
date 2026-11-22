(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;
  const React = window.React;
  const dayjs = window.dayjs;

  const MARKETS = [
    { value: "CN_A", label: "A股" },
    { value: "HK", label: "港股" },
    { value: "US", label: "美股" },
  ];

  // ---------------------------------------------------------------------
  // params_schema 渲染器：驱动动态参数表单
  // ---------------------------------------------------------------------
  function FieldControl(props) {
    // Form.Item 会向子组件注入 value/onChange，必须透传给底层控件，
    // 否则值进不了表单 store，必填校验永远不通过。
    const field = props.field;
    const t = field.type;
    const value = props.value;
    const onChange = props.onChange;
    if (t === "number") {
      return html`<${antd.InputNumber}
        style=${{ width: "100%" }}
        min=${field.min}
        step=${field.step || 1}
        placeholder=${field.help || ""}
        value=${value}
        onChange=${onChange}
      />`;
    }
    if (t === "date") {
      return html`<${antd.DatePicker}
        style=${{ width: "100%" }}
        value=${value}
        onChange=${onChange}
      />`;
    }
    if (t === "symbol_list") {
      return html`<${antd.Select}
        mode="tags"
        tokenSeparators=${[",", " "]}
        placeholder=${field.help || "输入 symbol 后回车（如 600519.SH）"}
        open=${false}
        value=${value || []}
        onChange=${onChange}
      />`;
    }
    if (t === "enum") {
      return html`<${antd.Select}
        style=${{ width: "100%" }}
        value=${value}
        onChange=${onChange}
        options=${(field.options || []).map(function (value) {
          return { value: value, label: value };
        })}
      />`;
    }
    return html`<${antd.Input} placeholder=${field.help || ""} value=${value} onChange=${onChange} />`;
  }

  function ParamsForm({ schema, form }) {
    const keys = Object.keys(schema || {});
    if (!keys.length) {
      return html`<${antd.Alert}
        type="info"
        showIcon
        style=${{ marginBottom: 16 }}
        message="该任务类型无需额外参数（市场在第一步已选择）"
      />`;
    }
    return html`<${antd.Form} form=${form} layout="vertical" className="tight">
      ${keys.map(function (key) {
        const field = schema[key];
        return html`<${antd.Form.Item}
          key=${key}
          label=${field.label}
          name=${key}
          rules=${field.required ? [{ required: true, message: "请填写" + field.label }] : []}
          extra=${field.help || undefined}
        >
          <${FieldControl} field=${field} form=${form} />
        <//>`;
      })}
    <//>`;
  }

  // ---------------------------------------------------------------------
  // 页面
  // ---------------------------------------------------------------------
  function TaskSubmit() {
    const [form] = antd.Form.useForm();
    const [market, setMarket] = React.useState("CN_A");
    const [taskType, setTaskType] = React.useState(null);
    const [executor, setExecutor] = React.useState(null);
    const [executors, setExecutors] = React.useState([]);
    const [loading, setLoading] = React.useState(false);
    const [submitting, setSubmitting] = React.useState(false);

    // 加载注册表（按市场）
    React.useEffect(
      function () {
        let cancelled = false;
        setLoading(true);
        Y.api("/executors" + Y.qs({ market: market }))
          .then(function (data) {
            if (cancelled) return;
            const items = (data.items || []).filter(function (item) { return item.implemented; });
            setExecutors(items);
            const types = Array.from(new Set(items.map(function (item) { return item.task_type; })));
            if (types.indexOf(taskType) < 0) {
              setTaskType(types.length ? types[0] : null);
            }
          })
          .catch(Y.notifyError)
          .finally(function () {
            if (!cancelled) setLoading(false);
          });
        return function () { cancelled = true; };
      },
      [market]
    );

    // 按任务类型过滤执行器，默认选第一个
    const candidates = executors.filter(function (item) {
      return !taskType || item.task_type === taskType;
    });
    const currentExecutor = candidates.find(function (item) { return item.name === executor; }) || candidates[0] || null;

    function onTaskTypeChange(value) {
      setTaskType(value);
      setExecutor(null);
      form.resetFields();
    }

    function onExecutorChange(value) {
      setExecutor(value);
      form.resetFields();
    }

    async function submit() {
      if (!currentExecutor) {
        antd.message.warning("当前市场暂无可用执行器（港美股将在后续版本接入）");
        return;
      }
      try {
        const values = await form.validateFields();
        setSubmitting(true);
        // 类型化转换：date → YYYY-MM-DD；其余原样
        const params = {};
        Object.keys(values || {}).forEach(function (key) {
          const field = currentExecutor.params_schema[key];
          const value = values[key];
          if (value === undefined || value === null || value === "") return;
          if (field && field.type === "date") {
            params[key] = dayjs(value).format("YYYY-MM-DD");
          } else if (Array.isArray(value)) {
            params[key] = value.filter(function (v) { return !!v; });
          } else {
            params[key] = value;
          }
        });
        const data = await Y.api("/tasks", {
          method: "POST",
          body: {
            task_type: currentExecutor.task_type,
            executor: currentExecutor.name,
            market: market,
            params: params,
          },
        });
        antd.Modal.success({
          title: "任务已提交",
          content: html`<div>
            <p>任务 ID：<span className="mono">${data.task_id}</span></p>
            <p style=${{ color: "#6b7280" }}>类型 ${currentExecutor.task_type} · 执行器 ${currentExecutor.name} · 市场 ${market}。可在任务中心查看实时进度与日志；白名单为空时日 K 任务会直接报错。</p>
          </div>`,
          okText: "查看任务",
          onOk: function () {
            window.location.hash = "#/tasks/" + data.task_id;
          },
        });
      } catch (error) {
        if (error && error.errorFields) return;
        Y.notifyError(error);
      } finally {
        setSubmitting(false);
      }
    }

    const taskTypes = Array.from(new Set(executors.map(function (item) { return item.task_type; })));

    return html`
      <div>
        <${Y.PageHeader}
          title="任务提交"
          subtitle="市场 → 任务类型 → 执行器 → 参数；全部任务人工触发，无常驻调度"
        />

        <${antd.Alert}
          style=${{ marginBottom: 16 }}
          type="info"
          showIcon
          message="提交前请确认白名单已就绪"
          description="日 K 增量/区间任务按白名单过滤；白名单为空时任务直接拒绝。元数据 / 市值指标需先跑 meta.sync_all，白名单规则读表更新。"
        />

        ${market === "CN_A" && taskType && taskType.indexOf("meta.") === 0
          ? html`<${antd.Alert}
              style=${{ marginBottom: 16 }}
              type="info"
              showIcon
              message="A 股元数据只收录当前上市股票"
              description="meta 任务通过 stock_basic 只拉取一次 list_status='L'（当前上市），不含退市/暂停上市：低积分账号该接口常被限到 1 次/小时，L/D/P 分段拉取无法在单个任务内完成；退市/暂停上市待「分段抓取+断点落库」机制完善后再放开。"
            />`
          : null}

        <${antd.Card}>
          <${antd.Space} direction="vertical" size=${16} style=${{ width: "100%" }}>
            <div>
              <div className="empty-note" style=${{ marginBottom: 8 }}>1. 选择市场</div>
              <${antd.Select}
                style=${{ width: 200 }}
                value=${market}
                onChange=${function (value) { setMarket(value); setTaskType(null); setExecutor(null); }}
                options=${MARKETS}
              />
            </div>

            <div>
              <div className="empty-note" style=${{ marginBottom: 8 }}>2. 选择任务类型</div>
              <${antd.Select}
                style=${{ width: 280 }}
                value=${taskType || undefined}
                onChange=${onTaskTypeChange}
                loading=${loading}
                options=${taskTypes.map(function (type) {
                  const item = executors.find(function (e) { return e.task_type === type; });
                  return { value: type, label: (item && item.task_type_label) || type };
                })}
                placeholder="加载执行器注册表…"
              />
            </div>

            <div>
              <div className="empty-note" style=${{ marginBottom: 8 }}>3. 选择执行器（数据源）</div>
              <${antd.Select}
                style=${{ width: 320 }}
                value=${currentExecutor ? currentExecutor.name : undefined}
                onChange=${onExecutorChange}
                options=${candidates.map(function (item) {
                  return { value: item.name, label: item.title + "（" + item.source + "）" };
                })}
                placeholder="无可用执行器"
              />
            </div>

            <div>
              <div className="empty-note" style=${{ marginBottom: 8 }}>4. 任务参数</div>
              ${currentExecutor
                ? html`<${ParamsForm} schema=${currentExecutor.params_schema} form=${form} />`
                : html`<${antd.Alert} type="warning" showIcon message="该市场暂无已实现的执行器（港美股将在后续版本接入）" />`}
            </div>

            <${antd.Space}>
              <${antd.Button}
                type="primary"
                loading=${submitting}
                disabled=${!currentExecutor}
                onClick=${submit}
              >
                提交任务
              <//>
              <${antd.Button} onClick=${function () { form.resetFields(); }}>重置<//>
            <//>
          <//>
        <//>
      </div>
    `;
  }

  Y.pages.data_fetch = TaskSubmit;
})();
