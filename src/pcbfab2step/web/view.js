// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Albert van Zyl
//
// `pcbfab2step --view`: one scene, full window. The scene is embedded in the
// page (window.PCB_SCENE) or served next to it (./scene).

import { Viewer, decodeScene, fetchScene } from "pcbfab2step/viewer";
import { mountChrome } from "pcbfab2step/viewer-ui";

const vp = document.getElementById("viewer");
const busy = vp.appendChild(Object.assign(document.createElement("div"), { className: "busy", textContent: "Loading the model" }));
const viewer = new Viewer(vp);
const chrome = mountChrome(vp, viewer);
window.pcbViewer = viewer;

try {
  const key = new URLSearchParams(location.search).get("t") || "";
  const sc = window.PCB_SCENE ? await decodeScene(window.PCB_SCENE) : await fetchScene("scene", key);
  viewer.load(sc.manifest, sc.buffer);
  document.title = sc.manifest.title;
  chrome.refresh();
  busy.remove();
} catch (err) {
  busy.textContent = `Could not show the model: ${err.message || err}`;
}
