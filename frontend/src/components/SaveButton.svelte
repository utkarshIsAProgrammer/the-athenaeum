<script lang="ts">
  import type { Article } from "../lib/types";

  interface Props {
    article: Article;
    initialSaved?: boolean;
    /** On the bookmarks screen, un-saving should drop the card it lives in. */
    onUnsave?: () => void;
  }

  let { article, initialSaved = false, onUnsave }: Props = $props();

  let saved = $state(initialSaved);
  let busy = $state(false);
  let error = $state<string | null>(null);
  /** A signed-out reader cannot save at all, so a link is more use than a message. */
  let needsAuth = $state(false);

  async function readError(response: Response): Promise<string> {
    const body = (await response.json().catch(() => null)) as { error?: unknown } | null;
    if (body && typeof body.error === "string" && body.error.trim()) return body.error;
    return `Could not save (${response.status}).`;
  }

  async function toggle() {
    if (busy) return;

    const next = !saved;
    saved = next; // optimistic — the round trip is a local SQLite write
    busy = true;
    error = null;
    needsAuth = false;

    try {
      const response = await fetch(next ? "/api/bookmarks" : `/api/bookmarks/${article.id}`, {
        method: next ? "POST" : "DELETE",
        headers: next ? { "content-type": "application/json" } : undefined,
        body: next ? JSON.stringify(article) : undefined,
      });

      if (!response.ok) {
        needsAuth = response.status === 401;
        throw new Error(await readError(response));
      }

      if (!next) onUnsave?.();
    } catch (cause) {
      saved = !next; // roll back
      error = cause instanceof Error ? cause.message : "Could not update bookmark.";
    } finally {
      busy = false;
    }
  }
</script>

<div class="save">
  <button
    class="save__btn"
    class:save__btn--on={saved}
    type="button"
    disabled={busy}
    aria-pressed={saved}
    onclick={toggle}
  >
    <svg
      viewBox="0 0 24 24"
      fill={saved ? "currentColor" : "none"}
      stroke="currentColor"
      stroke-width="1.7"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M7 4h10a1 1 0 0 1 1 1v15l-6-4-6 4V5a1 1 0 0 1 1-1" />
    </svg>
    <span>{saved ? "Saved" : "Save"}</span>
  </button>
  {#if needsAuth}
    <a class="save__signin" href="/account">Sign in to save</a>
  {:else if error}
    <span class="save__error" role="status">{error}</span>
  {/if}
</div>
