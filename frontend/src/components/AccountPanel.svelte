<script lang="ts">
  import { issueRecoveryCode, recoverAccount, signIn, signOut, signUp } from "../lib/auth";
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

  let mode = $state<"signin" | "register" | "recover">("signin");
  let email = $state("");
  let password = $state("");
  /** What the reader typed off their piece of paper, in recovery mode. */
  let recoveryEntry = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);
  let notice = $state<string | null>(null);

  const isRegister = $derived(mode === "register");
  const isRecover = $derived(mode === "recover");

  // ----------------------------------------------------------- recovery code

  let recoveryCode = $state<string | null>(null);
  let codeBusy = $state(false);
  let codeError = $state<string | null>(null);

  async function generateCode() {
    if (codeBusy) return;

    codeBusy = true;
    codeError = null;

    const result = await issueRecoveryCode();
    codeBusy = false;

    if (!result.ok || !result.code) {
      codeError = result.error ?? "Could not generate a code.";
      return;
    }
    recoveryCode = result.code;
  }

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

  type Field = "email" | "password" | "recovery_code";

  /** The field the current message is about, so it can be marked and cleared. */
  let badField = $state<Field | null>(null);

  /**
   * What is wrong with the form, if anything.
   *
   * The forms carry `novalidate`, so the browser does not answer with a bubble
   * of its own: the wording here can match the rest of the page, and a reader
   * who is told to fix something is told it in the same voice everywhere.
   * Only the first problem is reported — the others are one keystroke behind.
   */
  function problem(): { field: Field; message: string } | null {
    const address = email.trim();
    if (!address) {
      return { field: "email", message: "Enter the email address for your account." };
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) {
      return { field: "email", message: "That does not look like an email address — check it for a typo." };
    }

    if (isRecover) {
      // Dashes and case are the server's business; length is the reader's.
      const typed = recoveryEntry.replace(/[^0-9a-z]/gi, "");
      if (!typed) {
        return { field: "recovery_code", message: "Enter the recovery code you wrote down." };
      }
      if (typed.length < 20) {
        return {
          field: "recovery_code",
          message: "A recovery code is twenty characters long. Check the one you wrote down.",
        };
      }
    }

    if (!password) {
      return {
        field: "password",
        message: isRegister ? "Choose a password." : isRecover ? "Choose the password you want next." : "Enter your password.",
      };
    }
    if ((isRegister || isRecover) && password.length < 8) {
      return { field: "password", message: "A password needs at least eight characters." };
    }

    return null;
  }

  /** Once they start fixing the field the message is about, it has served its purpose. */
  function cleared(field: Field) {
    if (badField === field) {
      badField = null;
      error = null;
    }
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;

    const complaint = problem();
    if (complaint) {
      error = complaint.message;
      badField = complaint.field;
      return;
    }

    busy = true;
    error = null;
    notice = null;
    badField = null;

    const result = isRecover
      ? await recoverAccount(email, recoveryEntry, password)
      : isRegister
        ? await signUp(email, password)
        : await signIn(email, password);

    if (!result.ok) {
      error = result.error ?? "Something went wrong.";
      busy = false;
      return;
    }

    // Recovering sets a password and signs every device out — including this
    // one — so the reader has to sign in with what they just chose. Swapping
    // the form is more use than reloading into the same locked door.
    if (isRecover) {
      mode = "signin";
      password = "";
      recoveryEntry = "";
      notice = "That password is set and every device has been signed out. Sign in with it.";
      busy = false;
      return;
    }

    // A full reload rather than a client-side nav: the header, the tab bar and
    // every save button on the page were rendered on the server as anonymous.
    location.assign("/account");
  }

  function showMode(next: "signin" | "register" | "recover") {
    mode = next;
    error = null;
    notice = null;
    // The mark belongs to the form that was on screen, not to the one arriving:
    // a field left red across a tab switch is a complaint about nothing.
    badField = null;
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

<!-- The complaint is drawn against the box it is about, so a reader is never
     sent to the foot of the form to find out which field it meant. -->
{#snippet fieldMessage(field: Field)}
  {#if badField === field && error}
    <span class="auth__message" role="alert">{error}</span>
  {/if}
{/snippet}

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
              <!-- The stored key's tail stays recognisable on hover, so the row
                   itself can just say whether there is one. -->
              <span class="keys__state" title={entry.hint ?? undefined}>Saved</span>
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

    <section class="keys" aria-labelledby="recovery-title">
      <h2 class="keys__title" id="recovery-title">Recovery code</h2>
      <p class="keys__blurb">
        No email is ever sent from here, so a code written down before you need it is the only way
        back into an account whose password is forgotten. Only a hash of it is kept, and generating
        a new one replaces the old.
      </p>

      {#if recoveryCode}
        <p class="codes__code">{recoveryCode}</p>
        <p class="keys__notice" role="status">
          Write it down now — this is the only time it is shown. Dashes and capitalisation do not
          matter when you use it.
        </p>
      {:else}
        <div class="keys__controls">
          <button class="keys__save" type="button" disabled={codeBusy} onclick={generateCode}>
            {codeBusy ? "Generating…" : "Generate a code"}
          </button>
        </div>
      {/if}

      {#if codeError}<p class="auth__error" role="alert">{codeError}</p>{/if}
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
        class:auth__tab--on={mode === "signin"}
        type="button"
        role="tab"
        aria-selected={mode === "signin"}
        onclick={() => showMode("signin")}>Sign in</button
      >
      <button
        class="auth__tab"
        class:auth__tab--on={isRegister}
        type="button"
        role="tab"
        aria-selected={isRegister}
        onclick={() => showMode("register")}>Create an account</button
      >
    </div>

    {#if isRecover}
      <form class="auth__form" novalidate onsubmit={submit}>
        <p class="auth__blurb">
          Enter the recovery code you wrote down, and choose the password you want from now on.
        </p>

        <label class="auth__field">
          <span class="auth__label">Email</span>
          <input
            class="auth__input"
            type="email"
            name="email"
            autocomplete="email"
            required
            aria-invalid={badField === "email"}
            bind:value={email}
            oninput={() => cleared("email")}
          />
          {@render fieldMessage("email")}
        </label>

        <label class="auth__field">
          <span class="auth__label">Recovery code</span>
          <input
            class="auth__input"
            type="text"
            name="recovery_code"
            autocomplete="off"
            spellcheck="false"
            placeholder="XXXX-XXXX-XXXX-XXXX-XXXX"
            required
            aria-invalid={badField === "recovery_code"}
            bind:value={recoveryEntry}
            oninput={() => cleared("recovery_code")}
          />
          {@render fieldMessage("recovery_code")}
        </label>

        <label class="auth__field">
          <span class="auth__label">New password</span>
          <input
            class="auth__input"
            type="password"
            name="password"
            autocomplete="new-password"
            required
            aria-invalid={badField === "password"}
            bind:value={password}
            oninput={() => cleared("password")}
          />
          {@render fieldMessage("password")}
        </label>

        <p class="auth__meta">
          At least eight characters. Using the code signs every device out, and the code is spent —
          generate a new one once you are back in.
        </p>

        {#if error && !badField}<p class="auth__error" role="alert">{error}</p>{/if}
        {#if notice}<p class="keys__notice" role="status">{notice}</p>{/if}

        <button class="auth__submit" type="submit" disabled={busy}>
          {busy ? "One moment…" : "Set the new password"}
        </button>

        <button class="auth__link" type="button" onclick={() => showMode("signin")}>
          Back to sign in
        </button>
      </form>
    {:else}
      <form class="auth__form" novalidate onsubmit={submit}>
        <label class="auth__field">
          <span class="auth__label">Email</span>
          <input
            class="auth__input"
            type="email"
            name="email"
            autocomplete="email"
            required
            aria-invalid={badField === "email"}
            bind:value={email}
            oninput={() => cleared("email")}
          />
          {@render fieldMessage("email")}
        </label>

        <label class="auth__field">
          <span class="auth__label">Password</span>
          <input
            class="auth__input"
            type="password"
            name="password"
            autocomplete={isRegister ? "new-password" : "current-password"}
            required
            aria-invalid={badField === "password"}
            bind:value={password}
            oninput={() => cleared("password")}
          />
          {@render fieldMessage("password")}
        </label>

        {#if isRegister}
          <p class="auth__meta">At least eight characters. Nothing else is asked of you.</p>
        {/if}

        {#if error && !badField}<p class="auth__error" role="alert">{error}</p>{/if}
        {#if notice}<p class="keys__notice" role="status">{notice}</p>{/if}

        <button class="auth__submit" type="submit" disabled={busy}>
          {busy ? "One moment…" : isRegister ? "Create account" : "Sign in"}
        </button>

        {#if !isRegister}
          <button class="auth__link" type="button" onclick={() => showMode("recover")}>
            Forgotten your password?
          </button>
        {/if}
      </form>
    {/if}

    <p class="auth__blurb">
      An account keeps your bookmarks and your own AI key in one place, on any device you sign in
      from. Nothing is ever sent to you: if you forget your password, a recovery code you generated
      beforehand is what gets you back in.
    </p>
  </div>
{/if}
