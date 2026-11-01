import { ReferenceText } from './ReferenceNames';
import { Component, type ReactNode } from 'react';

/** Isolate a detail rendering failure so navigation and dialog closing still work. */
export class ViewBoundary extends Component<{ label: string; children: ReactNode; reloadPage?: boolean }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <section role="alert" className="inline-info">
      <p><ReferenceText value={this.props.label} />暂时无法显示。研究记录已保留，可以切换其他页面或重试。</p>
      <button type="button" className="btn" onClick={() => this.setState({ failed: false })}>重新显示</button>
      {this.props.reloadPage && <button type="button" className="btn" onClick={() => window.location.reload()}>重新载入页面</button>}
    </section>;
    return this.props.children;
  }
}
