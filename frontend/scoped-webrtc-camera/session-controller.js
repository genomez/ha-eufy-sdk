// Each attempt owns its peer, tracks, subscription and deadline.
const ERROR_CODES = ["webrtc_offer_failed", "go2rtc_webrtc_offer_failed"];
const FAILURE_CATEGORIES = ["subscribed-error", "local-start", "negotiation", "candidate", "ice", "timeout", "disconnected", "video", "queue-limit", "invalid-session"];
const STAGES = ["config", "offer", "awaiting-answer", "negotiating", "awaiting-video", "playing"];
const MESSAGE_CLASSES = ["unknown", "source-missing", "source-unsupported", "codec-mismatch", "producer-error", "timeout-wording"];
const EVENT_TYPES = ["session", "answer", "candidate", "error"];
const MAX_MESSAGE_LENGTH = 512;
const MAX_ELAPSED_MS = 86400000;
const PEER_STATES = {
  signalingState: ["stable", "have-local-offer", "have-remote-offer", "have-local-pranswer", "have-remote-pranswer", "closed"],
  iceConnectionState: ["new", "checking", "connected", "completed", "failed", "disconnected", "closed"],
  connectionState: ["new", "connecting", "connected", "disconnected", "failed", "closed"],
};
const finiteValue = (value, allowed) => value == null ? "not_supplied" : allowed.includes(value) ? value : "other";

function ownDataValue(object, key) {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
  } catch { return undefined; }
}

// These labels describe message wording only, never a provider branch or camera rejection.
function classifyMessage(event) {
  const unknown = { messageClass: "unknown", timeoutWording: false };
  let text = ownDataValue(event, "message");
  if (typeof text !== "string" || !text.length || text.length > MAX_MESSAGE_LENGTH || /[\r\n\u0000-\u001f\u007f\u2028\u2029]/.test(text)) return unknown;
  if (text.startsWith("webrtc/offer: ")) text = text.slice(14);
  if (text === "Camera has no stream source") return { messageClass: "source-missing", timeoutWording: false };
  if (text === "Stream source is not supported by go2rtc") return { messageClass: "source-unsupported", timeoutWording: false };
  const timeout = text === "i/o timeout" || text === "context deadline exceeded";
  if (timeout) return { messageClass: "timeout-wording", timeoutWording: true };
  // The retained producer formatter joins independent errors with comma-space.
  // Codec lists also use that delimiter, so classify that documented prefix separately.
  if (text.startsWith("streams: codecs not matched: ")) {
    if (!/^streams: codecs not matched: (?:video|audio):[A-Za-z0-9._/-]{1,32}(?:, (?:video|audio):[A-Za-z0-9._/-]{1,32})* => (?:video|audio):[A-Za-z0-9._/-]{1,32}(?:, (?:video|audio):[A-Za-z0-9._/-]{1,32})*$/.test(text)) return unknown;
    return { messageClass: "codec-mismatch", timeoutWording: false };
  }
  if (!text.startsWith("streams: ") || text === "streams: unknown error" || text.length === 9 || text.includes(", ") || /[;|]/.test(text) || text.includes("streams:", 1) || text.includes("webrtc/offer:", 1) || /Camera has no stream source|Stream source is not supported by go2rtc|codecs not matched/.test(text)) return unknown;
  return { messageClass: "producer-error", timeoutWording: text.endsWith(": i/o timeout") || text === "streams: i/o timeout" || text.endsWith(": context deadline exceeded") || text === "streams: context deadline exceeded" };
}

function elapsedMs(start, end) {
  if (start === null || end === null || end < start) return null;
  return Math.floor(Math.min(MAX_ELAPSED_MS, end - start));
}

const VIDEO_COUNTERS = ["packetsReceived", "bytesReceived", "framesReceived", "framesDecoded"];
const boundedInteger = (value, limit = Number.MAX_SAFE_INTEGER) =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= limit ? value : null;
const emptyStats = (state = "unavailable") => Object.freeze({ state,
  packetsReceived: null, bytesReceived: null, framesReceived: null, framesDecoded: null });

function videoStats(report) {
  try {
    const iterator = report.values();
    let selected = null;
    // At most 256 iterator steps; saturated or malformed reports are unavailable.
    for (let steps = 0; steps < 256; steps++) {
      const item = iterator.next();
      if (ownDataValue(item, "done") === true) {
        if (!selected) return emptyStats();
        const counters = Object.fromEntries(VIDEO_COUNTERS.map((key) =>
          [key, boundedInteger(ownDataValue(selected, key))]));
        return VIDEO_COUNTERS.some((key) => counters[key] !== null)
          ? Object.freeze({ state: "available", ...counters }) : emptyStats();
      }
      const row = ownDataValue(item, "value");
      if (ownDataValue(row, "type") !== "inbound-rtp") continue;
      const kind = ownDataValue(row, "kind") ?? ownDataValue(row, "mediaType");
      if (kind !== "video") continue;
      if (selected) return emptyStats();
      selected = row;
    }
  } catch { /* Only fixed unavailable state survives malformed/unsupported stats. */ }
  return emptyStats();
}

function fixedVideoState(value) {
  const paused = ownDataValue(value, "paused");
  return Object.freeze({ readyState: boundedInteger(ownDataValue(value, "readyState"), 4),
    width: boundedInteger(ownDataValue(value, "width"), 16384),
    height: boundedInteger(ownDataValue(value, "height"), 16384),
    currentTimeMs: boundedInteger(ownDataValue(value, "currentTimeMs"), MAX_ELAPSED_MS),
    paused: typeof paused === "boolean" ? paused : null });
}

export class SessionController {
  constructor({ rtcFactory, onState = () => {}, onTrack = () => {}, onClear = () => {}, onDiagnostic = () => {},
    schedule = (callback, delay) => globalThis.setTimeout(callback, delay),
    cancel = (id) => globalThis.clearTimeout(id), acquisitionMs = 15000,
    now = () => globalThis.performance.now(), readVideoState = () => null,
    scheduleStats = (callback, delay) => globalThis.setTimeout(callback, delay),
    cancelStats = (id) => globalThis.clearTimeout(id) }) {
    this.rtcFactory = rtcFactory;
    this.onState = onState;
    this.onTrack = onTrack;
    this.onClear = onClear;
    this.onDiagnostic = onDiagnostic;
    this.diagnostic = null;
    this.schedule = schedule;
    this.cancel = cancel;
    this.acquisitionMs = acquisitionMs;
    this.now = now;
    this.readVideoState = readVideoState;
    this.scheduleStats = scheduleStats;
    this.cancelStats = cancelStats;
    this.active = null;
    this.latest = null;
  }

  owns(session) { return this.active === session && !session.disposed; }

  timestamp() {
    try {
      const value = this.now();
      return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER ? value : null;
    } catch { return null; }
  }

  start(entity, hass) {
    const startedAt = this.timestamp();
    this.stop();
    this.clearDiagnostic();
    if (!/^camera\.[a-z0-9_]+$/.test(entity || "")) {
      this.onState("error", "Choose a camera entity.");
      return;
    }
    if (!hass?.connected || !hass.connection?.connected) {
      this.onState("error", "Home Assistant is disconnected.");
      return;
    }
    const session = { entity, hass, connection: hass.connection, disposed: false,
      pc: null, tracks: new Set(), stoppedTracks: new WeakSet(), mediaConfirmed: false,
      stage: "config", answerApplied: false, videoTrackSeen: false, startedAt, offerAt: null,
      videoTrack: null, playState: "unavailable", stats: [emptyStats(), emptyStats()],
      statsTimers: [], statsStarted: [false, false], statsScheduled: false,
      candidates: [], remoteCandidates: [], events: Promise.resolve(), queuedEvents: 0, id: null, unsub: null,
      unsubCalled: false, timer: null, disconnectListener: null };
    this.active = session;
    this.latest = session;
    this.onState("starting", "Connecting...");
    session.disconnectListener = () => this.fail(session, "Home Assistant disconnected.", "disconnected");
    session.connection.addEventListener("disconnected", session.disconnectListener);
    session.timer = this.schedule(() => this.fail(session, "Connection timed out. Press Start to try again.", "timeout"), this.acquisitionMs);
    void this.acquire(session);
  }

  async acquire(session) {
    try {
      const config = await session.hass.callWS({ type: "camera/webrtc/get_client_config", entity_id: session.entity });
      if (!this.owns(session)) return;
      session.stage = "offer";
      const pc = this.rtcFactory(config.configuration);
      session.pc = pc;
      pc.onicecandidate = (event) => {
        if (!this.owns(session) || !event.candidate?.candidate) return;
        let candidate;
        try { candidate = event.candidate.toJSON(); }
        catch { this.fail(session, "Invalid WebRTC candidate.", "candidate"); return; }
        if (session.id) this.sendCandidate(session, candidate);
        else if (session.candidates.length < 64) session.candidates.push(candidate);
        else this.fail(session, "Too many negotiation candidates.", "queue-limit");
      };
      pc.oniceconnectionstatechange = () => {
        if (["failed", "disconnected", "closed"].includes(pc.iceConnectionState)) {
          this.fail(session, "Camera connection ended. Press Start to try again.", "ice");
        }
      };
      pc.ontrack = (event) => {
        if (!this.owns(session)) { this.stopTrack(session, event.track); return; }
        session.tracks.add(event.track);
        if (event.track.kind !== "video") return;
        session.videoTrackSeen = true;
        session.videoTrack = event.track;
        session.playState = "pending";
        if (!session.mediaConfirmed) session.stage = "awaiting-video";
        const markFrame = () => {
          if (!this.owns(session) || !session.tracks.has(event.track)) return false;
          if (!session.mediaConfirmed) {
            session.mediaConfirmed = true;
            session.stage = "playing";
            this.cancel(session.timer);
            session.timer = null;
            this.clearStatsTimers(session);
            this.onState("receiving", "Receiving video.");
          }
          return true;
        };
        const markPlayState = (state) => {
          if (!this.owns(session) || session.videoTrack !== event.track || !session.tracks.has(event.track)) return;
          if (["resolved", "rejected"].includes(state)) session.playState = state;
        };
        try { this.onTrack(event.track, markFrame, markPlayState); }
        catch { this.fail(session, "Unable to display camera video.", "video"); return; }
        this.scheduleVideoStats(session);
      };
      if (config.dataChannel) pc.createDataChannel(config.dataChannel);
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      const offer = await pc.createOffer();
      if (!this.owns(session)) return;
      await pc.setLocalDescription(offer);
      if (!this.owns(session)) return;
      session.stage = "awaiting-answer";
      session.offerAt = this.timestamp();
      // Disable the library's reconnect resubscription: retries require Start.
      const pending = session.connection.subscribeMessage(
        (event) => {
          if (!this.owns(session)) return;
          const type = ownDataValue(event, "type");
          if (!EVENT_TYPES.includes(type)) return;
          if (session.queuedEvents >= 64) {
            this.fail(session, "Too many negotiation events.", "queue-limit");
            return;
          }
          // Replace the error before queuing so its raw message never enters the event chain.
          if (type === "error") {
            event = { type: "error", code: ownDataValue(event, "code"), receivedAt: this.timestamp(), ...classifyMessage(event) };
            event.code = finiteValue(event.code, ERROR_CODES);
          } else if (type === "session") event = { type, session_id: ownDataValue(event, "session_id") };
          else if (type === "answer") event = { type, answer: ownDataValue(event, "answer") };
          else event = { type, candidate: ownDataValue(event, "candidate") };
          session.queuedEvents++;
          session.events = session.events.then(async () => {
            try { await this.handleEvent(session, event); }
            finally { session.queuedEvents--; }
          });
        },
        { type: "camera/webrtc/offer", entity_id: session.entity, offer: pc.localDescription?.sdp || offer.sdp },
        { resubscribe: false }
      );
      session.unsub = await pending;
      if (!this.owns(session)) this.unsubscribe(session);
    } catch {
      this.fail(session, "Unable to start WebRTC. Press Start to try again.", "local-start");
    }
  }

  async handleEvent(session, event) {
    if (!this.owns(session)) return;
    let failureCategory = "negotiation";
    try {
      if (event.type === "session") {
        if (typeof event.session_id !== "string" || !event.session_id || session.id) {
          this.fail(session, "Invalid camera session.", "invalid-session");
          return;
        }
        session.id = event.session_id;
        for (const candidate of session.candidates) this.sendCandidate(session, candidate);
        session.candidates = [];
      } else if (event.type === "answer") {
        if (session.pc.signalingState !== "have-local-offer") return;
        if (!session.mediaConfirmed && !session.videoTrackSeen) session.stage = "negotiating";
        await session.pc.setRemoteDescription({ type: "answer", sdp: event.answer });
        if (!this.owns(session)) return;
        session.answerApplied = true;
        session.stage = session.mediaConfirmed ? "playing" : "awaiting-video";
        failureCategory = "candidate";
        for (const candidate of session.remoteCandidates) {
          if (!this.owns(session)) return;
          await session.pc.addIceCandidate(candidate);
        }
        session.remoteCandidates = [];
      } else if (event.type === "candidate") {
        failureCategory = "candidate";
        const candidate = event.candidate;
        const normalized = candidate.sdpMid || candidate.sdpMLineIndex != null
          ? candidate : { ...candidate, sdpMid: "0" };
        if (!session.pc.remoteDescription && session.remoteCandidates.length >= 64) {
          this.fail(session, "Too many negotiation candidates.", "queue-limit");
          return;
        }
        if (!session.pc.remoteDescription) session.remoteCandidates.push(normalized);
        else await session.pc.addIceCandidate(normalized);
      } else if (event.type === "error") {
        this.fail(session, "WebRTC connection failed.", "subscribed-error", event.code, event);
      }
    } catch {
      this.fail(session, "Unable to negotiate WebRTC. Press Start to try again.", failureCategory);
    }
  }

  sendCandidate(session, candidate) {
    if (!this.owns(session) || !session.id) return;
    Promise.resolve().then(() => {
      if (!this.owns(session)) return;
      return session.hass.callWS({ type: "camera/webrtc/candidate", entity_id: session.entity,
        session_id: session.id, candidate });
    }).catch(() => this.fail(session, "Unable to send WebRTC candidate.", "candidate"));
  }

  unsubscribe(session) {
    if (!session.unsub || session.unsubCalled) return;
    session.unsubCalled = true;
    try {
      Promise.resolve(session.unsub()).catch(() => {
        // Cleanup acknowledgement is not proof of camera source release.
        if (!this.active && this.latest === session) this.onState("cleanup-pending", "Session cleanup could not be confirmed.");
      });
    } catch {
      if (!this.active && this.latest === session) this.onState("cleanup-pending", "Session cleanup could not be confirmed.");
    }
  }

  scheduleVideoStats(session) {
    if (!this.owns(session) || session.mediaConfirmed || session.statsScheduled) return;
    session.statsScheduled = true;
    let method;
    try { method = session.pc?.getStats; } catch { return; }
    if (typeof method !== "function") return;
    const elapsed = elapsedMs(session.startedAt, this.timestamp());
    if (elapsed === null || elapsed >= this.acquisitionMs) return;
    for (const [index, atMs] of [10000, 14000].entries()) {
      if (elapsed >= atMs) continue; // A late track never replays a missed slot.
      try {
        session.statsTimers.push(this.scheduleStats(() => this.captureVideoStats(session, index, method), atMs-elapsed));
      } catch { /* A diagnostic scheduler failure must not change acquisition. */ }
    }
  }

  captureVideoStats(session, index, method) {
    if (![0, 1].includes(index)) return;
    const track = session.videoTrack;
    const elapsed = elapsedMs(session.startedAt, this.timestamp());
    if (!this.owns(session) || session.mediaConfirmed || !track || !session.tracks.has(track)
        || session.statsStarted[index] || elapsed === null || elapsed < [10000, 14000][index]
        || elapsed >= this.acquisitionMs) return;
    session.statsStarted[index] = true;
    session.stats[index] = emptyStats("pending");
    try {
      Promise.resolve(method.call(session.pc)).then((report) => {
        if (!this.owns(session) || session.videoTrack !== track || !session.tracks.has(track)) return;
        session.stats[index] = videoStats(report);
      }, () => {
        if (this.owns(session) && session.videoTrack === track && session.tracks.has(track)) session.stats[index] = emptyStats();
      });
    } catch { session.stats[index] = emptyStats(); }
  }

  clearStatsTimers(session) {
    for (const timer of session.statsTimers) {
      try { this.cancelStats(timer); } catch { /* Diagnostics must not affect cleanup. */ }
    }
    session.statsTimers = [];
  }

  dispose(session) {
    if (session.disposed) return;
    session.disposed = true;
    this.clearStatsTimers(session);
    if (session.timer !== null) this.cancel(session.timer);
    session.connection.removeEventListener("disconnected", session.disconnectListener);
    session.candidates = [];
    session.remoteCandidates = [];
    if (session.pc) {
      session.pc.onicecandidate = null;
      session.pc.oniceconnectionstatechange = null;
      session.pc.ontrack = null;
      for (const track of session.tracks) this.stopTrack(session, track);
      session.tracks.clear();
      session.pc.close();
    }
    this.unsubscribe(session);
  }

  stopTrack(session, track) {
    if (session.stoppedTracks.has(track)) return;
    session.stoppedTracks.add(track);
    track.stop();
  }

  stop(message = "Stopped. Session cleanup requested.") {
    const session = this.active;
    this.active = null;
    if (session) this.dispose(session);
    this.onClear();
    this.onState("stopped", message);
  }

  clearDiagnostic() {
    this.diagnostic = null;
    try { this.onDiagnostic(null); } catch { /* Diagnostics must not affect session cleanup. */ }
  }

  recordDiagnostic(session, category, code, classification) {
    const peerState = (key) => {
      try { return finiteValue(session.pc?.[key], PEER_STATES[key]); }
      catch { return "other"; }
    };
    // A queued subscribed error reports its ingress time, not later queue processing.
    const failedAt = classification && Object.hasOwn(classification, "receivedAt") ? classification.receivedAt : this.timestamp();
    let video = null;
    try { video = this.readVideoState(); } catch { /* Fixed unavailable video fields. */ }
    this.diagnostic = Object.freeze({
      category: finiteValue(category, FAILURE_CATEGORIES),
      code: code === "other" || code === "not_supplied" ? code : finiteValue(code, ERROR_CODES),
      stage: finiteValue(session.stage, STAGES),
      sessionAssigned: Boolean(session.id),
      answerApplied: session.answerApplied === true,
      videoTrackSeen: session.videoTrackSeen === true,
      frameConfirmed: session.mediaConfirmed === true,
      signalingState: peerState("signalingState"),
      iceConnectionState: peerState("iceConnectionState"),
      connectionState: peerState("connectionState"),
      messageClass: MESSAGE_CLASSES.includes(classification?.messageClass) ? classification.messageClass : "unknown",
      timeoutWording: classification?.timeoutWording === true,
      startElapsedMs: elapsedMs(session.startedAt, failedAt),
      offerElapsedMs: elapsedMs(session.offerAt, failedAt),
      transportSnapshots: Object.freeze([...session.stats]),
      video: fixedVideoState(video),
      playState: ["pending", "resolved", "rejected"].includes(session.playState) ? session.playState : "unavailable",
    });
  }

  fail(session, message, category = "negotiation", code, classification) {
    if (!this.owns(session)) return;
    this.recordDiagnostic(session, category, code, classification);
    this.active = null;
    this.dispose(session);
    this.onClear();
    this.onState("error", message);
    try { this.onDiagnostic(this.diagnostic); } catch { /* Diagnostics must not affect session cleanup. */ }
  }
}
