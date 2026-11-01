import { type Json } from './api';
import { ReferenceText } from './ReferenceNames';
import './architecture-delivery.css';

const states: Record<string, string> = {
  delivery_validation_failed: '作者已交付，架构接收校验失败',
  repair_pending: '已安排架构交付补正',
  repair_failed: '架构交付补正仍未通过',
};

export function architectureDeliveryLabel(revision?: Json): string | undefined {
  if (architectureLocallyRevalidated(revision)) return '已保存交付通过本地复验';
  return states[revision?.delivery_validation?.status ?? revision?.status];
}

export function architectureLocallyRevalidated(revision?: Json): boolean {
  return revision?.delivery_validation?.status === 'repair_pending'
    && revision.delivery_validation.local_revalidation?.status === 'passed';
}

export function architectureDeliveryNext(revision?: Json): string | undefined {
  if (architectureLocallyRevalidated(revision)) return '恢复执行后复用已保存答复，继续资源校验与独立架构评议。';
  const status = revision?.delivery_validation?.status ?? revision?.status;
  if (status === 'delivery_validation_failed') return '下一步：保留原答复并准备一次定向补正，修复接收格式后再独立评议。';
  if (status === 'repair_pending') return '下一步：恢复执行，由原作者补正已保存的交付，再校验统一架构。';
  if (status === 'repair_failed') return '补正仍有具体错误，已暂停自动重试；查看校验原因后处理。';
}

export function architectureReviewDeliveryLabel(current?: Json): string | undefined {
  const status = current?.review_delivery_validation?.status;
  if (status === 'repair_pending' || status === 'delivery_validation_failed') return '评审答复已保存，等待交付补正';
  if (status === 'repairing') return '正在补正已保存的评审交付';
  if (status === 'repair_failed') return '评审交付补正仍未通过';
}

export function ArchitectureDelivery({ revision }: { revision?: Json }) {
  const label = architectureDeliveryLabel(revision);
  if (!label) return null;
  const delivery = revision?.delivery_validation;
  const status = delivery?.status ?? revision?.status;
  const warning = status === 'delivery_validation_failed' || status === 'repair_failed';
  return <div className={`inline-info architecture-delivery-status architecture-delivery-${warning ? 'warning' : 'info'}`} aria-label="架构交付接收状态">
    <strong><ReferenceText value={label} /></strong>
    <p><ReferenceText value={architectureDeliveryNext(revision)} /></p>
    {delivery?.reason && delivery.reason !== delivery.error && <p><ReferenceText value={delivery.reason} /></p>}
    {delivery?.error && <details><summary>接收校验原因</summary><pre tabIndex={0}><ReferenceText value={delivery.error} /></pre></details>}
    <p className="muted">原答复、验收约束和历史意见保留。{architectureLocallyRevalidated(revision) ? '本地复验' : '交付补正'}不代表架构批准或实验已完成。</p>
  </div>;
}
