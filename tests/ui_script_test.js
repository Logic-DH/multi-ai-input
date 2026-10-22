const fs = require("fs");

const html = fs.readFileSync("web/index.html", "utf8");
const match = html.match(/<script>([\s\S]*)<\/script>/);
if (!match) {
  throw new Error("script block not found");
}
new Function(match[1]);
if (!html.includes("grid-template-columns:repeat(2,minmax(0,1fr))")) {
  throw new Error("browser selector is not 2x2");
}
if (!html.includes('assets/css/codex-parallel-v12.css') ||
    !fs.existsSync('web/assets/css/codex-parallel-v12.css')) {
  throw new Error('Codex parallel visual layer is missing');
}
if (!html.includes('assets/css/refined-neutral-v131.css') ||
    !fs.existsSync('web/assets/css/refined-neutral-v131.css') ||
    !html.includes('class="brand-mark"') ||
    !html.includes('class="brand-copy"') ||
    html.includes('class="desktop-menu"') ||
    html.includes('class="sidebar-product"')) {
  throw new Error('original product structure or refined visual layer is missing');
}
if (!fs.existsSync("web/assets/icons/360.png") || !html.includes("v=official-3")) {
  throw new Error("official 360 browser icon is missing");
}
for (const key of ["kimi", "zhipu"]) {
  if (!fs.existsSync(`web/assets/icons/${key}.png`) || !html.includes(`'${key}'`)) {
    throw new Error(`official ${key} platform icon is missing`);
  }
}
if (html.includes("ps.length<=4")) {
  throw new Error("status cards are still limited to four adaptive rows");
}
if (!html.includes(".sidebar .utility-nav{position:absolute") || !html.includes("bottom:24px")) {
  throw new Error("left utility controls are not bottom aligned");
}
if (!html.includes('class="status-signature"') || !fs.existsSync("web/assets/design-by-logic.png")) {
  throw new Error("Design by Logic signature is missing");
}
if (html.indexOf('class="status-top"') > html.indexOf('id="cards"')) {
  throw new Error("status summary was not moved above the cards");
}
if (!html.includes('assets/css/logic-space-v18.css') ||
    !fs.existsSync("web/assets/css/logic-space-v18.css") ||
    !fs.existsSync("web/assets/css/inter-v16.css")) {
  throw new Error("Logic.space V18 visual layer is missing");
}
for (const id of ["browserOptions", "browserPath", "platformList", "prompt", "attachmentInput", "chips", "launchBtn", "sendBtn", "cards"]) {
  if (!html.includes(`id="${id}"`)) {
    throw new Error(`functional UI contract missing: ${id}`);
  }
}
for (const fn of ["selectBrowser", "saveBrowserPath", "showModes", "launchSelected", "sendPrompt", "renderCards", "refreshStatuses", "showFullResponse", "hideFullResponse", "copyFullResponse", "uploadFiles", "clipboardFiles", "normalizePastedFile", "handlePromptPaste"]) {
  if (!match[1].includes(`function ${fn}`) && !match[1].includes(`async function ${fn}`)) {
    throw new Error(`functional handler missing: ${fn}`);
  }
}
if (!html.includes("addEventListener('paste'") || !html.includes("event.clipboardData") || !html.includes("直接粘贴图片和文件")) {
  throw new Error("clipboard image and file paste support is missing");
}
if (!html.includes('onclick="refreshStatuses()"') || !html.includes("↻ 重新检测")) {
  throw new Error("status refresh does not trigger a real page inspection");
}
if (!html.includes('id="monitorState"') || !html.includes("自动监测中")) {
  throw new Error("continuous platform monitoring indicator is missing");
}
if (!html.includes('id="responseModal"') || !html.includes("完整回复") || !html.includes("full_text")) {
  throw new Error("full response expansion is missing");
}
for (const id of ['gridViewBtn', 'listViewBtn']) {
  if (!html.includes(`id="${id}"`)) {
    throw new Error(`response layout control missing: ${id}`);
  }
}
if (html.includes('class="workspace-title"') || html.includes('class="workspace-subtitle"')) {
  throw new Error('removed workspace title still exists');
}
if (html.indexOf('id="launchBtn"') < html.indexOf('class="composer"')) {
  throw new Error('launch and layout controls are not in the lower-right composer area');
}
for (const token of ["function defaultResponseLayout", "count>=5?'list':'grid'", "function setResponseLayout", "cards.dataset.layout=responseLayout", "s.full_text||s.preview", "hasResponse?'has-response':'is-empty'"]) {
  if (!html.includes(token)) {
    throw new Error(`parallel response behavior missing: ${token}`);
  }
}
const parallelCss = fs.readFileSync('web/assets/css/codex-parallel-v12.css', 'utf8');
const refinedCss = fs.readFileSync('web/assets/css/refined-neutral-v131.css', 'utf8');
for (const token of [
  '.layout.parallel-shell',
  'grid-template-columns: minmax(276px, 310px) minmax(0, 1fr)',
  '.workspace .cards.count-1',
  '.workspace .cards.count-2',
  '.workspace .cards.count-3',
  '.workspace .cards.count-4',
  '.workspace .cards[data-layout="grid"].count-5',
  '.workspace .cards[data-layout="list"]',
  '.workspace .cards[data-layout="list"] .card.is-empty',
  '.workspace .cards[data-layout="list"] .card.has-response',
  '.composer-bottom-actions'
]) {
  if (!parallelCss.includes(token)) {
    throw new Error(`parallel layout CSS missing: ${token}`);
  }
}
for (const token of [
  'height: 48px',
  'grid-template-columns: minmax(0, 1fr) auto',
  '.workspace-top > .status-top',
  '.selection-toolbar',
  'width: min(1080px, calc(100% - 40px))',
  'border-radius: 20px'
]) {
  if (!refinedCss.includes(token)) {
    throw new Error(`refined neutral CSS missing: ${token}`);
  }
}
if (!parallelCss.includes('grid-template-rows: repeat(3, minmax(0, 1fr))') ||
    !parallelCss.includes('grid-template-rows: repeat(2, calc((100% - 9px) / 2))') ||
    !parallelCss.includes('grid-auto-rows: calc((100% - 9px) / 2)')) {
  throw new Error('three-platform vertical layout or six-per-screen grid limit is missing');
}
for (const signal of ["page_state", "page_label", "login_state", "login_label", "input_state", "input_label"]) {
  if (!html.includes(signal)) {
    throw new Error(`capability signal missing from UI: ${signal}`);
  }
}
if (!html.includes("page-tag") || !html.includes("input-tag")) {
  throw new Error("page and input capability badges are missing");
}
console.log("UI_SCRIPT_OK");
