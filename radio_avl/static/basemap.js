"use strict";
// Street map / satellite switch shared by the live map, reports and settings.
window.KTBasemap = function (map, cfg, opts = {}) {
  const key = "kt.basemap";
  const read = () => { try { return localStorage.getItem(key) || "map"; } catch (e) { return "map"; } };
  const write = (v) => { try { localStorage.setItem(key, v); } catch (e) { /* private mode */ } };
  const layers = {};
  if (cfg.tiles) layers.map = L.tileLayer("/tiles/{z}/{x}/{y}", { maxNativeZoom: cfg.max_zoom, maxZoom: 20, attribution: cfg.attribution });
  if (cfg.satellite) layers.sat = L.tileLayer("/tiles/sat/{z}/{x}/{y}", { maxNativeZoom: cfg.sat_max_zoom, maxZoom: 20, attribution: cfg.sat_attribution });
  let current = null;

  function use(name) {
    if (!layers[name]) name = layers.map ? "map" : layers.sat ? "sat" : null;
    if (current) map.removeLayer(layers[current]);
    current = name;
    if (name) { layers[name].addTo(map); layers[name].bringToBack(); }
    map.getContainer().classList.toggle("on-sat", name === "sat");
    if (ctl) for (const b of ctl.querySelectorAll("button")) b.classList.toggle("on", b.dataset.l === name);
    if (!opts.noSave && name) write(name);
  }

  let ctl = null;
  if (layers.map && layers.sat) {
    const C = L.Control.extend({
      options: { position: opts.position || "bottomright" },
      onAdd() {
        ctl = L.DomUtil.create("div", "leaflet-bar basemap-switch");
        for (const [l, label] of [["map", "Map"], ["sat", "Satellite"]]) {
          const b = L.DomUtil.create("button", "", ctl);
          b.type = "button"; b.dataset.l = l; b.textContent = label;
          L.DomEvent.on(b, "click", (e) => { L.DomEvent.stop(e); use(l); });
        }
        L.DomEvent.disableClickPropagation(ctl);
        return ctl;
      },
    });
    map.addControl(new C());
  }
  use(read());
  return { use, current: () => current };
};
