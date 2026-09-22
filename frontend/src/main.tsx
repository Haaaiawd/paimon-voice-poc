import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import 'blackchalk/fonts.css';
import 'blackchalk/styles.css';
import './styles/index.css';
import App from './App';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
