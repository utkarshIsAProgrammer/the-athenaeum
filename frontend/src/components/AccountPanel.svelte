<script lang="ts">
  import { signIn, signOut, signUp } from "../lib/auth";
  import { KEY_PAGES, removeAiKey, saveAiKey } from "../lib/ai";
  import type { AiKeyState, User } from "../lib/types";

  interface Props {
    user: User | null;
    /** Formatted on the server so the date cannot shift at hydration. */
    memberSince?: string | null;
    /** Rendered on the server, so a saved key is visible before any script runs. */
    keys?: AiKeyState[];
  }

  let { user, memberSince = null, keys = [] }: Props = $props();

  let mode = $state<"signin" | "register">("signin");
  let email = $state("");
  let password = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const isRegister = $derived(mode === "register");

  // ---------------------------------------------------------------- AI keys

  let aiKeys = $state<AiKeyState[]>(keys);
  let drafts = $state<Record<string, string>>({});
  /** The provider currently being saved or removed, if any. */
  let keyBusy = $state<string | null>(null);
  let keyError = $state<string | null>(null);
  let keyNotice = $state<string | null>(null);

  function labelFor(provider: string): string {
    return aiKeys.find((entry) => entry.provider === provider)?.label ?? provider;
  }

  async function submitKey(event: SubmitEvent, provider: string) {
    event.preventDefault();
    if (keyBusy) return;

    const key = (drafts[provider] ?? "").trim();
    // The server bounds this too; catching it here saves a round trip.
    if (key.length < 8) {
      keyError = "That does not look like a key.";
      keyNotice = null;
      return;
    }

    keyBusy = provider;
    keyError = null;
    keyNotice = null;

    const outcome = await saveAiKey(provider, key);
    keyBusy = null;

    if (!outcome.ok) {
      keyError = outcome.error;
      return;
    }

    drafts[provider] = "";
    aiKeys = aiKeys.map((entry) => (entry.provider === provider ? outcome.state : entry));
    keyNotice = `${outcome.state.label} is ready. Summaries will use it from now on.`;
  }

  async function dropKey(provider: string) {
    if (keyBusy) return;

    keyBusy = provider;
    keyError = null;
    keyNotice = null;

    const result = await removeAiKey(provider);
    keyBusy = null;

    if (!result.ok) {
      keyError = result.error ?? "Could not remove that key.";
      return;
    }

    aiKeys = aiKeys.map((entry) =>
      entry.provider === provider ? { ...entry, configured: false, hint: null } : entry,
    );
    keyNotice = `${labelFor(provider)} was removed.`;
  }

  // ------------------------------------------------------------- credential

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;

    busy = true;
    error = null;

    const result = isRegister
      ? await signUp(email, password)
      : await signIn(email, password);

    if (!result.ok) {
      error = result.error ?? "Something went wrong.";
      busy = false;
      return;
    }

    // A full reload rather than a client-side nav: the header, the tab bar and
    // every save button on the page were rendered on the server as anonymous.
    location.assign("/account");
  }

  async function leave() {
    if (busy) return;
    busy = true;
    error = null;

    const result = await signOut();
    if (!result.ok) {
      error = result.error ?? "Could not sign out.";
      busy = false;
      return;
    }
    location.assign("/");
  }
</script>

{#if user}
  <div class="auth">
    <p class="auth__eyebrow">Signed in as</p>
    <p class="auth__email">{user.email}</p>
    {#if memberSince}
      <p class="auth__meta">Reading since {memberSince}</p>
    {/if}

    <p class="auth__blurb">
      Bookmarks are kept against this account, so they follow you to any device you sign in
      from.
    </p>

    <section class="keys" aria-labelledby="keys-title">
      <h2 class="keys__title" id="keys-title">AI keys</h2>
      <p class="keys__blurb">
        Summaries run on a key of your own, so nothing here is billed to anyone else. Free tiers:
        {#each aiKeys as entry, index}{index > 0 ? ", " : ""}<a
            class="keys__link"
            href={KEY_PAGES[entry.provider]}
            target="_blank"
            rel="noopener noreferrer">{entry.label}</a
          >{/each}. A key is encrypted before it is stored, never sent back, and never used unless
        you press Summarise.
      </p>

      {#each aiKeys as entry (entry.provider)}
        <div class="keys__row">
          <div class="keys__head">
            <span class="keys__name">{entry.label}</span>
            {#if entry.configured}
              <span class="keys__state">Saved {entry.hint ?? ""}</span>
            {:else}
              <span class="keys__state keys__state--off">Not set</span>
            {/if}
          </div>

          <div class="keys__controls">
            <form class="keys__form" onsubmit={(event) => submitKey(event, entry.provider)}>
              <input
                class="keys__input"
                type="password"
                name={`key-${entry.provider}`}
                autocomplete="off"
                spellcheck="false"
                aria-label={`${entry.label} API key`}
                value={drafts[entry.provider] ?? ""}
                oninput={(event) => (drafts[entry.provider] = event.currentTarget.value)}
              />
              <button class="keys__save" type="submit" disabled={keyBusy === entry.provider}>
                {keyBusy === entry.provider ? "Saving…" : entry.configured ? "Replace" : "Save"}
              </button>
            </form>

            {#if entry.configured}
              <button
                class="keys__remove"
                type="button"
                disabled={keyBusy === entry.provider}
                onclick={() => dropKey(entry.provider)}
              >
                Remove
              </button>
            {/if}
          </div>
        </div>
      {/each}

      {#if keyError}
        <p class="auth__error" role="alert">{keyError}</p>
      {:else if keyNotice}
        <p class="keys__notice" role="status">{keyNotice}</p>
      {/if}
    </section>

    <div class="auth__actions">
      <button class="auth__submit" type="button" disabled={busy} onclick={leave}>
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </div>

    {#if error}<p class="auth__error" role="alert">{error}</p>{/if}
  </div>
{:else}
  <div class="auth">
    <div class="auth__tabs" role="tablist" aria-label="Account">
      <button
        class="auth__tab"
        class:auth__tab--on={!isRegister}
        type="button"
        role="tab"
        aria-selected={!isRegister}
        onclick={() => {
          mode = "signin";
          error = null;
        }}>Sign in</button
      >
      <button
        class="auth__tab"
        class:auth__tab--on={isRegister}
        type="button"
        role="tab"
        aria-selected={isRegister}
        onclick={() => {
          mode = "register";
          error = null;
        }}>Create an account</button
      >
    </div>

    <form class="auth__form" onsubmit={submit}>
      <label class="auth__field">
        <span class="auth__label">Email</span>
        <input
          class="auth__input"
          type="email"
          name="email"
          autocomplete="email"
          required
          bind:value={email}
        />
      </label>

      <label class="auth__field">
        <span class="auth__label">Password</span>
        <input
          class="auth__input"
          type="password"
          name="password"
          autocomplete={isRegister ? "new-password" : "current-password"}
          minlength={isRegister ? 8 : undefined}
          required
          bind:value={password}
        />
      </label>

      {#if isRegister}
        <p class="auth__meta">At least eight characters. Nothing else is asked of you.</p>
      {/if}

      {#if error}<p class="auth__error" role="alert">{error}</p>{/if}

      <button class="auth__submit" type="submit" disabled={busy}>
        {busy ? "One moment…" : isRegister ? "Create account" : "Sign in"}
      </button>
    </form>

    <p class="auth__blurb">
      An account keeps your bookmarks and your own AI key in one place, on any device you sign in
      from. No email is sent and no profile is built.
    </p>
  </div>
{/if}
