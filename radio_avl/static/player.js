"use strict";
// One shared player bar for call recordings, used by the map and the Channel page.
window.KTPlayer = (function () {
  let bar = null, audio = null, title = null, dl = null, currentId = null;
  const listeners = new Set();
  function build() {
    bar = document.createElement("div");
    bar.className = "kt-player";
    bar.hidden = true;
    const icon = document.createElement("span"); icon.className = "kt-player-icon"; icon.textContent = "🎙";
    title = document.createElement("div"); title.className = "kt-player-title";
    audio = document.createElement("audio"); audio.controls = true; audio.preload = "auto";
    dl = document.createElement("a"); dl.className = "kt-player-dl"; dl.textContent = "Download"; dl.title = "Download this recording";
    const close = document.createElement("button"); close.className = "kt-player-close"; close.textContent = "✕"; close.title = "Close";
    close.onclick = stop;
    audio.onended = () => notify("ended");
    audio.onplay = () => notify("playing");
    audio.onpause = () => notify("paused");
    bar.append(icon, title, audio, dl, close);
    document.body.appendChild(bar);
  }
  function notify(state) { for (const f of listeners) f(currentId, state); }
  function play(id, label) {
    if (!bar) build();
    if (currentId === id && !audio.paused) { audio.pause(); return; }
    if (currentId !== id) {
      currentId = id;
      audio.src = `/api/recordings/${id}`;
      dl.href = `/api/recordings/${id}?download=1`;
      title.textContent = label;
    }
    bar.hidden = false;
    document.body.classList.add("has-player");
    audio.play().catch(() => {});
  }
  function stop() {
    if (!bar) return;
    audio.pause(); audio.removeAttribute("src"); audio.load();
    bar.hidden = true; document.body.classList.remove("has-player");
    const id = currentId; currentId = null; for (const f of listeners) f(id, "stopped");
  }
  return { play, stop, onChange: (f) => listeners.add(f), current: () => currentId, playing: () => audio && !audio.paused };
})();
