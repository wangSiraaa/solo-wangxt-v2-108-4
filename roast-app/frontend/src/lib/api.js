// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster.
const qs = (p) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null));

export async function getBatches() {
  const r = await fetch('/api/batches');
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function seed() {
  const r = await fetch('/api/seed', { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getSeries(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/series?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getCompare(a, b, params = {}) {
  const r = await fetch(`/api/compare?${qs({ a, b, ...params })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function addEvent(batchId, ev) {
  const r = await fetch(`/api/batches/${batchId}/events`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(ev),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listEvents(batchId, includeHistory = false) {
  const r = await fetch(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function exportBatch(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/export?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function recompute(body) {
  const r = await fetch('/api/recompute', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// --- auditable interruption ledger ----------------------------------------
// 探针失联（采样 NULL）不是中断；只有操作员登记的“停止加热”才是。
// 合法迁移: in_progress --start--> interrupted --resume--> resumed --> ... --> ended

export async function listInterruptions(batchId, includeHistory = false) {
  const r = await fetch(
    `/api/batches/${batchId}/interruptions?${qs({ include_history: includeHistory })}`
  );
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function addInterruption(batchId, body) {
  const r = await fetch(`/api/batches/${batchId}/interruptions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function correctInterruption(batchId, intervalId, body) {
  const r = await fetch(
    `/api/batches/${batchId}/interruptions/correct?${qs({ interval_id: intervalId })}`,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }
  );
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export const INTERRUPTION_REASONS = {
  power_cut: '供电中断',
  safety_check: '安全检查',
  schedule: '调度/排班',
  equipment: '设备处置',
  other: '其他',
};

export const BATCH_STATUS_LABELS = {
  in_progress: '进行中',
  interrupted: '已中断（停止加热）',
  resumed: '已恢复',
  ended: '已结束',
};

export const EVENT_LABELS = {
  charge: '下豆/开火',
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
  damper_change: '风门变化',
  custom: '自定义',
};

export function fmtTime(s) {
  if (s === null || s === undefined) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
