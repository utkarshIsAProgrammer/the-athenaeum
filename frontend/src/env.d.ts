/// <reference types="astro/client" />

interface ImportMetaEnv {
  /** Origin of the FastAPI service, used for server-side fetches only. */
  readonly INTERNAL_API_URL?: string;
  /**
   * Shared with the FastAPI service, which refuses anything that does not
   * carry it. Must match `INTERNAL_API_SECRET` there; empty means the API is
   * not asking. Server-side only.
   */
  readonly INTERNAL_API_SECRET?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
