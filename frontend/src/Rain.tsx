import { useEffect, useRef } from "react";

// A faint band of falling glyphs behind the top of the console. Decorative: drawn at low opacity, faded out below
// the header, slowed to a few frames a second, paused while the tab is hidden, and a still frame when the system
// asks for reduced motion.
const GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉ0123456789:=*+<>";
const CELL = 16;
const FPS = 12;

export default function Rain() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)");
    let drops: number[] = [];
    let raf = 0, last = 0, w = 0, h = 0;

    const color = () => getComputedStyle(canvas).getPropertyValue("--phosphor").trim() || "#3ddc84";
    const resize = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drops = Array.from({ length: Math.ceil(w / CELL) }, () => Math.random() * -h / CELL);
    };
    const frame = (fade: boolean) => {
      ctx.globalCompositeOperation = "destination-out";
      ctx.fillStyle = fade ? "rgba(0,0,0,0.12)" : "rgba(0,0,0,1)";
      ctx.fillRect(0, 0, w, h);
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = color();
      ctx.font = `${CELL - 4}px "Geist Mono Variable", ui-monospace, monospace`;
      drops.forEach((y, i) => {
        if (y >= 0) ctx.fillText(GLYPHS[(Math.random() * GLYPHS.length) | 0], i * CELL, y * CELL);
        drops[i] = y * CELL > h && Math.random() > 0.96 ? Math.random() * -8 : y + 1;
      });
    };
    const tick = (t: number) => {
      raf = requestAnimationFrame(tick);
      if (document.hidden || t - last < 1000 / FPS) return;
      last = t;
      frame(true);
    };
    const start = () => {
      cancelAnimationFrame(raf);
      resize();
      if (still.matches) { // one settled frame, no motion
        for (let i = 0; i < 40; i++) frame(true);
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    start();
    window.addEventListener("resize", start);
    still.addEventListener("change", start);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", start); still.removeEventListener("change", start); };
  }, []);

  return <canvas ref={ref} className="rain" aria-hidden />;
}
