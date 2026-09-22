/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_BACKEND?: string;
  readonly VITE_WS_URL?: string;
  readonly VITE_HTTP_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
