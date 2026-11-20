/* DASH/MSE bridge for a caller-supplied, explicitly allowed source. */
(function () {
  const DASH_JS_URL = "https://cdn.dashjs.org/v5.3.0/modern/umd/dash.all.min.js";
  let dashLibraryPromise;

  function loadDashLibrary() {
    if (window.dashjs?.MediaPlayer) return Promise.resolve(window.dashjs);
    if (dashLibraryPromise) return dashLibraryPromise;
    dashLibraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = DASH_JS_URL;
      script.async = true;
      script.onload = () => window.dashjs?.MediaPlayer
        ? resolve(window.dashjs) : reject(new Error("dash_library_invalid"));
      script.onerror = () => reject(new Error("dash_library_unavailable"));
      document.head.append(script);
    }).catch(error => {
      dashLibraryPromise = null;
      throw error;
    });
    return dashLibraryPromise;
  }

  function safePlaybackUrl(value) {
    try {
      const url = new URL(value, location.href);
      const loopback = ["localhost", "127.0.0.1", "::1"].includes(url.hostname.toLowerCase());
      const localSameOrigin = url.protocol === "http:" && loopback && url.origin === location.origin;
      return (url.protocol === "https:" || localSameOrigin) && !url.username && !url.password && !url.hash ? url : null;
    } catch { return null; }
  }

  class DashPlaybackAdapter {
    constructor(audio, onState = () => {}) {
      this.audio = audio;
      this.onState = onState;
      this.player = null;
      this.source = null;
      this.deniedRequest = false;
      this.destroyed = false;
    }

    async prepare(provider) {
      await this.destroy();
      this.destroyed = false;
      if (!provider || typeof provider.getPlaybackSource !== "function") {
        throw new Error("dash_source_provider_invalid");
      }
      this.onState("preparing");
      const source = await provider.getPlaybackSource();
      if (!source || typeof source.manifestUrl !== "string" || !Array.isArray(source.allowedOrigins)) {
        throw new Error("dash_source_descriptor_invalid");
      }
      const manifestUrl = safePlaybackUrl(source.manifestUrl);
      const allowedOrigins = new Set(source.allowedOrigins.map(value => {
        const origin = safePlaybackUrl(`${value}/`);
        if (!origin || origin.pathname !== "/") throw new Error("dash_source_origin_invalid");
        return origin.origin;
      }));
      if (!manifestUrl || !allowedOrigins.has(manifestUrl.origin)) {
        throw new Error("dash_manifest_origin_not_allowed");
      }
      this.source = { ...source, manifestUrl: manifestUrl.href, allowedOrigins };

      const response = await fetch(manifestUrl.href, {
        method: "GET", mode: "cors", credentials: "omit", cache: "no-store",
        redirect: "follow",
      });
      if (!response.ok) throw new Error(`dash_manifest_http_${response.status}`);
      const responseUrl = safePlaybackUrl(response.url);
      if (!responseUrl || !allowedOrigins.has(responseUrl.origin)) {
        throw new Error("dash_manifest_redirect_not_allowed");
      }
      const xml = new DOMParser().parseFromString(await response.text(), "application/xml");
      if (xml.querySelector("parsererror") || xml.documentElement.localName !== "MPD") {
        throw new Error("dash_manifest_invalid");
      }
      if (xml.getElementsByTagNameNS("*", "ContentProtection").length) {
        throw new Error("dash_drm_content_rejected");
      }
      const adaptationSets = [...xml.getElementsByTagNameNS("*", "AdaptationSet")];
      if (adaptationSets.some(set => (set.getAttribute("contentType") || "").toLowerCase() === "video"
          || (set.getAttribute("mimeType") || "").toLowerCase().startsWith("video/"))) {
        throw new Error("dash_video_representation_unsupported");
      }
      const audioRepresentations = adaptationSets
        .filter(set => (set.getAttribute("contentType") || "").toLowerCase() === "audio"
          || (set.getAttribute("mimeType") || "").toLowerCase().startsWith("audio/"))
        .flatMap(set => [...set.getElementsByTagNameNS("*", "Representation")].map(rep => ({
          mime: rep.getAttribute("mimeType") || set.getAttribute("mimeType") || "",
          codecs: rep.getAttribute("codecs") || set.getAttribute("codecs") || "",
        })));
      if (!audioRepresentations.length) throw new Error("dash_audio_representation_missing");
      const supportsAac = audioRepresentations.some(rep => rep.mime.toLowerCase() === "audio/mp4"
        && /(?:^|,)\s*mp4a\.40\./i.test(rep.codecs)
        && window.MediaSource?.isTypeSupported(`${rep.mime}; codecs="${rep.codecs}"`));
      if (!supportsAac) {
        throw new Error("dash_audio_aac_unsupported");
      }

      const dashjs = await loadDashLibrary();
      if (this.destroyed) return;
      if (!window.MediaSource) {
        throw new Error("dash_mse_aac_unsupported");
      }
      const player = dashjs.MediaPlayer().create();
      this.player = player;
      player.extend("RequestModifier", () => ({
        modifyRequestHeader: xhr => {
          xhr.withCredentials = false;
          return xhr;
        },
        modifyRequestURL: url => {
          const requested = safePlaybackUrl(url);
          if (!requested || !allowedOrigins.has(requested.origin)) {
            this.deniedRequest = true;
            this.onState("request_blocked");
            return "about:blank";
          }
          return requested.href;
        },
      }), true);
      player.updateSettings({
        debug: { logLevel: dashjs.Debug.LOG_LEVEL_NONE },
        streaming: { errors: { recoverAttempts: { mediaErrorDecode: 3 } } },
      });
      player.on(dashjs.MediaPlayer.events.PLAYBACK_METADATA_LOADED, () => {
        if (this.player !== player) return;
        this.onState("ready");
      });
      player.on(dashjs.MediaPlayer.events.PLAYBACK_WAITING, () => {
        if (this.player === player) this.onState("buffering");
      });
      player.on(dashjs.MediaPlayer.events.ERROR, event => {
        if (this.player === player) {
          const code = Number.isInteger(event?.error?.code) ? event.error.code : null;
          this.onState(this.deniedRequest ? "request_blocked" : "error", code);
        }
      });
      player.on(dashjs.MediaPlayer.events.PLAYBACK_ERROR, () => {
        if (this.player === player) this.onState("error");
      });
      player.initialize(this.audio, responseUrl.href, false);
      player.setAutoPlay(false);
      this.onState("loading");
    }

    seek(seconds) {
      if (!this.player || !Number.isFinite(seconds) || seconds < 0) return;
      this.player.seek(seconds);
    }

    recover() {
      if (!this.player || this.audio.paused || !Number.isFinite(this.audio.currentTime)) return false;
      this.player.seek(this.audio.currentTime);
      return true;
    }

    async destroy() {
      this.destroyed = true;
      if (this.player) {
        try { this.player.pause(); } catch { /* player may already be detached */ }
        try { this.player.destroy(); } catch { /* tolerate partial initialization */ }
        this.player = null;
      }
      this.source = null;
    }
  }

  // Public test provider. Other approved providers implement the same one-method contract.
  class OpenBbcDashAudioSource {
    async getPlaybackSource() {
      return {
        id: "bbc-rdmedia-radio-lc-en",
        title: "BBC R&D · AAC-LC radio sample",
        artist: "BBC R&D Testcard",
        language: "en",
        manifestUrl: "https://rdmedia.bbc.co.uk/testcard/vod/manifests/radio-lc-en.mpd",
        allowedOrigins: ["https://rdmedia.bbc.co.uk", "https://vod-dash-ww-rd-live.akamaized.net"],
      };
    }
  }

  window.MusicStreamDashPlaybackAdapter = DashPlaybackAdapter;
  window.MusicStreamOpenBbcDashAudioSource = OpenBbcDashAudioSource;
})();
