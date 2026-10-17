<script lang="ts">
  import { fullDate, readerHref, shortDateUtc } from "../lib/format";
  import type { Article, StoryCluster } from "../lib/types";
  import SaveButton from "./SaveButton.svelte";

  interface Props {
    article: Article;
    saved?: boolean;
    /** Opened and read to the end — the card is dimmed rather than hidden. */
    read?: boolean;
    featured?: boolean;
    level?: 2 | 3;
    /** On the bookmarks screen, un-saving should drop the card it lives in. */
    onUnsave?: () => void;
    /** Who else ran this story, when the backend folded several copies into it. */
    cluster?: StoryCluster | null;
  }

  let {
    article,
    saved = false,
    read = false,
    featured = false,
    level = 3,
    onUnsave,
    cluster = null,
  }: Props = $props();

  // Every card leads into the reader; the outbound link to the newsroom lives
  // at the foot of that page.
  const href = readerHref(article.id);
  const excerpt = article.description?.trim() ?? "";
  const published = article.published_at;
  // `cluster.sources` includes the lead's own outlet.
  const others = (cluster?.sources ?? []).filter((name) => name !== article.source);
  // `svelte:element` takes a tag name, not a heading rank.
  const heading = $derived(level === 2 ? "h2" : "h3");
</script>

<article class="card" class:card--featured={featured} class:card--read={read}>
  {#if article.image_url}
    <a class="card__media" {href} tabindex="-1" aria-hidden="true">
      <img src={article.image_url} alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" />
    </a>
  {/if}

  <div class="card__body">
    <div class="card__meta">
      {#if article.source}<span class="card__source">{article.source}</span>{/if}
      <time datetime={published} data-relative title={fullDate(published)}>
        {shortDateUtc(published)}
      </time>
      {#if article.sentiment}<span class="chip chip--tiny">{article.sentiment}</span>{/if}
      {#if read}<span class="chip chip--tiny">Read</span>{/if}
    </div>

    <!-- The name is shared with the reader headline, so the title morphs. -->
    <svelte:element this={heading} class="card__title">
      <a {href} style={`view-transition-name: article-title-${article.id}`}>{article.title}</a>
    </svelte:element>

    {#if excerpt}<p class="card__excerpt">{excerpt}</p>{/if}

    {#if others.length > 0}
      <p class="card__cluster">Also covered by <em>{others.join(", ")}</em></p>
    {/if}

    <div class="card__foot">
      <SaveButton {article} initialSaved={saved} {onUnsave} />
    </div>
  </div>
</article>
