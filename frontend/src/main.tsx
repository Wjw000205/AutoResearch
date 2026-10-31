import React from 'react';
import 'katex/dist/katex.min.css';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';
import './research.css';
import './command-center.css';
import './stage-track.css';
import './paper-rag.css';
import './implementation-flow.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><App /></React.StrictMode>,
);
