import {offlineView} from "/static/offline-model.js";

export function updateOfflineNotice(element, data) {
  const view = offlineView(data);
  element.hidden = !view.visible;
  element.querySelector(".offline-reason").textContent = view.reason;
  element.querySelector(".offline-recovered").hidden = !view.recovered;
}
