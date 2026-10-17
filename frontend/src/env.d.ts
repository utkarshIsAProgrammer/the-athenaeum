/// <reference types="astro/client" />

interface ImportMetaEnv {
  /** Origin of the FastAPI service, used for server-side fetches only. */
  readonly INTERNAL_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
