import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  // Stage 2: backend ws_gateway (src/runtime/ws_gateway.py) listens here.
  // Proxy target is host-only; the /ws/chat path is forwarded unchanged.
  // 必须用 127.0.0.1 而非 localhost：node 会把 localhost 解析到 ::1，
  // 而后端只绑 IPv4，WS 代理会静默失败（曾导致前端全降级）。
  const wsGateway = env.VITE_WS_URL ?? 'ws://127.0.0.1:8766/ws/chat';
  const wsTarget = wsGateway.replace(/^ws/, 'http').replace(/\/.*$/, '');
  const httpGateway = env.VITE_HTTP_URL ?? 'http://127.0.0.1:8766';

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/ws': { target: wsTarget, ws: true },
        // Optional HTTP smoke bypass (POST /chat), FRONTEND_DEMO_DESIGN.md §1.4.
        '/api': {
          target: httpGateway,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
  };
});
