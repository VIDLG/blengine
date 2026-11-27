// React Compiler runs first in the pipeline because it analyses original source, before any other
// transform. Babel applies `plugins` before `presets`, so listing it here is what keeps that order.
// React 19 needs no options; `target` only matters when the runtime is React 17 or 18.
module.exports = {
  presets: ['module:@react-native/babel-preset'],
  plugins: ['babel-plugin-react-compiler'],
}
