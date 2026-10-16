import { higgsfield, type V2Response } from "@higgsfield/client/v2";

const IMAGE_MODEL = "z-image/turbo";
const VIDEO_MODEL = "kling-video/v2.5-turbo/standard/image-to-video";
const FAIL = "Generation failed. Try Again!";

export type GenerationResult = {
  outputUrl: string | null;
  error: string | null;
};

function outpurUrl(res: V2Response): string | null {
  return res.images?.[0]?.url || res.video?.url || null;
}

export async function generateImage(prompt: string): Promise<GenerationResult> {
  try {
    const res = await higgsfield.subscribe(IMAGE_MODEL, {
      input: {
        prompt,
        image_url: "https://example.com/first-frame.jpg",
      },
      withPolling: true,
    });
    const url = outpurUrl(res);
    return url
      ? { outputUrl: url, error: null }
      : { outputUrl: null, error: FAIL };
  } catch (err) {
    console.log(`Error: ${err}`);
    return { outputUrl: null, error: FAIL };
  }
}

export async function generateVideo(
  prompt: string,
  imageUrl: string,
): Promise<GenerationResult> {
  try {
    const res = await higgsfield.subscribe(VIDEO_MODEL, {
      input: {
        prompt,
        image_url: imageUrl,
      },
      withPolling: true,
    });
    const url = outpurUrl(res);
    return url
      ? { outputUrl: url, error: null }
      : { outputUrl: null, error: FAIL };
  } catch (err) {
    console.log(`Error: ${err}`);
    return { outputUrl: null, error: FAIL };
  }
}
