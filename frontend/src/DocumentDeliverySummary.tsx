import { useState } from 'react';
import type { Json } from './api';
import { useRemoteResource } from './useRemoteResource';
import './implementation-document.css';

export function DocumentDeliveryDetails({ document, delivery, reviewRound }: { document: Json; delivery: Json; reviewRound?: number }) {
  const [notice, setNotice] = useState('');
  if (delivery.document_id !== document.document_id || delivery.document_sha256 !== document.sha256)
    return <p role="alert">交付信息与所选文档版本不一致，请重新读取。</p>;
  const fileVersion = Number.isSafeInteger(delivery.file_version) && delivery.file_version > 0 ? `v${delivery.file_version}` : '以完整 SHA256 标识';
  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setNotice('已复制'); }
    catch { setNotice('复制失败，请选中下方文本手动复制'); }
  };
  const receipt = `文档：${delivery.file_name}\n路径：${delivery.absolute_path}\n文件版本：${fileVersion}`
    + (reviewRound ? `\n审核轮次：${reviewRound}` : '')
    + `\ndocument_id：${delivery.document_id}\ndocument_sha256：${delivery.document_sha256}`;
  return <div className="document-delivery-summary" aria-label="文档交付信息">
    <div className="document-delivery-label"><strong>{delivery.file_name}</strong><span>文件版本 {fileVersion}</span>
      {reviewRound && <span>审核第 {reviewRound} 轮</span>}</div>
    <dl><dt>保存位置</dt><dd><code>{delivery.absolute_path}</code></dd>
      <dt>文档 ID</dt><dd><code>{delivery.document_id}</code></dd>
      <dt>正文 SHA256</dt><dd><code>{delivery.document_sha256}</code></dd></dl>
    <div className="document-delivery-actions"><button type="button" className="btn small" onClick={() => void copy(delivery.absolute_path)}>复制路径</button>
      <button type="button" className="btn small" onClick={() => void copy(receipt)}>复制交付信息</button><span role="status">{notice}</span></div>
  </div>;
}

export function DocumentDeliverySummary({ runId, document, reviewRound }: { runId: string; document: Json; reviewRound?: number }) {
  const [retry, setRetry] = useState(0);
  const path = `/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(document.document_id)}/delivery?expected_sha256=${encodeURIComponent(document.sha256)}`;
  const resource = useRemoteResource<Json>(path, retry, 0, true);
  return resource.data ? <DocumentDeliveryDetails key={`${document.document_id}:${document.sha256}`} document={document} delivery={resource.data} reviewRound={reviewRound} />
    : resource.error ? <p role="alert">文档交付信息读取失败：{resource.error} <button className="btn small" onClick={() => setRetry(value => value + 1)}>重试</button></p>
    : <p className="muted" role="status">正在核对文档保存位置与文件版本…</p>;
}
