<script lang="ts">
  /**
   * The controls that sit above a story's body: type size, line width, and
   * read-aloud. Also the thing that decides a story has been read — reaching
   * the end of the article is the only honest signal for that, and it is
   * observed here because this island is already on the page.
   *
   * Preferences live in `localStorage` and are applied by setting two custom
   * properties on the root element, so the stylesheet keeps using the design
   * tokens rather than a second set of reader-only sizes.
   */
  interface Props {
    articleId: string;
    /** The story's body, read aloud verbatim. Empty when there is none. */
    text: string;
    /** Marking read needs an owner; a signed-out reader is never asked. */
    signedIn: boolean;
  }

  let { articleId, text, signedIn }: Props = $props();

  const SIZES = ["s", "m", "l"] as const;
  type Size = (typeof SIZES)[number];
  const MEASURES = ["narrow", "regular", "wide"] as const;
  type Measure = (typeof MEASURES)[number];

  const STORAGE_KEY = "athenaeum:reader";
  const SCALE: Record<Size, string> = { s: "0.92", m: "1", l: "1.15" };
  const WIDTH: Record<Measure, string> = {
    narrow: "calc(var(--measure) * 0.82)",
    regular: "var(--measure)",
    wide: "calc(var(--measure) * 1.2)",
  };
  const MEASURE_LABEL: Record<Measure, string> = {
    narrow: "Narrow",
    regular: "Standard",
    wide: "Wide",
  };

  let size = $state<Size>("m");
  let measure = $state<Measure>("regular");
  let speaking = $state(false);
  let paused = $state(false);
  // Starts false on both server and client, so hydration cannot mismatch; the
  // effect below is the only thing that turns it on.
  let canListen = $state(false);

  function apply() {
    const root = document.documentElement;
    root.style.setProperty("--reader-scale", SCALE[size]);
    root.style.setProperty("--reader-measure", WIDTH[measure]);
  }

  // Preferences first, then apply — declared in that order so the first paint
  // after hydration already carries the reader's own settings.
  $effect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as { size?: string; measure?: string };
      if (SIZES.includes(saved.size as Size)) size = saved.size as Size;
      if (MEASURES.includes(saved.measure as Measure)) measure = saved.measure as Measure;
    } catch {
      // Unreadable or hand-edited. The defaults are a fine place to start.
    }
  });

  $effect(() => {
    apply();
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ size, measure }));
    } catch {
      // Private mode. The choice holds for this page and then is gone.
    }
  });

  $effect(() => {
    canListen =
      typeof window !== "undefined" && "speechSynthesis" in window && text.trim().length > 0;
  });

  // Leaving the page mid-sentence should stop the voice with it.
  $effect(() => () => {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
  });

  /**
   * Mark the story read once the reader reaches its end.
   *
   * Intersection rather than a scroll handler: the marker only appears at the
   * foot of the article, so this fires for a story that was actually read to
   * the bottom and not for one that was opened and abandoned.
   */
  $effect(() => {
    if (!signedIn) return;
    const end = document.querySelector("[data-reader-end]");
    if (!end) return;

    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      void fetch(`/api/reading/${encodeURIComponent(articleId)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ read: true }),
      }).catch(() => {
        // The dim mark is cosmetic; losing it costs the reader nothing.
      });
    });

    observer.observe(end);
    return () => observer.disconnect();
  });

  /**
   * Long utterances are cut off in some browsers, so the story is spoken as a
   * queue of sentence-sized pieces instead of one request.
   */
  function chunk(source: string): string[] {
    const sentences = source
      .replace(/\s+/g, " ")
      .trim()
      .split(/(?<=[.!?])\s+/);
    const pieces: string[] = [];
    let current = "";
    for (const sentence of sentences) {
      if (current && current.length + sentence.length > 220) {
        pieces.push(current);
        current = "";
      }
      current = current ? `${current} ${sentence}` : sentence;
    }
    if (current) pieces.push(current);
    return pieces;
  }

  function speak() {
    if (!canListen) return;
    const synth = window.speechSynthesis;
    if (synth.speaking) synth.cancel();

    const pieces = chunk(text);
    pieces.forEach((piece, index) => {
      const utterance = new SpeechSynthesisUtterance(piece);
      if (index === pieces.length - 1) {
        utterance.onend = () => {
          speaking = false;
          paused = false;
        };
        utterance.onerror = () => {
          speaking = false;
          paused = false;
        };
      }
      synth.speak(utterance);
    });

    speaking = true;
    paused = false;
  }

  function pause() {
    window.speechSynthesis.pause();
    paused = true;
  }

  function resume() {
    window.speechSynthesis.resume();
    paused = false;
  }

  function stop() {
    window.speechSynthesis.cancel();
    speaking = false;
    paused = false;
  }

  function step(delta: number) {
    const index = SIZES.indexOf(size) + delta;
    if (index < 0 || index >= SIZES.length) return;
    size = SIZES[index];
  }
</script>

<div class="reader-tools" role="group" aria-label="Reading controls">
  <div class="reader-tools__group" role="group" aria-label="Text size">
    <button
      class="reader-tools__btn"
      type="button"
      disabled={size === "s"}
      aria-label="Smaller text"
      onclick={() => step(-1)}>A−</button
    >
    <button
      class="reader-tools__btn"
      type="button"
      disabled={size === "l"}
      aria-label="Larger text"
      onclick={() => step(1)}>A+</button
    >
  </div>

  <div class="reader-tools__group" role="group" aria-label="Line width">
    {#each MEASURES as option (option)}
      <button
        class="reader-tools__btn"
        class:reader-tools__btn--on={measure === option}
        type="button"
        aria-pressed={measure === option}
        onclick={() => (measure = option)}>{MEASURE_LABEL[option]}</button
      >
    {/each}
  </div>

  {#if canListen}
    <div class="reader-tools__group" role="group" aria-label="Read aloud">
      {#if !speaking}
        <button class="reader-tools__btn" type="button" onclick={speak}>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M5 9v6h3l4 3V6L8 9H5Z" />
            <path d="M16 9.5a3.5 3.5 0 0 1 0 5" />
          </svg>
          Listen
        </button>
      {:else if paused}
        <button class="reader-tools__btn" type="button" onclick={resume}>Resume</button>
      {:else}
        <button class="reader-tools__btn" type="button" onclick={pause}>Pause</button>
      {/if}
      {#if speaking}
        <button class="reader-tools__btn" type="button" onclick={stop}>Stop</button>
      {/if}
    </div>
  {/if}
</div>
