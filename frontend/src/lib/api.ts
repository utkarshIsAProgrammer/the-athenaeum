import type {
  AiKeysResponse,
  Article,
  ArticleDetail,
  BookmarksResponse,
  Category,
  FeedResponse,
  MeResponse,
  ReadingResponse,
  TrendingResponse,
} from "./types";
import { FALLBACK_CATEGORIES } from "./types";

/**
 * Origin of the FastAPI service. Only ever used by the Astro server — the
 * browser talks to `/api/*` on this same origin, which the catch-all route in
 * `src/pages/api/[...path].ts` forwards on.
 */
const API_ORIGIN = (import.meta.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

export interface ApiResult<T> {
  data: T | null;
  error: string | null;
}

type QueryValue = string | number | undefined | null;

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(path, `${API_ORIGIN}/`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

/** The API always fails with `{"error": "..."}`; fall back to the status code. */
export async function errorMessage(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as { error?: unknown } | null;
  if (body && typeof body.error === "string" && body.error.trim()) return body.error;
  return `Request failed (${response.status}).`;
}

/**
 * Never throws: a dead API should degrade into an empty page with a message,
 * not a 500 from the Astro server.
 *
 * `cookie` is the reader's incoming `cookie` header. These calls go straight to
 * the API rather than through the `/api/*` proxy, so the browser cannot attach
 * it for us — without it every server render is anonymous.
 */
export async function apiGet<T>(
  path: string,
  query?: Record<string, QueryValue>,
  cookie = "",
): Promise<ApiResult<T>> {
  try {
    const headers: Record<string, string> = { accept: "application/json" };
    if (cookie) headers.cookie = cookie;

    const response = await fetch(buildUrl(path, query), { headers });
    if (!response.ok) return { data: null, error: await errorMessage(response) };
    return { data: (await response.json()) as T, error: null };
  } catch (cause) {
    return {
      data: null,
      error: cause instanceof Error ? cause.message : "The news service is unreachable.",
    };
  }
}

export async function getCategories(): Promise<Category[]> {
  const { data } = await apiGet<{ categories: Category[] }>("/api/categories");
  return data?.categories?.length ? data.categories : FALLBACK_CATEGORIES;
}

export function getFeed(query: {
  category?: string;
  q?: string;
  limit?: number;
  cursor?: string | null;
}) {
  return apiGet<FeedResponse>("/api/feed", query);
}

export function getSearchResults(q: string, limit = 40) {
  return apiGet<FeedResponse>("/api/search", { q, limit });
}

export function getBookmarks(cookie = "") {
  return apiGet<BookmarksResponse>("/api/bookmarks", undefined, cookie);
}

/** Who is signed in, or nobody. Answers 200 either way. */
export function getCurrentUser(cookie = "") {
  return apiGet<MeResponse>("/api/auth/me", undefined, cookie);
}

/** Which AI providers the reader has a key for. 401 when signed out. */
export function getAiKeys(cookie = "") {
  return apiGet<AiKeysResponse>("/api/ai/keys", undefined, cookie);
}

export function getTrending(limit = 12) {
  return apiGet<TrendingResponse>("/api/trending", { limit });
}

/** Reader mode. A 404 means the story aged out of the store. */
export function getArticle(articleId: string) {
  return apiGet<ArticleDetail>(`/api/articles/${encodeURIComponent(articleId)}`);
}

/** Ids are needed to render every save button in its correct initial state. */
export async function getSavedIds(cookie = ""): Promise<Set<string>> {
  const { data } = await getBookmarks(cookie);
  return new Set((data?.bookmarks ?? []).map((bookmark) => bookmark.id));
}

/** What this reader has opened, plus the ids a feed should dim. */
export function getReading(cookie = "") {
  return apiGet<ReadingResponse>("/api/reading", undefined, cookie);
}

/**
 * Note that a story was just opened, so it turns up in the resume rail.
 *
 * Sent from the Astro server rather than the browser: the reader cannot be
 * asked to click "I am reading this", and a server-side write means the rail
 * fills up even with JavaScript off. Failure is ignored — a reader who cannot
 * be recorded should still be able to read.
 */
export async function recordReading(article: Article, cookie = ""): Promise<void> {
  try {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (cookie) headers.cookie = cookie;
    await fetch(buildUrl("/api/reading"), {
      method: "POST",
      headers,
      body: JSON.stringify(article),
    });
  } catch {
    // The reading room does not depend on this.
  }
}
