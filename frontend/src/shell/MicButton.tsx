import { Microphone, Stop } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { errText } from "../api";
import { canRecord, captureStatus, transcribe, useRecorder } from "../capture";

/** Speak instead of typing: records, turns it into text on the lake's computer, and hands the text back. */
export default function MicButton({ onText, className = "" }: { onText: (text: string) => void; className?: string }) {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const rec = useRecorder();
  useEffect(() => { if (canRecord()) captureStatus().then((s) => setReady(!!s?.speech.available)); }, []);
  if (!ready) return null;

  const toggle = async () => {
    setError("");
    if (rec.state === "recording") {
      const audio = await rec.stop();
      if (!audio || audio.blob.size < 1200) return;
      rec.setState("busy");
      try { const text = await transcribe(audio.blob, audio.name); if (text) onText(text); else setError("No speech heard"); }
      catch (e) { setError(errText(e)); }
      rec.setState("idle");
      return;
    }
    try { await rec.start(); } catch (e) { setError(/denied|NotAllowed/i.test(String(e)) ? "Allow the microphone to speak" : errText(e)); }
  };

  const label = rec.state === "recording" ? `Stop recording (${rec.seconds}s)` : rec.state === "busy" ? "Turning speech into text" : "Speak";
  return (
    <span className="relative inline-flex">
      <button type="button" className={`icon-btn ${rec.state === "recording" ? "bg-coral-50 text-coral-500" : ""} ${className}`} aria-label={label} title={error || label}
        disabled={rec.state === "busy"} onClick={toggle}>
        {rec.state === "recording" ? <Stop size={16} weight="fill" /> : <Microphone size={16} className={rec.state === "busy" ? "animate-pulse" : ""} />}
      </button>
      {rec.state === "recording" && <span className="pointer-events-none absolute -top-6 left-1/2 -translate-x-1/2 rounded bg-coral-500 px-1.5 text-[10px] font-semibold text-white">{rec.seconds}s</span>}
      {error && <span role="alert" className="absolute -top-7 right-0 whitespace-nowrap rounded bg-surface-primary px-2 py-0.5 text-[11px] text-coral-500 shadow">{error}</span>}
    </span>
  );
}
