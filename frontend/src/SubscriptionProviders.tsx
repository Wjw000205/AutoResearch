import { ReferenceText } from './ReferenceNames';
import type { Json, Provider } from './api';

export function SubscriptionProvider({ provider, change, check, checking, result }: {
  provider: Provider; change: (key: string, value: any) => void;
  check: () => void; checking: boolean; result?: Json;
}) {
  return <section className="panel provider-settings">
    <div className="provider-settings-top"><div><h3><ReferenceText value={provider.name} /></h3><span>Codex runtime · ChatGPT 登录</span></div>
      <button className="btn small" type="button" disabled={checking} onClick={check}>{checking ? '检查中…' : '检查连接（不生成）'}</button></div>
    <p>复用本机 Codex 的 ChatGPT 登录；自动提交任务、等待结果并回填工具结果。Codex 仅接收材料并输出推理结果，原生工具已禁用；研究工具和实验由本程序管理。</p>
    <p>使用套餐额度；实际词元与金额仅在有回执时报告。连接失败或等待超时不会回退到付费 API。</p>
    <div className="form-grid">
      <label className="field"><span>模型标识</span><input value={provider.model} onChange={e => change('model', e.target.value)} /></label>
        <label className="field"><span>普通阶段推理强度</span><select value={provider.reasoning_effort} onChange={e => change('reasoning_effort', e.target.value)}>{['low', 'medium', 'high', 'xhigh', 'max', 'ultra'].map(v => <option key={v}>{v}</option>)}</select></label>
        <label className="field"><span>创新讨论、架构设计推理强度</span><select value={provider.deep_reasoning_effort} onChange={e => change('deep_reasoning_effort', e.target.value)}>{['low', 'medium', 'high', 'xhigh', 'max', 'ultra'].map(v => <option key={v}>{v}</option>)}</select></label>
      {[['timeout_seconds', '普通阶段等待时间（分钟）'], ['deep_timeout_seconds', '创新讨论、架构设计等待时间（分钟）']].map(([key, label]) => <label className="field" key={key}><span>{label}</span><input type="number" min="1" max="240" value={provider[key] / 60} onChange={e => change(key, Number(e.target.value) * 60)} /></label>)}
    </div>
    {result && <p role="status"><ReferenceText value={String(result.message ?? '')} /><ReferenceText value={result.auth_mode && ` 登录：${result.auth_mode} · ${result.plan_type ?? '未知套餐'}`} /></p>}
  </section>;
}
