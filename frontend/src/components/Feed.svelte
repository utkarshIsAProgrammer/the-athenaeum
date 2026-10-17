<script lang="ts">
  import type { Article, FeedResponse, StoryCluster } from "../lib/types";
  import ArticleCard from "./ArticleCard.svelte";

  interface Props {
    /** The first page, resolved and rendered on the server. */
    articles: Article[];
    clusters: StoryCluster[];
    savedIds: string[];
    /** Already-read story ids, so a feed can dim them. */
    readIds?: string[];
    category: string;
    cursor: string | null;
    hasMore: boolean;
    pageSize?: number;
  }

  let {
    articles,
    clusters,
    savedIds,
    readIds = [],
    category,
    cursor,
    hasMore,
    pageSize = 30,
  }: Props = $props();

  const byLead = (list: StoryCluster[]): Record<string, StoryCluster> =>
    Object.fromEntries(list.map((cluster) => [cluster.lead_id, cluster]));

  let items = $state<Article[]>(articles);
  let notes = $state<Record<string, StoryCluster>>(byLead(clusters));
  let nextCursor = $state<string | null>(cursor);
  let more = $state(hasMore);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let sentinel = $state<HTMLElement | null>(null);

  const saved = new Set(savedIds);
  // Fixed for the life of this island: a story finished after this page was
  // rendered stays bright until the next navigation, which is less distracting
  // than a card dimming under the reader's eyes as they come back up.
  const read = new Set(readIds);

  async function readError(response: Response): Promise<string> {
    const body = (await response.json().catch(() => null)) as { error?: unknown } | null;
    if (body && typeof body.error === "string" && body.error.trim()) return body.error;
    return `Could not load more stories (${response.status}).`;
  }

  async function loadMore(): Promise<void> {
    if (loading || !more || error) return;

    loading = true;
    try {
      const params = new URLSearchParams({ category, limit: String(pageSize) });
      if (nextCursor) params.set("cursor", nextCursor);

      const response = await fetch(`/api/feed?${params}`, {
        headers: { accept: "application/json" },
      });
      if (!response.ok) throw new Error(await readError(response));

      const page = (await response.json()) as FeedResponse;
      const known = new Set(items.map((article) => article.id));
      const fresh = page.articles.filter((article) => !known.has(article.id));

      items = [...items, ...fresh];
      notes = { ...notes, ...byLead(page.meta.clusters) };
      nextCursor = page.meta.next_cursor;
      // A page that added nothing new would be asked for on every scroll, so
      // running into one is treated as the end of the feed.
      more = page.meta.has_more && fresh.length > 0;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : "Could not load more stories.";
    } finally {
      loading = false;
    }
  }

  // Unobserved on the server, so the first page needs no JavaScript at all.
  $effect(() => {
    if (!sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMore();
      },
      { rootMargin: "600px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  });
</script>

<div class="stack">
  {#each items as article, index (article.id)}
    <ArticleCard
      {article}
      saved={saved.has(article.id)}
      read={read.has(article.id)}
      cluster={notes[article.id] ?? null}
      featured={index === 0}
      level={index === 0 ? 2 : 3}
    />
  {/each}
</div>

{#if more || loading || error}
  <div class="more" bind:this={sentinel}>
    {#if error}
      <span>{error}</span>
      <button
        class="more__retry"
        type="button"
        onclick={() => {
          error = null;
          void loadMore();
        }}>Try again</button
      >
    {:else}
      <span class="more__spinner" aria-hidden="true"></span>
      <span role="status">Loading more stories</span>
    {/if}
  </div>
{/if}
