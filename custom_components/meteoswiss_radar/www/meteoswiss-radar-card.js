/**
 * meteoswiss-radar-card.js
 *
 * A Lovelace custom card that shows the MeteoSwiss precipitation timeline
 * for a configured location, with:
 *
 *  * A header showing the current intensity bin + a colour swatch.
 *  * A 24-hour timeline of (past + forecast) rates as a stacked bar chart.
 *  * A "next rain in N min" pill (or "dry" if the next 6 h are rain-free).
 *  * A scrubber so the user can preview any historical or forecast step.
 *
 * The card reads the ``sensor.meteoswiss_radar_timeline`` entity (JSON
 * attribute produced by the AppDaemon app) plus the current state. It
 * renders everything from the data; no map is embedded (the upstream
 * MeteoSwiss radar page is iframe-blocked, and the AppDaemon app already
 * has the full data so the client doesn't need a map).
 *
 * To install:
 *   1. Drop this file into ``config/www/meteoswiss-radar-card.js`` in your
 *      Home Assistant config dir.
 *   2. Add a Lovelace resource for it (Settings → Dashboards → Resources):
 *        URL:  /local/meteoswiss-radar-card.js
 *        Type: JavaScript Module
 *   3. Add the card to a view:
 *        type: custom:meteoswiss-radar-card
 *        entity: sensor.meteoswiss_radar_timeline
 *        location_name: Romanshorn
 */

class MeteoSwissRadarCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._selectedTimestamp = null; // scrubbed-to step
  }

  static getStubConfig() {
    return {
      entity: "sensor.meteoswiss_radar_timeline",
      location_name: "Romanshorn",
      title: "MeteoSwiss rain radar",
      show_legend: true,
    };
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("Please define an entity (e.g. sensor.meteoswiss_radar_timeline)");
    }
    this._config = {
      title: "MeteoSwiss rain radar",
      show_legend: true,
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // --- Helpers ----------------------------------------------------------------

  _getEntity(entityId) {
    return this._hass && this._hass.states[entityId];
  }

  _timelineAttr() {
    const entity = this._getEntity(this._config.entity);
    if (!entity) return null;
    return entity.attributes && entity.attributes.timeline
      ? entity.attributes.timeline
      : null;
  }

  _currentRate() {
    return this._getEntity("sensor.meteoswiss_radar_current_rate");
  }
  _currentState() {
    return this._getEntity("sensor.meteoswiss_radar_current_state");
  }
  _nextRain() {
    return this._getEntity("sensor.meteoswiss_radar_next_rain");
  }
  _forecastMax6h() {
    return this._getEntity("sensor.meteoswiss_radar_forecast_max_6h");
  }

  _colorForBin(binLabel) {
    // Match the AppDaemon app's colour map (matches MeteoSwiss legend).
    const map = {
      "0–1 mm/h": "#9a7e95",
      "1–2 mm/h": "#0001fc",
      "2–4 mm/h": "#058c2d",
      "4–6 mm/h": "#05ff05",
      "6–10 mm/h": "#feff01",
      "10–20 mm/h": "#ffc703",
      "20–40 mm/h": "#ff7d01",
      "40–60 mm/h": "#ff1900",
      "60+ mm/h": "#af00dd",
      "no data": "#ffffff",
      warning: "#333e48",
      unknown: "#888888",
    };
    return map[binLabel] || "#888888";
  }

  _formatTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  _formatDate(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  }

  // --- Rendering --------------------------------------------------------------

  _render() {
    if (!this._config || !this._hass) return;
    const timeline = this._timelineAttr();
    if (!timeline) {
      this.shadowRoot.innerHTML = `
        <ha-card>
          <div class="card">
            <div class="header"><h2>${this._config.title}</h2></div>
            <div class="empty">Waiting for data from <code>${this._config.entity}</code>…</div>
          </div>
        </ha-card>
        <style>${this._styles()}</style>
      `;
      return;
    }

    const entries = Object.values(timeline).sort((a, b) => a.ts - b.ts);
    const now = Math.floor(Date.now() / 1000);
    const currentState = this._currentState() ? this._currentState().state : "—";
    const currentColor = this._colorForBin(currentState);
    const nextRainEntity = this._nextRain();
    const nextRain = nextRainEntity ? nextRainEntity.state : null;
    const max6hEntity = this._forecastMax6h();
    const max6h = max6hEntity ? max6hEntity.state : 0;

    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="card">
          <div class="header">
            <h2>
              <span class="swatch" style="background:${currentColor}"></span>
              ${this._config.title} · ${this._config.location_name || ""}
            </h2>
            <div class="now">${currentState}</div>
          </div>
          <div class="summary">
            <div class="metric">
              <div class="value">${this._formatRain(nextRain)}</div>
              <div class="label">Next rain</div>
            </div>
            <div class="metric">
              <div class="value">${(max6h || 0).toFixed ? (max6h).toFixed(1) : max6h} mm/h</div>
              <div class="label">Max 6h</div>
            </div>
            <div class="metric">
              <div class="value">${entries.length}</div>
              <div class="label">Steps</div>
            </div>
          </div>
          <div class="chart" id="chart">
            ${this._renderBars(entries, now)}
          </div>
          ${this._config.show_legend !== false ? this._renderLegend() : ""}
        </div>
      </ha-card>
      <style>${this._styles()}</style>
    `;
    this._bindScrubber(entries, now);
  }

  _formatRain(value) {
    if (value === null || value === undefined || value === "unknown" || value === "unavailable") {
      return "—";
    }
    if (typeof value === "number") {
      if (value === 0) return "now";
      if (value < 60) return `${value} min`;
      return `${Math.round(value / 60)} h`;
    }
    return String(value);
  }

  _renderBars(entries, now) {
    // 6 columns: past, now, +0-6h, +6-12h, +12-18h, +18-24h
    // But the timeline from the AppDaemon is sparse (1 per 5 min for the
    // first 6h, then 1 per hour for the rest). We render one bar per
    // entry, sized by time-spacing. That gives a natural-looking chart.
    if (entries.length === 0) {
      return `<div class="empty">No timeline data</div>`;
    }
    const minTs = entries[0].ts;
    const maxTs = entries[entries.length - 1].ts;
    const total = Math.max(1, maxTs - minTs);
    const selectedTs = this._selectedTimestamp;
    return `
      <div class="bars">
        ${entries.map((e) => {
          const left = ((e.ts - minTs) / total) * 100;
          const color = e.rate === null ? "#888" : this._colorForBin(e.bin);
          const isPast = e.ts < now;
          const isSelected = selectedTs === e.ts;
          const title = `${this._formatTime(e.ts)} — ${e.bin} (${e.rate !== null ? e.rate.toFixed(1) + " mm/h" : "—"})`;
          return `<div class="bar ${isPast ? "past" : "future"} ${isSelected ? "selected" : ""}"
                       data-ts="${e.ts}"
                       title="${title}"
                       style="left:${left}%;background:${color}"></div>`;
        }).join("")}
        <div class="now-marker" style="left:${((now - minTs) / total) * 100}%"></div>
      </div>
      <div class="axis">
        <span>${this._formatTime(minTs)}</span>
        <span>now</span>
        <span>${this._formatTime(maxTs)}</span>
      </div>
    `;
  }

  _renderLegend() {
    const bins = [
      ["0–1 mm/h", "no rain"],
      ["1–2 mm/h", ""],
      ["2–4 mm/h", ""],
      ["4–6 mm/h", ""],
      ["6–10 mm/h", ""],
      ["10–20 mm/h", ""],
      ["20–40 mm/h", ""],
      ["40–60 mm/h", "heavy"],
      ["60+ mm/h", "extreme"],
    ];
    return `
      <div class="legend">
        ${bins.map(([bin, note]) => `
          <div class="legend-item">
            <span class="swatch small" style="background:${this._colorForBin(bin)}"></span>
            <span>${bin}${note ? ` <em>(${note})</em>` : ""}</span>
          </div>
        `).join("")}
      </div>
    `;
  }

  _bindScrubber(entries, now) {
    const chart = this.shadowRoot.getElementById("chart");
    if (!chart) return;
    chart.addEventListener("click", (ev) => {
      const bar = ev.target.closest(".bar");
      if (!bar) return;
      const ts = parseInt(bar.getAttribute("data-ts"), 10);
      this._selectedTimestamp = ts;
      this._render();
    });
  }

  _styles() {
    return `
      :host { display: block; }
      ha-card { display: block; padding: 0; }
      .card { padding: 16px; }
      .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
      .header h2 { font-size: 18px; margin: 0; display: flex; align-items: center; gap: 8px; }
      .swatch { display: inline-block; width: 16px; height: 16px; border-radius: 50%; border: 1px solid rgba(0,0,0,0.2); }
      .swatch.small { width: 12px; height: 12px; }
      .now { font-weight: 500; font-size: 16px; }
      .summary { display: flex; gap: 12px; margin-bottom: 16px; }
      .metric { flex: 1; background: rgba(127,127,127,0.1); padding: 8px 12px; border-radius: 8px; text-align: center; }
      .metric .value { font-size: 20px; font-weight: 600; }
      .metric .label { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
      .chart { height: 80px; position: relative; margin-bottom: 8px; }
      .bars { position: relative; height: 60px; background: rgba(127,127,127,0.1); border-radius: 4px; overflow: hidden; }
      .bar { position: absolute; top: 0; height: 100%; width: 3px; cursor: pointer; transition: transform 0.1s ease, width 0.1s ease; opacity: 0.85; }
      .bar.past { opacity: 0.55; }
      .bar:hover, .bar.selected { transform: scaleX(2.5); width: 4px; opacity: 1; }
      .now-marker { position: absolute; top: 0; height: 100%; width: 2px; background: var(--primary-text-color); pointer-events: none; }
      .axis { display: flex; justify-content: space-between; font-size: 11px; color: var(--secondary-text-color); padding: 0 2px; }
      .legend { display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px 12px; margin-top: 12px; font-size: 12px; }
      .legend-item { display: flex; align-items: center; gap: 6px; }
      .empty { color: var(--secondary-text-color); padding: 16px; text-align: center; }
    `;
  }
}

customElements.define("meteoswiss-radar-card", MeteoSwissRadarCard);

// Register with the custom-card helper so the picker shows the card
// (see https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card)
window.customCards = window.customCards || [];
window.customCards.push({
  type: "meteoswiss-radar-card",
  name: "MeteoSwiss rain radar",
  preview: true,
  description: "MeteoSwiss precipitation timeline for a single location.",
});
