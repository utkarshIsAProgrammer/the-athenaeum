/** Shapes mirroring the FastAPI schemas — the contract between the two halves. */

export interface Article {
  id: string;
  title: string;
  description: string | null;
  url: string;
  image_url: string | null;
  source: string | null;
  source_icon: string | null;
  author: string | null;
  published_at: string;
  categories: string[];
  countries: string[];
  sentiment: string | null;
  provider: string;
}

/** Reader mode. Only FreeNewsApi supplies a `body`; the rest arrive without. */
export interface ArticleDetail extends Article {
  body: string | null;
  incipit: string | null;
  authors: string[];
  topics: string[];
}

export interface Bookmark extends Article {
  bookmarked_at: string;
}

export interface QuotaState {
  provider: string;
  used: number;
  limit: number;
  remaining: number;
}

/**
 * The other outlets that ran a lead story's story, folded onto it by the
 * backend's clusterer. `sources` includes the lead's own outlet.
 */
export interface StoryCluster {
  lead_id: string;
  count: number;
  sources: string[];
  titles: string[];
}

export interface FeedMeta {
  count: number;
  cached: boolean;
  stale: boolean;
  providers: string[];
  quotas: QuotaState[];
  next_cursor: string | null;
  has_more: boolean;
  clusters: StoryCluster[];
}

export interface FeedResponse {
  articles: Article[];
  meta: FeedMeta;
}

export interface BookmarksResponse {
  bookmarks: Bookmark[];
}

/** A registered reader. The password hash never leaves the database. */
export interface User {
  id: string;
  email: string;
  created_at: string;
}

/** `user` is null when signed out — that is a 200, not an error. */
export interface MeResponse {
  user: User | null;
}

/** A story condensed by the reader's own key. */
export interface Summary {
  text: string;
  bullets: string[];
}

/** `hint` is the last four characters; the key itself never leaves the server. */
export interface AiKeyState {
  provider: string;
  label: string;
  configured: boolean;
  hint: string | null;
}

export interface AiKeysResponse {
  keys: AiKeyState[];
}

export interface SummaryResponse {
  provider: string;
  label: string;
  summary: Summary;
  cached: boolean;
}

/** One story a reader has opened. `article` is null only if the stored copy aged out. */
export interface ReadingEntry {
  article: Article | null;
  article_id: string;
  opened_at: string;
  read: boolean;
}

export interface ReadingResponse {
  entries: ReadingEntry[];
  read_ids: string[];
}

export interface Category {
  slug: string;
  label: string;
}

export interface CategoriesResponse {
  categories: Category[];
}

export interface TrendingStory {
  id: string;
  title: string;
  url: string;
  source: string | null;
  image_url: string | null;
  published_at: string;
  sources: string[];
}

export interface TrendingTopic {
  slug: string;
  label: string;
  count: number;
}

export interface TrendingResponse {
  stories: TrendingStory[];
  topics: TrendingTopic[];
}

/** Used when the API is unreachable, so the shell still navigates. */
export const FALLBACK_CATEGORIES: Category[] = [
  { slug: "top", label: "Top Stories" },
  { slug: "business", label: "Business" },
  { slug: "technology", label: "Technology" },
  { slug: "science", label: "Science" },
  { slug: "health", label: "Health" },
  { slug: "sports", label: "Sports" },
  { slug: "entertainment", label: "Entertainment" },
  { slug: "politics", label: "Politics" },
  { slug: "world", label: "World" },
  { slug: "environment", label: "Environment" },
];
