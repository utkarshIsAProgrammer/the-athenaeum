<script lang="ts">
  import type { Bookmark } from "../lib/types";
  import ArticleCard from "./ArticleCard.svelte";

  interface Props {
    bookmarks: Bookmark[];
    /** Already-read story ids, so a saved story already finished reads as such. */
    readIds?: string[];
  }

  let { bookmarks, readIds = [] }: Props = $props();
  let items = $state<Bookmark[]>(bookmarks);
  const read = new Set(readIds);
</script>

{#if items.length > 0}
  <div class="stack">
    {#each items as bookmark (bookmark.id)}
      <ArticleCard
        article={bookmark}
        saved
        read={read.has(bookmark.id)}
        onUnsave={() => {
          items = items.filter((entry) => entry.id !== bookmark.id);
        }}
      />
    {/each}
  </div>
{:else}
  <div class="state">
    <p class="state__title">No bookmarks yet</p>
    <p class="state__hint">Tap Save on any story and it lands here.</p>
  </div>
{/if}
