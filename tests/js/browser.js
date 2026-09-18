// The smallest browser the web remote will start in.
//
// Not a rendering engine and not trying to be one. It answers every call the
// page makes at load and records nothing: the question is whether the script
// runs at all, which is the one thing reading the served HTML cannot tell you.
//
// It exists because the remote has gone blank twice. Once on a real newline
// inside a JavaScript string, and once on a null dereference that only
// happened when the browser remembered a tab -- so a page that loaded
// perfectly on a fresh browser was a black rectangle on the one it was for.
// Both were shipped by tests that asked whether some text was present in the
// page, and it was: the page was broken around it.
//
// Timers are stubbed to do nothing. This is a load test, so nothing may be
// scheduled for later and nothing may keep node alive.
function El(tag) {
  this.tagName = (tag || 'div').toUpperCase();
  this.children = [];
  this.style = {};
  this.dataset = {};
  this.classList = {
    add: function () {}, remove: function () {},
    toggle: function () {}, contains: function () { return false; }
  };
  this.hidden = false;
  this.textContent = '';
  this.value = '';
  this.firstChild = {nodeValue: ''};
}
El.prototype.appendChild = function (child) {
  this.children.push(child); return child;
};
El.prototype.removeChild = function () {};
El.prototype.addEventListener = function () {};
El.prototype.removeEventListener = function () {};
El.prototype.setAttribute = function () {};
El.prototype.getAttribute = function () { return ''; };
El.prototype.querySelector = function () { return new El('div'); };
El.prototype.querySelectorAll = function () { return []; };
El.prototype.focus = function () {};
El.prototype.click = function () {};
El.prototype.getBoundingClientRect = function () {
  return {top: 0, left: 0, width: 100, height: 100};
};
El.prototype.insertBefore = function (child) { return child; };
El.prototype.contains = function () { return false; };

var made = {};
global.document = {
  hidden: false,
  body: new El('body'),
  documentElement: new El('html'),
  getElementById: function (id) {
    if (!made[id]) { made[id] = new El('div'); }
    return made[id];
  },
  createElement: function (tag) { return new El(tag); },
  createTextNode: function (text) { return {nodeValue: text}; },
  querySelector: function () { return new El('div'); },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  exitFullscreen: function () {},
  fullscreenElement: null
};
global.window = global;
global.localStorage = {
  _v: {},
  getItem: function (k) { return this._v[k] === undefined ? null : this._v[k]; },
  setItem: function (k, v) { this._v[k] = String(v); },
  removeItem: function (k) { delete this._v[k]; }
};
global.navigator = {userAgent: 'node', wakeLock: undefined};
global.location = {href: 'http://10.0.0.99:8778/', protocol: 'http:',
                   host: '10.0.0.99:8778', reload: function () {}};
global.fetch = function () {
  return Promise.resolve({
    status: 200, ok: true,
    json: function () { return Promise.resolve({}); },
    text: function () { return Promise.resolve('{}'); }
  });
};
global.alert = function () {};
global.confirm = function () { return true; };
global.matchMedia = function () {
  return {matches: false, addListener: function () {},
          addEventListener: function () {}};
};
global.requestAnimationFrame = function (fn) { return setTimeout(fn, 0); };
// Timers must not keep node alive or schedule real work: this is a load test.
global.setTimeout = function () { return 0; };
global.setInterval = function () { return 0; };
global.clearTimeout = function () {};
global.clearInterval = function () {};
