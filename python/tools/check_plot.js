// Check the web page's plotting code.
//
// The page is one file of HTML with its JavaScript inline, so this pulls
// the script out of it, runs it against a stubbed DOM, and checks what it
// draws and what the axes are scaled to.
//
//   node tools/check_plot.js
//   node tools/check_plot.js path/to/index.html
//
// It needs nothing but node.  Exits non-zero if a check fails.

"use strict";

const fs = require("fs");
const path = require("path");

const page = process.argv[2]
  || path.join(__dirname, "..", "linkam", "static", "index.html");
const html = fs.readFileSync(page, "utf8");

const script = html.match(/<script>([\s\S]*?)<\/script>/);
if (!script) {
  console.error(`no script in ${page}`);
  process.exit(1);
}

// --------------------------------------------------------------------------- //
// A stub of the bits of the browser the script touches
// --------------------------------------------------------------------------- //

const texts = [];       // everything the page drew
const rects = [];       // every rectangle it filled

function stubContext() {
  return {
    setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    stroke() {}, fill() {}, arc() {}, setLineDash() {},
    fillText(t, x, y) { texts.push({ t: String(t), x, y }); },
    measureText(t) { return { width: String(t).length * 6 }; },
    fillRect(x, y, w, h) { rects.push({ x, y, w, h }); },
  };
}

function stubElement() {
  return {
    textContent: "", className: "", children: [], value: "1",
    addEventListener() {}, prepend() {}, removeChild() {},
    getContext: () => stubContext(),
    set innerHTML(v) {}, get innerHTML() { return ""; },
  };
}

const elements = {
  spark: {
    clientWidth: 700, clientHeight: 190, width: 0, height: 0,
    getContext: () => stubContext(),
  },
  sparkinfo: { textContent: "" },
};

global.document = {
  getElementById: (id) => elements[id] || (elements[id] = stubElement()),
  createElement: () => stubElement(),
};
global.window = { devicePixelRatio: 2, addEventListener() {} };
global.EventSource = class { constructor() {} };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve([]) });

// --------------------------------------------------------------------------- //
// The page's own code
// --------------------------------------------------------------------------- //

eval(`${script[1]}\n;globalThis.page = { state, draw, temperatureTicks, timeTicks,`
  + ` tempLabel, clockLabel, formatSpan };`);

const { state, draw, temperatureTicks, timeTicks, tempLabel, clockLabel,
        formatSpan } = globalThis.page;

// --------------------------------------------------------------------------- //
// The checks
// --------------------------------------------------------------------------- //

const results = [];

function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  results.push({ ok, name, got, want });
}

function checkThat(name, ok) {
  results.push({ ok, name });
}

// -- the temperature scale -------------------------------------------------- //
check("a 100 degree range is scaled every 20 degrees",
  temperatureTicks(0, 100, 6).ticks, [0, 20, 40, 60, 80, 100]);
check("a narrow range keeps its resolution",
  temperatureTicks(48, 60, 6).ticks, [48, 50, 52, 54, 56, 58, 60]);
check("a range below zero is scaled from the bottom",
  temperatureTicks(-40, 60, 6).ticks, [-40, -20, 0, 20, 40, 60]);
check("a flat range is still scaled",
  temperatureTicks(19.96, 20.44, 6).ticks.map((v) => tempLabel(v, 0.1)),
  ["20.0", "20.1", "20.2", "20.3", "20.4"]);
check("a label has as many decimals as the spacing needs",
  tempLabel(20.25, 0.25), "20.25");
check("a label on a whole degree has none",
  tempLabel(40, 20), "40");

// -- the time scale --------------------------------------------------------- //
const t0 = 1700000000;
check("a five minute window is scaled every minute",
  timeTicks(t0, t0 + 300, 6).step, 60);
check("the clock ticks land on whole minutes",
  timeTicks(t0, t0 + 300, 6).ticks.every((t) => t % 60 === 0), true);
check("a short window is scaled in seconds",
  timeTicks(t0, t0 + 30, 6).step, 5);
check("a long window is scaled in minutes",
  timeTicks(t0, t0 + 600, 6).step, 120);
check("a clock label is a time of day",
  /^\d\d:\d\d:\d\d$/.test(clockLabel(t0, 300)), true);
check("a clock label over a day carries the date too",
  /\d\d\/\d\d \d\d:\d\d/.test(clockLabel(t0, 90000)), true);
check("a length of time is in words",
  formatSpan(303), "5 min 3 s");

// -- the plot ---------------------------------------------------------------- //
const trend = [];
for (let i = 0; i < 300; i++) trend.push([t0 + i, 20 + i * 0.2]);
state.trend = trend;
state.target_c = 75;
draw();

const numbers = texts.filter((x) => /^-?\d+(\.\d+)?$/.test(x.t));
const clocks = texts.filter((x) => /^\d\d:\d\d:\d\d$/.test(x.t));

checkThat("the plot has a temperature scale", numbers.length >= 3);
checkThat("the plot has a time scale", clocks.length >= 3);
checkThat("the temperature numbers are left of the plot",
  numbers.every((x) => x.x < 58));
checkThat("the clock numbers are below the plot",
  clocks.every((x) => x.y > 190 - 22));
checkThat("everything is inside the canvas",
  texts.every((x) => x.x >= 0 && x.x <= 700 && x.y >= 0 && x.y <= 190));
checkThat("the setpoint is written on the plot",
  texts.some((x) => /^target /.test(x.t)));
checkThat("the units are written on the plot",
  texts.some((x) => x.t === "°C"));
checkThat("the window is written out",
  /\d\d:\d\d:\d\d . \d\d:\d\d:\d\d . \d+ min/.test(elements.sparkinfo.textContent));

// -- and the odd cases ------------------------------------------------------ //
texts.length = 0;
state.trend = [];
draw();
checkThat("an empty plot says it is waiting",
  texts.some((x) => /waiting/.test(x.t)));

texts.length = 0;
state.trend = [[t0, 20], [t0 + 1, 20]];
draw();
checkThat("a flat trace still gets a scale",
  texts.filter((x) => /^-?\d+(\.\d+)?$/.test(x.t)).length >= 2);

// --------------------------------------------------------------------------- //
// The report
// --------------------------------------------------------------------------- //

let failed = 0;
for (const r of results) {
  if (r.ok) {
    console.log(`PASS  ${r.name}`);
  } else {
    failed++;
    console.log(`FAIL  ${r.name}`);
    if (r.want !== undefined) {
      console.log(`      got  ${JSON.stringify(r.got)}`);
      console.log(`      want ${JSON.stringify(r.want)}`);
    }
  }
}
console.log(`\n${results.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
