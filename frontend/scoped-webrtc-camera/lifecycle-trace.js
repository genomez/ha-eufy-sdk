// Anonymous, page-memory-only evidence. A full page reload resets this record.
export const LIFECYCLE_TRACE_LIMITS = Object.freeze({ events: 128, counter: 65535, instances: 65535, elapsedMs: 86400000 });
export const LIFECYCLE_REASONS = Object.freeze([
  "created", "config-initial", "config-equivalent", "config-changed", "config-invalid",
  "connected", "disconnected", "visibility-hidden", "visibility-visible", "pagehide",
  "ha-connection-changed", "start-requested", "stop-requested", "playback-rejected",
  "state-starting", "state-receiving", "state-stopped", "state-error", "state-cleanup-pending",
]);

function createTrace() {
  const events = [], counters = Object.fromEntries(LIFECYCLE_REASONS.map(reason => [reason, 0]));
  const clock = () => { try { return performance.now(); } catch { return NaN; } };
  const origin = clock();
  let instances = 0, instanceOrdinalSaturated = false, droppedEvents = 0, lastElapsedMs = 0;
  const read = () => Object.freeze({
    version: 1, reloadLimit: "full-page-reload-resets-trace", limits: LIFECYCLE_TRACE_LIMITS,
    instances, instanceOrdinalSaturated, droppedEvents,
    counters: Object.freeze({ ...counters }),
    events: Object.freeze(events.map(event => Object.freeze({ ...event }))),
  });
  const createRecorder = () => {
    if (instances < LIFECYCLE_TRACE_LIMITS.instances) instances++;
    else instanceOrdinalSaturated = true;
    const instance = instances;
    return (reason, active) => {
      try {
        if (!LIFECYCLE_REASONS.includes(reason)) return;
        const elapsed = clock() - origin;
        if (Number.isFinite(elapsed) && elapsed >= 0) {
          lastElapsedMs = Math.max(lastElapsedMs, Math.min(LIFECYCLE_TRACE_LIMITS.elapsedMs, Math.floor(elapsed)));
        }
        counters[reason] = Math.min(LIFECYCLE_TRACE_LIMITS.counter, counters[reason] + 1);
        if (events.length === LIFECYCLE_TRACE_LIMITS.events) {
          events.shift();
          droppedEvents = Math.min(LIFECYCLE_TRACE_LIMITS.counter, droppedEvents + 1);
        }
        events.push(Object.freeze({ reason, instance, elapsedMs: lastElapsedMs, active: active === true }));
      } catch { /* Trace failures must not affect player ownership or cleanup. */ }
    };
  };
  return Object.freeze({ version: 1, read, createRecorder });
}

// Re-evaluating the module on this page retains the trace, without retaining cards.
const trace = (() => {
  const key = Symbol.for("scoped-webrtc-camera.lifecycle.v1");
  try {
    const existing = Object.getOwnPropertyDescriptor(window, key)?.value;
    if (existing?.version === 1 && typeof existing.read === "function" && typeof existing.createRecorder === "function") return existing;
    const value = createTrace();
    Object.defineProperty(window, key, { value });
    return value;
  } catch { return createTrace(); }
})();

export const readLifecycleTrace = () => { try { return trace.read(); } catch { return null; } };
export const createLifecycleRecorder = () => {
  try {
    const record = trace.createRecorder();
    return (reason, active) => { try { record(reason, active); } catch { /* Observational only. */ } };
  } catch { return () => {}; }
};
