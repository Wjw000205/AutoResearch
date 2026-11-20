import { useEffect, useState } from 'react';
import { type HarnessActivity } from './api';
import { ReferenceText } from './ReferenceNames';
import './HarnessPreparation.css';

const preparationPhases = new Set(['source_inventory', 'historical_handoffs', 'capability_materials']);
export function localValidationHeading(value: HarnessActivity['local_validation']) {
  return value?.phase && preparationPhases.has(value.phase) ? '正在准备审核材料' : '正在本地核验已保存回执';
}

export function LocalPreparationProgress({ value, running, label }: {
  value?: HarnessActivity['local_validation']; running: boolean; label?: string;
}) {
  const [now, setNow] = useState(Date.now);
  const started = value?.started_at ? Date.parse(value.started_at) : NaN;
  useEffect(() => {
    if (!running || !value || !Number.isFinite(started)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, started, value?.operation_id]);
  if (!running || !value) return null;
  const seconds = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : null;
  const counted = Number.isInteger(value.completed) && Number.isInteger(value.total)
    && Number(value.total) > 0 && Number(value.completed) >= 0 && Number(value.completed) <= Number(value.total);
  return <div className="harness-local-progress" role="status" aria-label="宿主本地准备进度">
    <div><strong>{localValidationHeading(value)}</strong>
      {seconds !== null && <small>已用 {Math.floor(seconds / 60)} 分 {seconds % 60} 秒</small>}</div>
    <p><ReferenceText value={label ?? value.label} /></p>
    {counted && <p>当前步骤已核验 {value.completed} / {value.total} {value.unit ?? '项'}
      {value.role && <span> · {({ director: '研究总监', scout: '文献研究员' } as Record<string, string>)[value.role] ?? value.role}</span>}</p>}
    {value.heartbeat_at && <small>本地校验心跳：{new Date(value.heartbeat_at).toLocaleTimeString('zh-CN')}</small>}
    {value.model_dispatched === false && <small>当前步骤由宿主本地处理；此处计数不代表审核通过或实验完成。</small>}
  </div>;
}
