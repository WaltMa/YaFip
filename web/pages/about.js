(function () {
  "use strict";
  const Y = window.YAFIP;
  const html = Y.html;
  const antd = Y.antd;

  function About() {
    return html`
      <div>
        <${Y.PageHeader} title="关于与帮助" subtitle="版本、数据说明、口径与已知边界" />

        <${antd.Row} gutter=${16}>
          <${antd.Col} xs=${24} lg=${12}>
            <${antd.Card} title="YaFip" style=${{ marginBottom: 16 }}>
              <${antd.Descriptions} column=${1} size="small">
                <${antd.Descriptions.Item} label="版本">v${Y.version}<//>
                <${antd.Descriptions.Item} label="定位">本地运行的量化数据底座（A股 / 港股 / 美股）<//>
                <${antd.Descriptions.Item} label="范围">数据底座：多市场日 K + 复权因子 + 白名单 + 任务系统<//>
                <${antd.Descriptions.Item} label="不做">实盘交易 / 券商下单（永久不做）；策略 / 回测为后续规划<//>
                <${antd.Descriptions.Item} label="技术栈">FastAPI · SQLite(WAL) · React 18 + antd 5 UMD · ECharts<//>
                <${antd.Descriptions.Item} label="API 文档">${html`<a href="/docs" target="_blank" rel="noreferrer">/docs</a>`}<//>
              <//>
            <//>

            <${antd.Alert}
              type="warning"
              showIcon
              message="幸存者偏差声明（重要）"
              description="白名单用最新交易日市值决定历史入库范围，会带来幸存者偏差。它只是「存储范围」，未来回测必须按当时市值做 point-in-time 重筛，否则会系统性高估收益。"
              style=${{ marginBottom: 16 }}
            />
          <//>

          <${antd.Col} xs=${24} lg=${12}>
            <${antd.Card} title="口径与约定">
              <${antd.Collapse}
                defaultActiveKey=${["units", "qfq", "whitelist", "kill", "resume"]}
                items=${[
                  {
                    key: "units",
                    label: "单位约定",
                    children: html`<div>
                      <p>日 K：价格元、成交量手、成交额千元；全系统不做单位换算，界面表头标注单位。</p>
                      <p>市值：万元（来自 tushare daily_basic 最新交易日）；50 亿元 = 500000 万元；界面以亿元输入，内部自动 ×10000。</p>
                    </div>`,
                  },
                  {
                    key: "qfq",
                    label: "复权口径",
                    children: html`<div>
                      <p>原始行情与复权因子是权威数据，只落一张原始表；复权价在查询时计算，不落盘。</p>
                      <p>后复权价 = 原始价 × 当日因子，不随未来分红变化；前复权价 = 原始价 × 当日因子 ÷ 当前最新因子，会随每次新的除权除息变化。</p>
                    </div>`,
                  },
                  {
                    key: "whitelist",
                    label: "白名单语义",
                    children: html`<div>
                      <p>口径：最新交易日总市值（total_mv）大于门槛，指标来自 instrument_metric（先运行「元数据全量同步」）。判定参数存 app_config。</p>
                      <p>白名单只进不出：规则任务仅新增/更新（INSERT ON CONFLICT），不删除、不落选；删除只有 Web 人工一条路径并记审计。</p>
                      <p>白名单为空时日 K 任务直接拒绝执行；元数据缺失时白名单任务会提示先同步。</p>
                    </div>`,
                  },
                  {
                    key: "kill",
                    label: "「杀死」的真实语义",
                    children: html`<p>协作式取消，不是进程强杀。点击后状态变为「停止中」，任务在下一个交易日边界退出；staging 已提交单元不回滚，重跑自动复用。杀死响应上限约一次请求超时（默认 15 秒）。</p>`,
                  },
                  {
                    key: "resume",
                    label: "断点续传",
                    children: html`<p>增量水位存 sync_state（market + dataset），与白名单内容解耦：白名单变动不会让增量退化为全量重扫。失败日保留 staging 文件，重跑复用不重复请求数据源。</p>`,
                  },
                ]}
              />
            <//>

            <${antd.Card} title="已知边界" style=${{ marginTop: 16 }}>
              <ul style=${{ margin: 0, paddingLeft: 20, lineHeight: 1.9 }}>
                <li>白名单只降存储量，不降抓取请求数与耗时（daily 按交易日拉全市场）。</li>
                <li>默认不抓停牌信息，质量页的「缺失交易日」可能包含停牌日。</li>
                <li>港美股执行器本期未实现（占位不可用）；复权对 HK/US 一律 NONE/1.0，具体折算待数据源确定后立项。</li>
                <li>白名单任务仅手动触发；白名单为空时日 K 任务拒绝执行，不会自动退化为全量。</li>
                <li>全部任务均由界面人工触发，系统无常驻定时调度。</li>
                <li>服务停止为平滑退出：停止接收新任务，等待运行中任务结束后退出；重启请执行 start_yafip 脚本的 restart。</li>
              </ul>
            <//>
          <//>
        <//>
      </div>
    `;
  }

  Y.pages.system_about = About;
})();
