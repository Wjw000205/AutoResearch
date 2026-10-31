import { useId, useState } from 'react';
import { Code2, Copy, FolderOpen, RefreshCw } from 'lucide-react';
import { post, type Json } from './api';
import { useRemoteResource } from './useRemoteResource';

export function CodeWorkspaceActions({ value, loading = false, error = '', busy = false, message = '', selected = '',
  onSelect, onRefresh, onCopy, onOpen }: { value?: Json | null; loading?: boolean; error?: string; busy?: boolean; message?: string;
  selected?: string; onSelect?: (id: string) => void; onRefresh?: () => void; onCopy?: () => void; onOpen?: (file?: string) => void }) {
  const selectId = useId();
  const editors = Array.isArray(value?.editors) ? value.editors : [];
  const current = editors.some((editor: Json) => editor.id === selected) ? selected : editors[0]?.id ?? '';
  return <section className="code-workspace-access" aria-label="本机代码目录与编辑器">
    <div className="code-workspace-heading"><strong><FolderOpen size={15} />代码位置</strong>
      <button type="button" className="native-code-text-button" disabled={loading || busy} onClick={onRefresh}><RefreshCw size={12} />重新检测软件</button></div>
    {value?.available ? <>
      <div className="code-workspace-path"><code>{value.display_path || value.workspace}</code><button type="button" className="btn small" onClick={onCopy}><Copy size={13} />复制地址</button></div>
      {value.display_path !== value.workspace && <details><summary>实际写入目录</summary><code>{value.workspace}</code></details>}
      <div className="code-workspace-open"><label htmlFor={selectId}>打开方式</label>
        <select id={selectId} value={current} disabled={!editors.length || busy} onChange={event => onSelect?.(event.target.value)}>
          {editors.length ? editors.map((editor: Json) => <option key={editor.id} value={editor.id}>{editor.name}</option>) : <option value="">未检测到支持的编辑器</option>}</select>
        <button type="button" className="btn small" disabled={!current || busy} onClick={() => onOpen?.()}><FolderOpen size={14} />打开代码项目</button>
        {value.entry_file && <button type="button" className="btn small" disabled={!current || busy} onClick={() => onOpen?.(value.entry_file)}><Code2 size={14} />打开 {value.entry_file}</button>}
      </div>
      {!editors.length && <p className="muted">支持检测已安装的 PyCharm、VS Code 和 VS Code Insiders。也可复制地址后在编辑器中打开。</p>}
      {current && <small className="code-workspace-detected">已检测：{editors.find((editor: Json) => editor.id === current)?.executable}</small>}
    </> : <p className="muted">{loading ? '正在读取代码目录和本机编辑器…' : value?.reason ?? '代码目录尚未同步。'}</p>}
    {error && <p className="inline-warning" role="alert">{error}</p>}
    {(message || busy) && <p className="code-workspace-feedback" role="status">{busy ? '正在打开本机编辑器…' : message}</p>}
  </section>;
}

export function CodeWorkspaceAccess({ runId, workflowId }: { runId: string; workflowId?: string }) {
  const [refresh, setRefresh] = useState(0), [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false), [message, setMessage] = useState('');
  const resource = useRemoteResource<Json>(`/runs/${encodeURIComponent(runId)}/code-workspace`, `${workflowId}:${refresh}`);
  const value = resource.data?.workflow_id === workflowId || resource.data?.available === false ? resource.data : null;
  const editor = value?.editors?.find((item: Json) => item.id === selected) ?? value?.editors?.[0];
  async function copy() {
    try { await navigator.clipboard.writeText(value?.display_path || value?.workspace); setMessage('代码地址已复制'); }
    catch { setMessage('复制失败，请选中上方地址手动复制。'); }
  }
  async function open(file?: string) {
    if (!editor || !value?.available || busy) return;
    setBusy(true); setMessage('');
    try {
      const result = await post<Json>(`/runs/${encodeURIComponent(runId)}/code-workspace/open`,
        { workflow_id: value.workflow_id, editor_id: editor.id, relative_path: file ?? null });
      setMessage(`已请求 ${result.editor} 打开${file ? ` ${file}` : '代码项目'}`);
    } catch (error) { setMessage(error instanceof Error ? error.message : '打开失败，请重新检测编辑器。'); }
    finally { setBusy(false); }
  }
  return <CodeWorkspaceActions value={value} loading={resource.loading} error={resource.error} busy={busy} message={message}
    selected={selected} onSelect={id => { setSelected(id); setMessage(''); }} onRefresh={() => { setMessage(''); setRefresh(n => n + 1); }} onCopy={() => void copy()} onOpen={file => void open(file)} />;
}
