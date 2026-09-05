// A tiny function registry that breaks the import cycles between socket.js
// and the panel modules it renders into, and between the shell/messages/
// rooms/thread siblings that used to import each other directly. A module
// calls a sibling's function through `emit` instead of importing it; app.js,
// which already sits above every module and can import all of them without
// creating a cycle, wires the real function in with `on` at startup. Same
// function, same arguments, same order, just resolved one step later.
const handlers = {};

export function on(name, fn) {
  handlers[name] = fn;
}

export function emit(name, ...args) {
  return handlers[name](...args);
}
