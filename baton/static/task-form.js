const form = document.querySelector(".task-form");
const catalogs = JSON.parse(document.querySelector("#task-catalogs").textContent);
const settings = document.querySelector("#task-settings");
const openCode = document.querySelector("#oc-settings");
const model = form.elements.model;
const effort = form.elements.effort;
const defaultLabel = model.options[0].text;
const selectedAgent = () => form.elements.where.value.replace(/^local:/, "");

function setEfforts() {
  const levels = catalogs[selectedAgent()]?.[model.value]?.efforts || [];
  effort.replaceChildren(new Option(defaultLabel, ""));
  for (const level of levels) effort.add(new Option(level, level));
  effort.disabled = !levels.length;
}

function setAgent() {
  const agent = selectedAgent();
  const options = catalogs[agent];
  settings.hidden = !options;
  model.replaceChildren(new Option(defaultLabel, ""));
  for (const [id, item] of Object.entries(options || {})) model.add(new Option(item.label, id));
  model.disabled = model.options.length === 1;
  document.querySelector("#catalog-message").hidden = !options || !model.disabled;
  if (openCode) {
    openCode.hidden = agent !== "opencode";
    openCode.querySelector("select").disabled = openCode.hidden;
  }
  setEfforts();
}

form.addEventListener("change", event => {
  if (event.target.name === "where") setAgent();
  if (event.target === model) setEfforts();
});
setAgent();
