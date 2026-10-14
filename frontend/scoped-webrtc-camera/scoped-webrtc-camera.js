import { SessionController } from "./session-controller.js";
import { createLifecycleRecorder } from "./lifecycle-trace.js";
export { readLifecycleTrace } from "./lifecycle-trace.js";

// Enable only after the backend pending-session cleanup prerequisite is accepted.
export const BACKEND_CLEANUP_VERIFIED = true;

export class ScopedWebRtcCamera extends HTMLElement {
  constructor() {
    super();
    this.recordLifecycle = createLifecycleRecorder();
    this.traceLifecycle("created");
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `<style>
      :host{display:block} article{border:1px solid var(--divider-color,#888);border-radius:12px;padding:16px;background:var(--ha-card-background,var(--card-background-color,#fff));color:var(--primary-text-color,#222)}
      h2{font-size:18px;margin:0 0 12px} video{display:block;width:100%;aspect-ratio:16/9;background:#111;object-fit:contain} p{min-height:1.5em} #diagnostic{font-size:14px;line-height:1.5;overflow-wrap:anywhere;white-space:pre-line} #diagnostic:empty{display:none} button{font:inherit;padding:10px 16px;margin-right:8px}
    </style><article><h2></h2><video muted playsinline controls></video><p role="status" aria-live="polite"></p><p id="diagnostic"></p><button id="start">Start</button><button id="stop">Stop</button></article>`;
    this.video = this.shadowRoot.querySelector("video");
    this.status = this.shadowRoot.querySelector("p");
    this.diagnostic = this.shadowRoot.querySelector("#diagnostic");
    this.startButton = this.shadowRoot.querySelector("#start");
    this.stopButton = this.shadowRoot.querySelector("#stop");
    this.startButton.addEventListener("click", () => this.start());
    this.stopButton.addEventListener("click", () => this.stop());
    this.controller = new SessionController({
      rtcFactory: (config) => new RTCPeerConnection(config),
      onDiagnostic: (snapshot) => {
        this.diagnostic.textContent = snapshot
          ? `WebRTC diagnostic: category=${snapshot.category}; code=${snapshot.code}; stage=${snapshot.stage}\nMessage class=${snapshot.messageClass}; timeout wording=${snapshot.timeoutWording} (wording only)\nElapsed from Start=${snapshot.startElapsedMs === null ? "unavailable" : snapshot.startElapsedMs + " ms"}; offer submission=${snapshot.offerElapsedMs === null ? "unavailable" : snapshot.offerElapsedMs + " ms"} (${snapshot.category === "subscribed-error" ? "at error receipt" : "at failure"})\nSession assigned=${snapshot.sessionAssigned}; answer applied=${snapshot.answerApplied}; video track seen=${snapshot.videoTrackSeen}; frame confirmed=${snapshot.frameConfirmed}\nSignaling=${snapshot.signalingState}; ICE=${snapshot.iceConnectionState}; connection=${snapshot.connectionState}\nVideo play=${snapshot.playState}; ready=${this.fixedDisplay(snapshot.video.readyState)}; size=${this.fixedDisplay(snapshot.video.width)}x${this.fixedDisplay(snapshot.video.height)}; time=${this.fixedDisplay(snapshot.video.currentTimeMs)} ms; paused=${this.fixedDisplay(snapshot.video.paused)}\n${snapshot.transportSnapshots.map((item, index) => `Video transport ${index+1}: ${item.state}; packets=${this.fixedDisplay(item.packetsReceived)}; bytes=${this.fixedDisplay(item.bytesReceived)}; frames received=${this.fixedDisplay(item.framesReceived)}; frames decoded=${this.fixedDisplay(item.framesDecoded)}`).join("\n")}`
          : "";
      },
      onState: (state, message) => {
        this.traceLifecycle(`state-${state}`);
        this.status.textContent = message;
        this.startButton.disabled = !BACKEND_CLEANUP_VERIFIED || !this.isConnected || ["starting", "receiving"].includes(state);
        this.stopButton.disabled = !this.controller?.active;
      },
      readVideoState: () => {
        if (!this.frameOwner || !this.video.srcObject) return null;
        const read = (key) => { try { return this.video[key]; } catch { return null; } };
        const time = read("currentTime");
        return { readyState: read("readyState"), width: read("videoWidth"), height: read("videoHeight"),
          currentTimeMs: typeof time === "number" && Number.isFinite(time) && time >= 0 && time <= 86400 ? Math.floor(time*1000) : null,
          paused: read("paused") };
      },
      onTrack: (track, markFrame, markPlayState) => {
        this.clearFrameObserver();
        const stream = new MediaStream([track]);
        this.video.srcObject = stream;
        const owner = { handle: null, playing: null };
        this.frameOwner = owner;
        if (typeof this.video.requestVideoFrameCallback === "function") {
          owner.handle = this.video.requestVideoFrameCallback(() => {
            if (this.frameOwner === owner && this.video.srcObject === stream) {
              owner.handle = null;
              markFrame();
            }
          });
        } else {
          // Compatibility fallback proves playback/current data, not a frame callback.
          owner.playing = () => {
            if (this.frameOwner === owner && this.video.srcObject === stream && this.video.readyState >= 2) {
              markFrame();
              this.video.removeEventListener("playing", owner.playing);
            }
          };
          this.video.addEventListener("playing", owner.playing);
        }
        this.video.play().then(() => markPlayState("resolved")).catch(() => {
          markPlayState("rejected");
          if (this.controller.active?.tracks.has(track)) {
            this.traceLifecycle("playback-rejected");
            this.stop("Video playback failed. Session cleanup requested.");
          }
        });
      },
      onClear: () => { this.clearFrameObserver(); this.video.pause(); this.video.srcObject = null; this.video.removeAttribute("src"); this.video.load(); },
    });
    this.onVisibility = () => {
      this.traceLifecycle(document.hidden ? "visibility-hidden" : "visibility-visible");
      if (document.hidden) this.stop("Page hidden. Session cleanup requested.");
    };
    this.onPageHide = () => { this.traceLifecycle("pagehide"); this.stop("Page closed. Session cleanup requested."); };
  }

  setConfig(config) {
    let next;
    try {
      if (!config || typeof config.entity !== "string" || !/^camera\.[a-z0-9_]+$/.test(config.entity)) throw new Error("A camera entity is required.");
      if (Object.keys(config).some((key) => !["type", "entity", "name"].includes(key))) throw new Error("Only entity and optional name are supported.");
      if (config.name !== undefined && typeof config.name !== "string") throw new Error("Camera name must be text.");
      next = { entity: config.entity, name: config.name || "" };
    } catch (error) { this.traceLifecycle("config-invalid"); throw error; }
    if (this.config && this.config.entity === next.entity && this.config.name === next.name) {
      this.traceLifecycle("config-equivalent");
      return;
    }
    this.traceLifecycle(this.config ? "config-changed" : "config-initial");
    this.stop("Ready. Press Start to connect.");
    this.controller.clearDiagnostic();
    this.config = next;
    this.shadowRoot.querySelector("h2").textContent = next.name || next.entity;
    if (!BACKEND_CLEANUP_VERIFIED) this.status.textContent = "Offline candidate: backend cleanup verification required.";
  }

  set hass(value) {
    if (this.controller.active && (!value?.connected || value.connection !== this._hass?.connection)) {
      this.traceLifecycle("ha-connection-changed");
      this.stop("Home Assistant connection changed. Session cleanup requested.");
    }
    this._hass = value;
  }

  get hass() { return this._hass; }
  getCardSize() { return 5; }

  fixedDisplay(value) { return value === null ? "unavailable" : String(value); }

  clearFrameObserver() {
    const owner = this.frameOwner;
    this.frameOwner = null;
    if (owner?.handle !== null && owner?.handle !== undefined && typeof this.video.cancelVideoFrameCallback === "function") {
      this.video.cancelVideoFrameCallback(owner.handle);
    }
    if (owner?.playing) this.video.removeEventListener("playing", owner.playing);
  }

  connectedCallback() {
    this.traceLifecycle("connected");
    document.addEventListener("visibilitychange", this.onVisibility);
    window.addEventListener("pagehide", this.onPageHide);
    this.stop("Ready. Press Start to connect.");
    if (!BACKEND_CLEANUP_VERIFIED) this.status.textContent = "Offline candidate: backend cleanup verification required.";
  }

  disconnectedCallback() {
    this.traceLifecycle("disconnected");
    document.removeEventListener("visibilitychange", this.onVisibility);
    window.removeEventListener("pagehide", this.onPageHide);
    this.stop("Card removed. Session cleanup requested.");
    this.controller.clearDiagnostic();
  }

  start() {
    this.traceLifecycle("start-requested");
    this.controller.clearDiagnostic();
    if (!BACKEND_CLEANUP_VERIFIED) { this.status.textContent = "Offline candidate: backend cleanup verification required."; return; }
    if (!this.isConnected || document.hidden) { this.status.textContent = "Open this page before starting."; return; }
    const entity = this._hass?.states?.[this.config?.entity];
    if (!entity || entity.state === "unavailable") { this.status.textContent = "Camera entity is unavailable."; return; }
    if (typeof RTCPeerConnection === "undefined") { this.status.textContent = "WebRTC is unavailable in this browser."; return; }
    this.controller.start(this.config.entity, this._hass);
  }

  traceLifecycle(reason) {
    try { this.recordLifecycle(reason, Boolean(this.controller?.active)); } catch { /* Evidence must not change player behavior. */ }
  }

  stop(message) { this.traceLifecycle("stop-requested"); this.controller.stop(message); }
}

if (!customElements.get("scoped-webrtc-camera")) customElements.define("scoped-webrtc-camera", ScopedWebRtcCamera);
