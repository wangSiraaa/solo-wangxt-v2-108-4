<script>
  import { onMount } from 'svelte';
  import RoastChart from './lib/RoastChart.svelte';
  import {
    getBatches,
    seed,
    getSeries,
    getCompare,
    addEvent,
    listEvents,
    addInterruption,
    correctInterruption,
    exportBatch,
    recompute,
    EVENT_LABELS,
    INTERRUPT_ACTION_LABELS,
    BATCH_STATUS_LABELS,
    INTERRUPT_REASONS,
    fmtTime,
  } from './lib/api.js';

  let batches = [];
  let view = 'single'; // single | compare
  let selA = null;
  let selB = null;
  let dataA = null;
  let dataB = null;
  let comparePayload = null;
  let eventHistory = [];
  let loading = '';
  let error = '';

  // Analysis parameters — affect DERIVED traces only, never stored samples.
  let windowS = 30;
  let smoothS = 12;
  let maxGapFillS = 45;

  // event correction form
  let newEventType = 'turning_point';
  let newEventTime = '1:00';
  let newEventDamper = '';
  let showHistory = false;

  // interruption ledger form
  let interruptTime = '';
  let interruptReason = 'power_outage';
  let interruptNote = '';
  // per-current-interval backfill correction: { [interval_id]: {start,end,close} }
  let corrections = {};
  let ledgerBusy = false;

  // export verification
  let verifyResult = null;

  // source tag anchor for each phase row (the "interesting" boundary)
  function anchorKey(metricKey) {
    return {
      drying: 'turning_point',
      maillard: 'first_crack_start',
      development: 'drop',
      first_crack_window: 'first_crack_end',
      total: 'drop',
    }[metricKey];
  }

  onMount(loadBatches);

  async function loadBatches() {
    error = '';
    try {
      batches = await getBatches();
      if (batches.length) {
        selA = batches[0].id;
        selB = batches[batches.length - 1].id;
        await refresh();
      }
    } catch (e) {
      error = `无法连接后端：${e.message}`;
    }
  }

  async function doSeed() {
    loading = '正在生成合成批次…';
    error = '';
    try {
      await seed();
      await loadBatches();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function refresh() {
    if (!selA) return;
    loading = '加载曲线…';
    error = '';
    verifyResult = null;
    const params = {
      window_s: windowS,
      display_smooth_s: smoothS,
      max_gap_fill_s: maxGapFillS,
    };
    try {
      dataA = await getSeries(selA, { ...params, include_history: showHistory });
      eventHistory = await listEvents(selA, showHistory);
      if (view === 'compare' && selB && selB !== selA) {
        comparePayload = await getCompare(selA, selB, params);
        dataB = comparePayload.batches[1];
      } else {
        comparePayload = null;
        dataB = null;
      }
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function parseMMSS(str) {
    const m = /^(\d+):([0-5]?\d)$/.exec(str.trim());
    if (!m) return null;
    return Number(m[1]) * 60 + Number(m[2]);
  }

  async function submitEvent() {
    const t = parseMMSS(newEventTime);
    if (t === null) {
      error = '时间格式应为 m:ss，例如 1:05';
      return;
    }
    const body = {
      event_type: newEventType,
      t_s: t,
      source: 'manual',
      created_by: '操作员(界面)',
      label: `${EVENT_LABELS[newEventType] || newEventType} 人工修正`,
    };
    if (newEventType === 'damper_change') {
      const v = Number(newEventDamper);
      if (!Number.isFinite(v)) {
        error = '风门变化需要填写新风门开度 (%)';
        return;
      }
      body.value_num = v;
      body.label = `风门 → ${v}% 人工标记`;
    }
    loading = '保存修正…';
    error = '';
    try {
      await addEvent(selA, body);
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  // --- interruption ledger ----------------------------------------------

  async function submitInterrupt(action) {
    const t = parseMMSS(interruptTime);
    if (t === null) {
      error = '中断时间格式应为 m:ss，例如 3:20';
      return;
    }
    ledgerBusy = true;
    loading = action === 'start' ? '记录中断开始…' : action === 'resume' ? '记录恢复…' : '确认终止…';
    error = '';
    try {
      await addInterruption(selA, {
        action,
        t_s: t,
        reason: interruptReason,
        note: interruptNote,
        source: 'manual',
        created_by: '操作员(界面)',
      });
      interruptTime = '';
      interruptNote = '';
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      ledgerBusy = false;
      loading = '';
    }
  }

  function correctionDraft(iv) {
    if (!corrections[iv.interval_id]) {
      corrections[iv.interval_id] = {
        start: fmtTime(iv.start_s),
        end: iv.end_s === null ? '' : fmtTime(iv.end_s),
        close_action: iv.close_action || 'resume',
        reason: iv.reason || 'power_outage',
        note: '',
      };
    }
    return corrections[iv.interval_id];
  }

  async function submitCorrection(iv) {
    const d = correctionDraft(iv);
    const start = parseMMSS(d.start);
    const end = parseMMSS(d.end);
    if (start === null || end === null) {
      error = `区间 #${iv.interval_id} 修正时间格式应为 m:ss`;
      return;
    }
    if (end <= start) {
      error = `区间 #${iv.interval_id} 结束必须晚于开始，拒绝猜测`;
      return;
    }
    ledgerBusy = true;
    loading = `写入区间 #${iv.interval_id} 的新版本…`;
    error = '';
    try {
      await correctInterruption(selA, iv.interval_id, {
        start_s: start,
        end_s: end,
        close_action: d.close_action,
        reason: d.reason,
        note: d.note || '操作员后补修正（界面）',
        source: 'post_hoc',
        created_by: '操作员(界面)',
      });
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      ledgerBusy = false;
      loading = '';
    }
  }

  $: ledgerRecords = dataA ? dataA.interruptions.records : [];
  $: ledgerIntervals = dataA ? dataA.interruptions.intervals : [];
  $: ledgerConflicts = dataA ? dataA.interruptions.conflicts : [];

  async function verifyExport() {
    loading = '导出并重算校验…';
    error = '';
    verifyResult = null;
    try {
      const ex = await exportBatch(selA, {
        window_s: windowS,
        display_smooth_s: smoothS,
      });
      const rc = await recompute({
        samples: ex.series.raw_points.map((p) => ({
          t_s: p.t_s,
          bean_temp_c: p.bean_temp_c,
          env_temp_c: p.env_temp_c,
        })),
        events: ex.events,
        interrupt_records: ex.interruptions.records,
        params: ex.params,
      });
      const keys = Object.keys(ex.metrics).filter(
        (k) =>
          (k.endsWith('_s') || k.endsWith('_active_s') || k === 'development_ratio' ||
            k === 'development_ratio_active')
      );
      const rows = keys.map((k) => ({
        key: k,
        exported: ex.metrics[k],
        recomputed: rc.metrics[k],
        match: ex.metrics[k] === rc.metrics[k],
      }));
      // ledger boundaries + time basis must survive an independent recompute
      const sigIv = (v) =>
        JSON.stringify(
          (v.interruptions.intervals || []).map((i) => [
            i.interval_id, i.version, i.start_s, i.end_s, i.duration_s, i.is_open,
          ])
        );
      const ledgerSame = sigIv(ex) === sigIv(rc);
      const elapsedSame =
        ex.elapsed.wall_elapsed_s === rc.elapsed.wall_elapsed_s &&
        ex.elapsed.active_elapsed_s === rc.elapsed.active_elapsed_s;
      const recomputedStatus = rc.interruptions.ledger_status;
      // Changing window/smoothing must leave every stored sample untouched.
      const alt = await getSeries(selA, {
        window_s: windowS * 2,
        display_smooth_s: smoothS === 0 ? 30 : 0,
        max_gap_fill_s: maxGapFillS,
      });
      const sig = (arr) =>
        JSON.stringify(arr.map((p) => [p.t_s, p.bean_temp_c, p.env_temp_c]));
      const rawSame = sig(ex.series.raw_points) === sig(alt.series.raw_points);
      verifyResult = {
        rows, rawSame, ledgerSame, elapsedSame, recomputedStatus, exportObj: ex,
      };
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function downloadExport() {
    if (!verifyResult?.exportObj) return;
    const blob = new Blob([JSON.stringify(verifyResult.exportObj, null, 2)], {
      type: 'application/json',
    });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${verifyResult.exportObj.batch.name}-export.json`;
    a.click();
  }

  $: chartPayloads =
    view === 'compare' && comparePayload
      ? comparePayload.batches
      : dataA
        ? [dataA]
        : [];

  let refreshTimer;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }
</script>

<header style="padding:14px 20px;border-bottom:1px solid var(--line)">
  <h1>咖啡烘焙批次曲线 · 过程记录对比</h1>
  <div class="muted" style="margin-top:2px">
    豆温 / 环境温度 / 操作事件的过程视角 · 合成数据离线运行，<b>未连接真实烘焙机</b>
  </div>
</header>

<main style="padding:16px 20px;display:flex;flex-direction:column;gap:14px">
  {#if error}
    <div class="warn">⚠ {error}</div>
  {/if}

  <section class="panel">
    <div class="row" style="align-items:flex-end">
      <div>
        <div class="muted">数据</div>
        {#if batches.length === 0}
          <button on:click={doSeed}>① 生成两个合成批次（含噪声/不均采样/探针缺测）</button>
        {:else}
          <button class="ghost" on:click={doSeed}>重新生成合成批次</button>
        {/if}
      </div>
      <div>
        <div class="muted">视图</div>
        <label class="inline">
          <input type="radio" bind:group={view} value="single" on:change={refresh} />单批次
        </label>
        <label class="inline">
          <input type="radio" bind:group={view} value="compare" on:change={refresh} />双批次对比
        </label>
      </div>
      <div>
        <div class="muted">批次 A</div>
        <select bind:value={selA} on:change={refresh}>
          {#each batches as b}
            <option value={b.id}>{b.name} · {b.bean}</option>
          {/each}
        </select>
      </div>
      {#if view === 'compare'}
        <div>
          <div class="muted">批次 B</div>
          <select bind:value={selB} on:change={refresh}>
            {#each batches as b}
              <option value={b.id}>{b.name} · {b.bean}</option>
            {/each}
          </select>
        </div>
      {/if}
    </div>

    <div class="row" style="margin-top:12px;align-items:flex-end">
      <label class="inline">
        RoR 回归窗口
        <input
          type="number"
          min="5"
          max="300"
          step="5"
          bind:value={windowS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        显示平滑（仅 RoR 曲线）
        <input
          type="number"
          min="0"
          max="180"
          step="3"
          bind:value={smoothS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        最大插值桥接
        <input
          type="number"
          min="5"
          max="600"
          step="5"
          bind:value={maxGapFillS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      {#if loading}<span class="muted">{loading}</span>{/if}
    </div>
    <div class="muted" style="margin-top:6px;font-size:12px">
      RoR 口径：在每个实测时刻，对居中 ±{(windowS / 2).toFixed(0)}s 时间窗内的<b>实测</b>豆温点做最小二乘直线拟合取斜率（°C/min），
      至少 4 个点且跨度 ≥10s 才出值；插值点不参与拟合，缺测宽缺口处 RoR 断档。调整窗口/平滑<b>只改变派生曲线，不改原始温度</b>。
    </div>
  </section>

  {#if dataA}
    <section class="panel">
      <RoastChart {chartPayloads} {windowS} {smoothS} />
      <div class="row" style="margin-top:6px;font-size:12px;flex-wrap:wrap">
        <span class="tag">圆点＝实测豆温</span>
        <span class="tag">虚线菱形＝线性插值（非实测）</span>
        <span class="tag">细点线＝环境温度</span>
        <span class="tag">金色竖虚线＝风门变化</span>
        <span class="tag">曲线断档＝缺测未桥接</span>
        <span class="tag" style="border-color:#d4af37;color:#f3c98b">琥珀色带＝加热中断（区间内不计活动时长）</span>
        <span class="tag" style="border-color:#e35d5d;color:#e35d5d">红色虚线带＝中断区间冲突（活动指标不可计算）</span>
      </div>
      {#if comparePayload}
        <div class="warn" style="margin-top:8px">{comparePayload.interpretation}</div>
      {/if}
    </section>

    <section class="row">
      <div class="panel col" style="flex:1.4">
        <h2>
          阶段指标（墙钟 vs 活动烘焙时长）
          <span class="tag" style="margin-left:8px">
            批次状态：{BATCH_STATUS_LABELS[dataA.batch.status] || dataA.batch.status}
          </span>
        </h2>

        <table style="margin-bottom:8px">
          <tr>
            <td style="width:140px">墙上时钟经过</td>
            <td><b style="font-size:15px">{fmtTime(dataA.elapsed.wall_elapsed_s)}</b></td>
            <td class="muted" style="font-size:11px">
              从下豆到出锅{dataA.elapsed.ended_with_drop ? '' : '（进行中，按最后实测点计）'}，时钟不暂停
            </td>
          </tr>
          <tr>
            <td>真正持续加热（活动）</td>
            <td>
              <b style="font-size:15px;color:#f3c98b">
                {dataA.elapsed.active_elapsed_s === null
                  ? '不可计算'
                  : fmtTime(dataA.elapsed.active_elapsed_s)}
              </b>
            </td>
            <td class="muted" style="font-size:11px">
              {#if dataA.elapsed.excluded_interrupted_s}
                已扣除中断 {fmtTime(dataA.elapsed.excluded_interrupted_s)}
              {:else}
                墙钟减去当前版本已关闭中断区间的并集
              {/if}
              {#if dataA.elapsed.open_interval_id !== null}
                ；当前中断区间计至最后实测点，不外推
              {/if}
            </td>
          </tr>
        </table>

        {#if dataA.metrics.active_metrics_status === 'conflict'}
          <div class="warn">
            ⛔ 中断账本存在冲突，<b>活动口径与活动 DTR 全部不输出</b>（绝不伪造）；墙钟指标照常给出：
            {ledgerConflicts.map((c) => c.message).join('；')}
          </div>
        {:else if dataA.metrics.active_metrics_status === 'partial'}
          <div class="warn" style="border-color:#d4af37">
            ⚠ 有阶段锚点落在中断期内，对应活动时长为「不可计算」而不是猜测：
            {dataA.metrics.active_blockers.map((b) => b.message).join('；')}
          </div>
        {/if}

        <div class="row" style="gap:8px">
          {#each view === 'compare' && dataB ? [dataA, dataB] : [dataA] as pl, i}
            <div style="flex:1;min-width:300px">
              <div class="muted" style="margin-bottom:4px">
                {i === 0 ? 'A' : 'B'} · {pl.batch.name}
              </div>
              <table>
                <tr>
                  <th>阶段</th>
                  <th>区间定义</th>
                  <th>墙钟时长</th>
                  <th>活动时长</th>
                  <th>中断扣除</th>
                  <th>来源</th>
                </tr>
                {#each pl.metrics.duration_table as row}
                  <tr>
                    <td>{row.label}</td>
                    <td class="muted" style="font-size:11px">{row.definition}</td>
                    <td>{fmtTime(row.wall_s)}</td>
                    <td style="color:#f3c98b">
                      {row.active_s === null
                        ? (row.wall_s === null ? '—' : '不可计算')
                        : fmtTime(row.active_s)}
                    </td>
                    <td>{row.interrupted_s ? fmtTime(row.interrupted_s) : (row.wall_s === null ? '—' : '0:00')}</td>
                    <td style="font-size:11px">
                      <span class="tag {pl.metrics.anchors[anchorKey(row.key)]?.source}">
                        {pl.metrics.anchors[anchorKey(row.key)]?.source || '—'}
                      </span>
                    </td>
                  </tr>
                {/each}
                <tr>
                  <td><b>发展时间比 DTR（墙钟）</b></td>
                  <td class="muted" style="font-size:11px">发展期/总时长</td>
                  <td colspan="2">
                    <b>{pl.metrics.development_ratio !== null
                      ? (pl.metrics.development_ratio * 100).toFixed(1) + '%'
                      : '—'}</b>
                  </td>
                  <td colspan="2"></td>
                </tr>
                <tr>
                  <td><b style="color:#f3c98b">发展时间比 DTR（活动口径）</b></td>
                  <td class="muted" style="font-size:11px">活动发展期/活动总时长</td>
                  <td colspan="2">
                    <b style="color:#f3c98b">
                      {pl.metrics.development_ratio_active !== null
                        ? (pl.metrics.development_ratio_active * 100).toFixed(1) + '%'
                        : (pl.metrics.active_metrics_status === 'unavailable' ? '—' : '不可计算')}
                    </b>
                  </td>
                  <td colspan="2"></td>
                </tr>
              </table>
              <div class="muted" style="font-size:11px;margin-top:4px">
                口径：{pl.metrics.time_basis.active_fields_suffix} 字段＝墙钟减去区间并集；区间按半开
                [开始,恢复) 处理，恢复瞬间算活动、开始瞬间算中断。
              </div>
            </div>
          {/each}
        </div>
      </div>

      <div class="panel col">
        <h2>人工修正事件（批次 A）· 保留来源</h2>
        <div class="row" style="gap:8px;align-items:flex-end">
          <div>
            <div class="muted">类型</div>
            <select bind:value={newEventType}>
              {#each Object.entries(EVENT_LABELS) as [k, v]}
                {#if k !== 'charge'}<option value={k}>{v}</option>{/if}
              {/each}
            </select>
          </div>
          <div>
            <div class="muted">时间 m:ss</div>
            <input bind:value={newEventTime} placeholder="1:05" style="width:80px" />
          </div>
          {#if newEventType === 'damper_change'}
            <div>
              <div class="muted">新风门 %</div>
              <input bind:value={newEventDamper} type="number" min="0" max="100" style="width:80px" />
            </div>
          {/if}
          <button on:click={submitEvent}>提交修正</button>
          <label class="inline" style="align-self:center">
            <input type="checkbox" bind:checked={showHistory} on:change={refresh} />
            显示已被取代的旧值
          </label>
        </div>

        <table style="margin-top:10px">
          <tr><th>事件</th><th>时间</th><th>来源</th><th>备注</th><th>状态</th></tr>
          {#each eventHistory as e}
            <tr style={e.superseded ? 'opacity:.45' : ''}>
              <td>
                {EVENT_LABELS[e.event_type] || e.event_type}
                {e.value_num !== null && e.value_num !== undefined ? ` → ${e.value_num}%` : ''}
              </td>
              <td>{fmtTime(e.t_s)}</td>
              <td>
                <span class="tag {e.source}">{e.source === 'manual' ? '人工' : '自动建议'}</span>
                {e.created_by}
              </td>
              <td class="muted" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis">
                {e.label}
              </td>
              <td>{e.superseded ? '已被修正取代（保留）' : '当前'}</td>
            </tr>
          {/each}
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>中断区间账本 · 只追加 / 有版本 / 可审计（批次 A）</h2>
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        供电中断、安全检查等导致<b>真正停止加热</b>时在此记账。探针失联（缺测点）<b>不</b>自动算作停机——只有操作员明确记录才成立。
        墙上时钟不暂停，活动烘焙时长只扣除当前版本已关闭区间；非法动作（无未关闭中断却恢复、重复开始/恢复）会被服务端拒绝，不猜测。
      </div>

      <div class="row" style="gap:10px;align-items:flex-end;flex-wrap:wrap">
        <div>
          <div class="muted">时刻 m:ss（墙钟，自下豆起）</div>
          <input bind:value={interruptTime} placeholder="3:20" style="width:90px" />
        </div>
        <div>
          <div class="muted">原因</div>
          <select bind:value={interruptReason}>
            {#each Object.entries(INTERRUPT_REASONS) as [k, v]}
              <option value={k}>{v}</option>
            {/each}
          </select>
        </div>
        <div style="flex:1;min-width:160px">
          <div class="muted">备注（可选）</div>
          <input bind:value={interruptNote} placeholder="如：安全门连锁触发，单号 A-17" style="width:100%" />
        </div>
        <button on:click={() => submitInterrupt('start')} disabled={ledgerBusy}>中断开始</button>
        <button class="ghost" on:click={() => submitInterrupt('resume')} disabled={ledgerBusy}>恢复加热</button>
        <button class="ghost" on:click={() => submitInterrupt('terminate')} disabled={ledgerBusy}>确认终止批次</button>
      </div>

      {#if dataA.interruptions.open_interval_id !== null}
        <div class="warn" style="border-color:#d4af37;margin-top:8px">
          当前有<b>未关闭</b>的中断区间 #{dataA.interruptions.open_interval_id}：
          批次状态＝已中断；活动时长暂按最后实测点封顶，恢复后自动结算。
        </div>
      {/if}

      {#if ledgerConflicts.length}
        <div class="warn" style="margin-top:8px">
          {#each ledgerConflicts as cf}
            <div>⛔ [{cf.kind}] {cf.message || '账本结构冲突'}
              {#if cf.interval_ids}（区间 {cf.interval_ids.join('、')}）{/if}</div>
          {/each}
        </div>
      {/if}

      <div class="row" style="gap:12px;margin-top:10px;align-items:flex-start;flex-wrap:wrap">
        <div style="flex:1;min-width:420px">
          <div class="muted" style="margin-bottom:4px">操作记录（append-only）</div>
          <table>
            <tr>
              <th>#</th><th>动作</th><th>时刻</th><th>原因</th><th>来源/记录人</th><th>版本</th><th>状态</th>
            </tr>
            {#each ledgerRecords as r}
              <tr style={r.superseded ? 'opacity:.45' : ''}>
                <td>{r.interval_id ?? '—'}</td>
                <td>{INTERRUPT_ACTION_LABELS[r.action] || r.action}</td>
                <td>{fmtTime(r.t_s)}</td>
                <td>{INTERRUPT_REASONS[r.reason] || r.reason || '—'}</td>
                <td style="font-size:11px">
                  <span class="tag {r.source}">
                    {r.source === 'manual' ? '现场' : r.source === 'post_hoc' ? '后补' : r.source}
                  </span>
                  {r.created_by}
                </td>
                <td>v{r.version}</td>
                <td>{r.superseded ? `已被 v${ledgerRecords.find((x) => x.id === r.superseded_by_id)?.version || ''} 取代（保留）` : '当前'}</td>
              </tr>
            {/each}
            {#if ledgerRecords.length === 0}
              <tr><td colspan="7" class="muted">暂无中断记录</td></tr>
            {/if}
          </table>
        </div>

        <div style="flex:1;min-width:420px">
          <div class="muted" style="margin-bottom:4px">当前区间与后补修正（写入新版本，旧版本保留）</div>
          <table>
            <tr>
              <th>区间</th><th>开始</th><th>结束</th><th>时长</th><th>关闭方式</th><th>新版本修正</th>
            </tr>
            {#each ledgerIntervals as iv}
              <tr>
                <td>#{iv.interval_id}<div class="muted" style="font-size:10px">v{iv.version}</div></td>
                <td>{fmtTime(iv.start_s)}</td>
                <td>{iv.end_s === null ? '未关闭' : fmtTime(iv.end_s)}</td>
                <td>{iv.duration_s === null ? `→ ${fmtTime(dataA.series.raw_points.at(-1)?.t_s)} 封顶` : fmtTime(iv.duration_s)}</td>
                <td>{iv.close_action ? INTERRUPT_ACTION_LABELS[iv.close_action] : '—'}</td>
                <td>
                  <div class="row" style="gap:4px;align-items:center;flex-wrap:wrap">
                    <input
                      value={correctionDraft(iv).start}
                      on:input={(e) => (correctionDraft(iv).start = e.target.value)}
                      style="width:52px" placeholder="m:ss"
                    />
                    →
                    <input
                      value={correctionDraft(iv).end}
                      on:input={(e) => (correctionDraft(iv).end = e.target.value)}
                      style="width:52px" placeholder="m:ss"
                    />
                    <select
                      value={correctionDraft(iv).close_action}
                      on:change={(e) => (correctionDraft(iv).close_action = e.target.value)}
                    >
                      <option value="resume">恢复</option>
                      <option value="terminate">终止</option>
                    </select>
                    <select
                      value={correctionDraft(iv).reason}
                      on:change={(e) => (correctionDraft(iv).reason = e.target.value)}
                    >
                      {#each Object.entries(INTERRUPT_REASONS) as [k, v]}
                        <option value={k}>{v}</option>
                      {/each}
                    </select>
                    <button class="ghost" style="padding:2px 8px" on:click={() => submitCorrection(iv)}>
                      存为 v{iv.version + 1}
                    </button>
                  </div>
                </td>
              </tr>
            {/each}
            {#if ledgerIntervals.length === 0}
              <tr><td colspan="6" class="muted">无区间</td></tr>
            {/if}
          </table>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2>缺测与插值审计 · 导出可复现</h2>
      <div class="row">
        <div style="flex:1;min-width:280px">
          <table>
            <tr><th>通道</th><th>起(s)</th><th>止(s)</th><th>缺测点</th><th>处理</th></tr>
            {#each dataA.series.missing_segments as g}
              <tr>
                <td>{g.channel === 'bean' ? '豆温' : '环境'}</td>
                <td>{g.t_start_s.toFixed(1)}</td>
                <td>{g.t_end_s.toFixed(1)}</td>
                <td>{g.n_missing}</td>
                <td>
                  {#if g.status === 'interpolated'}
                    <span style="color:#f3c98b">线性插值并标记（非实测）</span>
                  {:else if g.status === 'wide_unfilled'}
                    <span style="color:#e35d5d">缺口超 {maxGapFillS}s，不桥接（曲线断档）</span>
                  {:else}
                    端点缺测，不填充
                  {/if}
                </td>
              </tr>
            {/each}
          </table>
          <div class="muted" style="font-size:12px;margin-top:6px">
            实测豆温 {dataA.series.raw_points.filter((p) => p.bean_temp_c !== null).length} /
            总点 {dataA.series.raw_points.length}；
            插值点 {dataA.series.interpolated_t_s.length} 个，仅用于引导线，不写回原始采样表。
          </div>
        </div>
        <div style="flex:1;min-width:280px">
          <button on:click={verifyExport}>
            ② 导出 JSON 并用 /api/recompute 重算全部阶段指标
          </button>
          {#if verifyResult}
            <table style="margin-top:10px">
              <tr><th>指标</th><th>导出值</th><th>独立重算</th><th>一致</th></tr>
              {#each verifyResult.rows as r}
                <tr>
                  <td>{r.key}</td>
                  <td>{r.exported ?? '—'}</td>
                  <td>{r.recomputed ?? '—'}</td>
                  <td>{r.match ? '✅' : '❌'}</td>
                </tr>
              {/each}
            </table>
            <div style="margin-top:8px">
              <span class="{verifyResult.rawSame ? '' : 'warn'}">
                改变窗口/平滑后原始豆温/环温逐点比对：
                {verifyResult.rawSame ? '✅ 完全不变' : '❌ 被修改'}
              </span>
              <div style="margin-top:4px">
                <span class="{verifyResult.ledgerSame && verifyResult.elapsedSame ? '' : 'warn'}">
                  中断边界/版本/开放状态 + 墙钟/活动时长，独立重算比对：
                  {verifyResult.ledgerSame && verifyResult.elapsedSame ? '✅ 完全一致' : '❌ 不一致'}
                  （重算账本状态：{BATCH_STATUS_LABELS[
                    verifyResult.recomputedStatus === 'in_progress' ? 'in_progress'
                    : verifyResult.recomputedStatus === 'interrupted' ? 'interrupted'
                    : verifyResult.recomputedStatus === 'resumed' ? 'resumed'
                    : 'ended'
                  ] || verifyResult.recomputedStatus}）
                </span>
              </div>
              <button class="ghost" style="margin-left:10px" on:click={downloadExport}>
                下载导出 JSON
              </button>
            </div>
          {/if}
        </div>
      </div>
    </section>
  {/if}
</main>
