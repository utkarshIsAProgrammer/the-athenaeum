<script lang="ts">
  import { summarise } from "../lib/ai";
  import type { SummaryResponse } from "../lib/types";

  interface Props {
    articleId: string;
  }

  let { articleId }: Props = $props();

  let busy = $state(false);
  let result = $state<SummaryResponse | null>(null);
  let error = $state<string | null>(null);
  /** A signed-out reader needs a link, not a message they cannot act on. */
  let needsAuth = $state(false);
  /** Signed in, but no key stored yet. Also fixed on the account page. */
  let needsKey = $state(false);

  let controller: AbortController | null = null;

  // Nothing is generated on mount — the reader asks for this, and leaving the
  // page mid-summary should not leave the request running.
  $effect(() => () => controller?.abort());

  async function run() {
    if (busy) return;

    busy = true;
    error = null;
    needsAuth = false;
    needsKey = false;

    controller = new AbortController();
    try {
      const outcome = await summarise(articleId, controller.signal);
      if (outcome.ok && outcome.data) {
        result = outcome.data;
      } else {
        needsAuth = outcome.status === 401;
        needsKey = outcome.status === 409;
        error = outcome.error;
      }
    } catch {
      // Cancelled. Whatever was already on screen stays there, unsaid.
    } finally {
      controller = null;
      busy = false;
    }
  }

  function cancel() {
    controller?.abort();
  }
</script>

<div class="summarise">
  <div class="summarise__bar">
    {#if busy}
      <span class="summarise__busy">
        <span class="summarise__spinner" aria-hidden="true"></span>
        Writing a summary…
      </span>
      <button class="summarise__cancel" type="button" onclick={cancel}>Cancel</button>
    {:else}
      <button class="summarise__btn" type="button" onclick={run}>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M4 6h16M4 11h11M4 16h7" />
        </svg>
        <span>{result ? "Summarise again" : "Summarise this story"}</span>
      </button>
    {/if}
  </div>

  {#if needsAuth}
    <a class="summarise__link" href="/account">Sign in to summarise</a>
  {:else if needsKey}
    <a class="summarise__link" href="/account">Add a free AI key</a>
  {:else if error}
    <p class="summarise__error" role="alert">{error}</p>
  {/if}

  {#if result}
    <div class="summarise__body">
      <p class="summarise__eyebrow">
        <span>Summarised by {result.label}</span>
        {#if result.cached}
          <span class="dateline__sep" aria-hidden="true">·</span>
          <span>kept from an earlier reading</span>
        {/if}
      </p>
      <p class="summarise__text">{result.summary.text}</p>
      {#if result.summary.bullets.length > 0}
        <ul class="summarise__list">
          {#each result.summary.bullets as bullet}<li>{bullet}</li>{/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>
