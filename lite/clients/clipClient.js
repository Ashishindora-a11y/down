/**
 * Node.js client for the Clip Download API (Lite).
 *
 * Zero dependencies: uses the global fetch and the web streams API from
 * Node 18+. Drop it into the service that consumes the API (for example your
 * Google Cloud app).
 *
 *   const { ClipClient } = require("./clipClient");
 *
 *   const client = new ClipClient("https://clip-api-xxxx.run.app", {
 *     apiKey: process.env.CLIP_API_KEY,
 *   });
 *
 *   const meta = await client.info("https://youtube.com/shorts/VIDEO_ID");
 *   await client.downloadDirect("https://youtube.com/shorts/VIDEO_ID", "clip.mp4");
 *
 * Prefer downloadDirect: it resolves the CDN URL and pulls the bytes from the
 * CDN, so nothing transits the API service and its request timeout is
 * irrelevant. It falls back to the proxy path when the CDN refuses this host,
 * which happens because media URLs are often bound to the IP that resolved
 * them and expire within minutes to hours.
 *
 * For a private Cloud Run service, pass `idToken`. Cloud Run consumes the
 * Authorization header for IAM, so the application key travels separately in
 * X-API-Key and the two do not collide.
 */

"use strict";

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { Readable } = require("node:stream");
const { pipeline } = require("node:stream/promises");

const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 504]);

class ClipApiError extends Error {
  constructor(status, code, message, options = {}) {
    let detail = `[${status} ${code}] ${message}`;
    if (options.hint) detail += ` | hint: ${options.hint}`;
    if (options.requestId) detail += ` | request_id: ${options.requestId}`;
    super(detail);
    this.name = "ClipApiError";
    this.status = status;
    this.code = code;
    this.apiMessage = message;
    this.hint = options.hint;
    this.requestId = options.requestId;
    this.retryAfter = options.retryAfter;
  }

  get retryable() {
    return RETRYABLE_STATUS.has(this.status);
  }

  /** True when the platform wants cookies or a different egress IP. */
  get needsCredentials() {
    return ["auth_required", "blocked_by_platform", "geo_blocked"].includes(this.code);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class ClipClient {
  /**
   * @param {string} baseUrl    Service base URL.
   * @param {object} [options]
   * @param {string} [options.apiKey]     Value from the service's API_KEYS.
   * @param {string} [options.idToken]    GCP identity token for a private Cloud Run service.
   * @param {number} [options.timeoutMs]  Per-request timeout. Default 300000.
   * @param {number} [options.maxRetries] Retries for retryable statuses. Default 3.
   */
  constructor(baseUrl, options = {}) {
    if (!baseUrl) throw new Error("baseUrl is required");
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.idToken = options.idToken;
    this.timeoutMs = options.timeoutMs ?? 300000;
    this.maxRetries = Math.max(0, options.maxRetries ?? 3);
  }

  headers(extra = {}) {
    const headers = { Accept: "application/json", ...extra };
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    if (this.idToken) headers.Authorization = `Bearer ${this.idToken}`;
    return headers;
  }

  async #raise(response) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      /* non-JSON error body */
    }
    const error = payload && payload.error;
    if (error) {
      throw new ClipApiError(response.status, error.code || "unknown", error.message || "request failed", {
        hint: error.hint,
        requestId: error.request_id || response.headers.get("x-request-id"),
        retryAfter: error.retry_after,
      });
    }
    throw new ClipApiError(response.status, "http_error", response.statusText || "request failed", {
      requestId: response.headers.get("x-request-id"),
    });
  }

  async #fetch(url, init = {}, { retry = true } = {}) {
    let lastError;
    for (let attempt = 0; attempt <= (retry ? this.maxRetries : 0); attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      let response;
      try {
        response = await fetch(url, { ...init, signal: controller.signal });
      } catch (cause) {
        clearTimeout(timer);
        lastError = new ClipApiError(0, cause.name === "AbortError" ? "timeout" : "network_error", String(cause.message || cause));
        if (attempt === (retry ? this.maxRetries : 0)) throw lastError;
        await sleep(Math.min(2 ** attempt, 30) * 1000 + Math.random() * 500);
        continue;
      }
      clearTimeout(timer);

      if (response.ok) return response;

      try {
        await this.#raise(response);
      } catch (error) {
        lastError = error;
        if (!error.retryable || attempt === (retry ? this.maxRetries : 0)) throw error;
        const waitSec = error.retryAfter || Math.min(2 ** attempt, 30);
        await sleep(waitSec * 1000 + Math.random() * 500);
      }
    }
    throw lastError;
  }

  // --- metadata -----------------------------------------------------------

  /** Service caps. Cheap and unauthenticated; worth caching at startup. */
  async limits() {
    const response = await this.#fetch(`${this.baseUrl}/api/v1/limits`, {
      headers: this.headers(),
    });
    return response.json();
  }

  /** Metadata and available formats. Downloads nothing. */
  async info(url, { formats = true } = {}) {
    const response = await this.#fetch(`${this.baseUrl}/api/v1/info`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url, formats }),
    });
    return response.json();
  }

  /** Resolve the CDN URL. No bytes move through the API service. */
  async directUrl(url, { mode = "video", quality = "best" } = {}) {
    const response = await this.#fetch(`${this.baseUrl}/api/v1/url`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url, mode, quality: String(quality) }),
    });
    return response.json();
  }

  // --- downloads ----------------------------------------------------------

  /** Open a relayed download and return the raw Response. */
  async openProxied(url, { mode = "video", quality = "best", via = "auto", audioFormat = "original", container = "auto" } = {}) {
    const params = new URLSearchParams({
      url,
      mode,
      quality: String(quality),
      via,
      audio_format: audioFormat,
      container,
    });
    return this.#fetch(
      `${this.baseUrl}/api/v1/download?${params}`,
      { headers: this.headers({ Accept: "*/*" }) },
      { retry: false } // a half-written body must not be silently restarted
    );
  }

  /** Relay through the API service and write to disk. */
  async downloadProxied(url, destination, options = {}) {
    const response = await this.openProxied(url, options);
    return writeBody(response, destination);
  }

  /**
   * Resolve the CDN URL, then fetch the bytes straight from the CDN.
   * Falls back to the proxy path when the CDN refuses this host.
   */
  async downloadDirect(url, destination, { mode = "video", quality = "best", fallbackToProxy = true } = {}) {
    const resolved = await this.directUrl(url, { mode, quality });

    if (resolved.delivery === "split_streams") {
      if (!fallbackToProxy) {
        throw new ClipApiError(422, "split_streams", "Video and audio are separate at this quality and must be muxed.", {
          hint: "Allow fallbackToProxy, or mux the two URLs with ffmpeg yourself.",
        });
      }
      return this.downloadProxied(url, destination, { mode, quality, via: "server" });
    }

    const descriptor = resolved.file || {};
    if (!descriptor.url) {
      throw new ClipApiError(502, "no_url", "The API returned no media URL.");
    }

    // Replay the headers the API supplied; many CDNs check Referer.
    const response = await fetch(descriptor.url, { headers: descriptor.http_headers || {} });
    if ([401, 403, 404].includes(response.status)) {
      if (!fallbackToProxy) {
        throw new ClipApiError(response.status, "cdn_refused", "The CDN refused this host. The URL is likely IP-locked or expired.");
      }
      return this.downloadProxied(url, destination, { mode, quality });
    }
    if (!response.ok) {
      throw new ClipApiError(response.status, "cdn_error", `CDN returned HTTP ${response.status}.`);
    }
    return writeBody(response, destination);
  }
}

/**
 * Stream a Response body to disk through a .part file, so a failed transfer
 * never leaves a truncated file that looks complete.
 */
async function writeBody(response, destination) {
  const target = path.resolve(destination);
  await fsp.mkdir(path.dirname(target), { recursive: true });
  const staging = `${target}.part`;

  try {
    if (!response.body) throw new ClipApiError(502, "empty_response", "Response carried no body.");
    await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(staging));
    const { size } = await fsp.stat(staging);
    if (size === 0) throw new ClipApiError(502, "empty_response", "Received zero bytes.");
    await fsp.rename(staging, target);
    return { path: target, bytes: size };
  } catch (error) {
    await fsp.rm(staging, { force: true });
    throw error;
  }
}

/**
 * Build a client for a private Cloud Run service.
 * Requires `npm i google-auth-library`, and roles/run.invoker on the target.
 */
async function forPrivateCloudRun(serviceUrl, apiKey, options = {}) {
  const { GoogleAuth } = require("google-auth-library");
  const auth = new GoogleAuth();
  const clientInstance = await auth.getIdTokenClient(serviceUrl);
  const headers = await clientInstance.getRequestHeaders();
  const idToken = String(headers.Authorization || headers.authorization || "").replace(/^Bearer\s+/i, "");
  return new ClipClient(serviceUrl, { ...options, apiKey, idToken });
}

module.exports = { ClipClient, ClipApiError, forPrivateCloudRun };
