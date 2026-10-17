const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * Deterministic date used for the server-rendered pass. Locale and time zone
 * are pinned so the client's hydration output matches the server's exactly.
 */
export function shortDateUtc(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(date);
}

export function fullDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

/** Client-only: replaces `shortDateUtc` once the component has mounted. */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";

  const diff = Date.now() - then;
  if (diff < 0) return "just now";
  if (diff < MINUTE) return "just now";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)}d ago`;
  return shortDateUtc(iso);
}

export function providerLabel(provider: string): string {
  if (provider === "newsapi") return "NewsAPI";
  if (provider === "newsdata") return "NewsData";
  if (provider === "freenewsapi") return "FreeNewsApi";
  return provider;
}

/** Every card leads into the reader, which is where the outbound link lives. */
export function readerHref(articleId: string): string {
  return `/article/${encodeURIComponent(articleId)}`;
}

const SENTENCE = /(?<=[.!?])\s+/;
const SENTENCES_PER_PARAGRAPH = 3;

/**
 * Split an article body into paragraphs.
 *
 * FreeNewsApi returns a body as one unbroken run of prose — no newlines at all
 * — so explicit breaks are trusted when present and sentences are otherwise
 * grouped into readable paragraphs.
 */
export function paragraphs(body: string): string[] {
  const text = body.replace(/\r\n?/g, "\n").trim();
  if (!text) return [];

  const explicit = text
    .split(/\n\s*\n+/)
    .map((block) => block.replace(/\s*\n\s*/g, " ").trim())
    .filter(Boolean);
  if (explicit.length > 1) return explicit;

  const sentences = text
    .replace(/\s+/g, " ")
    .split(SENTENCE)
    .map((sentence) => sentence.trim())
    .filter(Boolean);

  const grouped: string[] = [];
  for (let i = 0; i < sentences.length; i += SENTENCES_PER_PARAGRAPH) {
    grouped.push(sentences.slice(i, i + SENTENCES_PER_PARAGRAPH).join(" "));
  }
  return grouped;
}
