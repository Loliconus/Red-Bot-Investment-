# Frontend dependencies (offline, no runtime CDN)

The precompiled assets in `static/` include third-party software, used only in the browser.
JavaScript dependencies are installed and bundled only at *development/build* time from
`assets/package-lock.json`; Python/Node applications do not need Node.js to serve the GUI.

| Library | Version | License | Source |
|---|---:|---|---|
| htmx | 2.0.8 | 0BSD | https://github.com/bigskysoftware/htmx |
| Alpine.js | 3.15.0 | MIT | https://github.com/alpinejs/alpine |
| Lightweight Charts™ | 5.0.8 | Apache-2.0 | https://github.com/tradingview/lightweight-charts |
| CodeMirror 6 and Lezer packages | as pinned in lockfile | MIT | https://github.com/codemirror |
| Tailwind CSS | 3.4.17 | MIT | build-time CSS compiler, not shipped as JS |

`LICENSE-*.txt` contains the upstream license texts shipped for htmx, Lightweight
Charts and CodeMirror. Alpine.js and its dependencies retain their MIT license;
refer to the upstream repository for their copyright notices. The Lightweight
Charts™ attribution and a link to TradingView are visible beneath every chart.
