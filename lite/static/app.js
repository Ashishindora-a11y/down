/* Clipfetch - minimal download page.
 *
 * One interaction: paste a link, press Download, get a file.
 *
 * It resolves metadata first even though nothing is displayed. That call is
 * cheap and server-side cached, and it buys a real error message ("clip is too
 * long", "needs sign-in") instead of dumping the browser on a JSON error page
 * after the user has already committed to a download.
 */
(function () {
  "use strict";

  var CFG = JSON.parse(document.getElementById("cfg").textContent);
  var KEY_STORE = "clipfetch.key";

  var form = document.getElementById("form");
  var urlInput = document.getElementById("url");
  var button = document.getElementById("go");
  var statusEl = document.getElementById("status");
  var quality = document.getElementById("quality");
  var keyInput = document.getElementById("api-key");

  // --- key persistence ----------------------------------------------------

  if (keyInput) {
    try {
      keyInput.value = localStorage.getItem(KEY_STORE) || "";
    } catch (e) { /* storage blocked */ }
    keyInput.addEventListener("change", function () {
      try {
        localStorage.setItem(KEY_STORE, keyInput.value.trim());
      } catch (e) { /* ignore */ }
    });
  }

  function key() {
    return keyInput ? keyInput.value.trim() : "";
  }

  // --- helpers ------------------------------------------------------------

  function setStatus(text, kind, detail) {
    statusEl.className = "status" + (kind ? " " + kind : "");
    statusEl.textContent = text || "";
    if (detail) {
      var small = document.createElement("span");
      small.className = "status-detail";
      small.textContent = detail;
      statusEl.appendChild(small);
    }
  }

  function busy(on) {
    button.disabled = on;
    button.classList.toggle("busy", on);
  }

  function mode() {
    var picked = document.querySelector('input[name="mode"]:checked');
    return picked ? picked.value : "video";
  }

  function params(extra) {
    var p = new URLSearchParams();
    p.set("url", urlInput.value.trim());
    p.set("mode", mode());
    if (mode() === "video") p.set("quality", quality.value);
    if (key()) p.set("key", key());
    Object.keys(extra || {}).forEach(function (k) { p.set(k, extra[k]); });
    return p;
  }

  /** Trigger a browser download without navigating away from the page. */
  function fetchFile(href, filename) {
    var a = document.createElement("a");
    a.href = href;
    if (filename) a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  async function post(path, body) {
    var headers = { "Content-Type": "application/json" };
    if (key()) headers["X-API-Key"] = key();

    var response = await fetch(path, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body)
    });

    var payload = null;
    try {
      payload = await response.json();
    } catch (e) { /* non-JSON body */ }

    if (!response.ok) {
      var error = new Error("request_failed");
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function messageFor(error) {
    var detail = error.payload && error.payload.error;
    if (!detail) {
      if (error.status === 0 || error.message === "Failed to fetch") {
        return "Could not reach the server. Check your connection.";
      }
      return "Something went wrong (HTTP " + (error.status || "?") + ").";
    }

    // Plain-language rewrites for the cases a user can actually act on.
    switch (detail.code) {
      case "unauthorized":
        return "Access key missing or wrong.";
      case "limit_exceeded":
        return detail.message;
      case "blocked_by_platform":
        return "The platform is blocking this server, not you. It wants proof the " +
               "request is not automated.";
      case "age_restricted":
        return "This one is age-restricted and needs a signed-in session.";
      case "auth_required":
        return "The platform withheld this media. It is either private, or it is " +
               "refusing this server.";
      case "geo_blocked":
        return "Not available in this server's region.";
      case "rate_limited":
        return "Too many requests. Wait " + (detail.retry_after || 30) + "s and retry.";
      case "upstream_rate_limited":
        return "The platform is throttling this server. Wait a minute and retry.";
      case "server_busy":
        return "Server is busy. Try again in a moment.";
      case "unsupported_url":
        return "That link is not supported.";
      case "media_unavailable":
        return "That media is unavailable or was removed.";
      case "live_not_supported":
        return "Live streams are not supported.";
      case "deadline_exceeded":
        return "Took too long and was cancelled. Try a lower quality.";
      case "ffmpeg_missing":
        return "This file needs combining and the server cannot do it.";
      case "resolver_mode":
        return "This deployment cannot combine video and audio. Audio-only works.";
      default:
        return detail.message || "Could not fetch that link.";
    }
  }

  /**
   * The platform's own words, shown under the friendly message.
   *
   * Worth the extra line: a rewritten message alone cost real debugging time
   * when a bot-check was being reported as "private or age-restricted". Seeing
   * what the platform actually said is what makes the difference diagnosable.
   */
  function detailFor(error) {
    var detail = error.payload && error.payload.error;
    if (!detail || !detail.message) return "";
    var parts = [];
    if (detail.code) parts.push(detail.code);
    parts.push(detail.message);
    return parts.join(": ");
  }

  // --- submit -------------------------------------------------------------

  form.addEventListener("submit", async function (event) {
    event.preventDefault();

    var link = urlInput.value.trim();
    if (!link) {
      setStatus("Paste a link first.", "err");
      urlInput.focus();
      return;
    }
    if (CFG.authRequired && !key()) {
      setStatus("Enter your access key.", "err");
      if (keyInput) keyInput.focus();
      return;
    }

    busy(true);
    setStatus("Checking the link\u2026");

    try {
      // Validate and surface a useful error before committing to a transfer.
      var info = await post("/api/v1/info", { url: link, formats: false });
      if (info.within_limits === false) {
        setStatus(info.limit_note || "That media is outside this service's limits.", "err");
        return;
      }

      if (CFG.resolverOnly) {
        // No bytes may pass through this deployment, so hand the browser the
        // CDN URL instead. Works for single-file platforms; YouTube video
        // needs muxing and will report that clearly.
        var resolved = await post("/api/v1/url", {
          url: link,
          mode: mode(),
          quality: quality.value
        });
        if (resolved.delivery !== "single_file" || !resolved.file) {
          setStatus(
            "This platform serves video and audio separately, which this " +
            "deployment cannot combine. Audio-only works.",
            "err"
          );
          return;
        }
        fetchFile(resolved.file.url, resolved.file.suggested_filename);
        setStatus("Download started.", "ok");
        return;
      }

      setStatus("Preparing your file\u2026");
      fetchFile("/api/v1/download?" + params().toString());
      // The transfer now belongs to the browser, so this is the last word the
      // page can honestly say about it.
      setStatus("Download started. Large files take a few seconds to appear.", "ok");
    } catch (error) {
      setStatus(messageFor(error), "err", detailFor(error));
    } finally {
      busy(false);
    }
  });

  // Audio has no quality choice to make.
  Array.prototype.forEach.call(
    document.querySelectorAll('input[name="mode"]'),
    function (radio) {
      radio.addEventListener("change", function () {
        quality.disabled = mode() === "audio";
        quality.style.opacity = quality.disabled ? "0.45" : "";
      });
    }
  );

  urlInput.addEventListener("input", function () {
    if (statusEl.textContent) setStatus("");
  });

  urlInput.focus();
})();
