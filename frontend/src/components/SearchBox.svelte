<script lang="ts">
  interface Props {
    initialQuery?: string;
  }

  let { initialQuery = "" }: Props = $props();
  let value = $state(initialQuery);
</script>

<form class="searchbar" method="get" action="/search" role="search">
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.6-3.6" />
  </svg>
  <label class="sr-only" for="search-q">Search headlines</label>
  <input
    id="search-q"
    name="q"
    type="search"
    bind:value
    placeholder="Search headlines — climate, elections, AI"
    autocomplete="off"
    enterkeyhint="search"
    minlength="2"
  />
  {#if value !== ""}
    <button
      class="searchbar__clear"
      type="button"
      aria-label="Clear search"
      onclick={() => {
        value = "";
      }}
    >
      ✕
    </button>
  {/if}
</form>
