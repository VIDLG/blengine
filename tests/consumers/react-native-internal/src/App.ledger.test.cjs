// Hardware-free ledger for the private pending-read reload gate. Run with node --test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, 'App.tsx'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const contractSource = fs.readFileSync(path.join(__dirname, 'rf_contract.ts'), 'utf8');
const contractCompiled = ts.transpileModule(contractSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
const contractModule = new Module(path.join(__dirname, 'rf_contract.ts'), module);
contractModule.filename = path.join(__dirname, 'rf_contract.ts');
contractModule.paths = module.paths;
contractModule._compile(contractCompiled, contractModule.filename);
const linkingListeners = new Set();
const notificationName = 'dev.blehub.rn.reload.12345678-1234-4234-8234-123456789abc';
const armURL = 'blehub-rn-spike://arm/12345678-1234-4234-8234-123456789abc';
function deliverURL(url) {
  for (const listener of linkingListeners) listener({ url });
}
const appModule = new Module(path.join(__dirname, 'App.tsx'), module);
appModule.filename = path.join(__dirname, 'App.tsx');
appModule.paths = module.paths;
appModule.require = (name) => {
  if (name === 'react') return {};
  if (name === 'react-native') return {
    Settings: { get: () => undefined },
    Linking: { addEventListener: (type, listener) => {
      assert.equal(type, 'url');
      linkingListeners.add(listener);
      return { remove: () => { linkingListeners.delete(listener); } };
    } },
    StyleSheet: { create: (styles) => styles },
  };
  if (name === '@blehub/react-native-internal') return {};
  if (name === './rf_contract') return contractModule.exports;
  return require(name);
};
appModule._compile(compiled, appModule.filename);
const { runRfPendingReadReload } = appModule.exports;
const serviceUuid = 'fd100000-0000-4000-8000-000000000000';
const uuids = ['fff10000', 'fff20000', 'fff50000', 'fff30000', 'fff40000'];
const props = [
  [true, true, true, false, false],
  [true, false, false, true, true],
  [true, false, false, false, true],
  [true, false, false, false, false],
  [false, true, true, false, false],
];

function fakeProbe(generation, staleRead = false, onRead = () => {}, onInitialize = () => {}) {
  const base = generation === 1 ? 12000 : 13000;
  const id = (offset) => String(base + offset);
  let listener;
  const emit = (event) => listener(event);
  const complete = (commandId) => emit({ kind: 'commandCompleted', commandId });
  const characteristic = (index) => ({
    id: String(20 + index), uuid: `${uuids[index]}-0000-4000-8000-000000000000`,
    readable: props[index][0], writableWithResponse: props[index][1],
    writableWithoutResponse: props[index][2], notifiable: props[index][3],
    indicatable: props[index][4],
    descriptors: index === 1 || index === 2
      ? [{ id: String(30 + index), uuid: '00002902-0000-1000-8000-00805f9b34fb' }] : [],
  });
  return {
    inject(event) { emit(event); },
    setRfScanListener(callback) { listener = callback; },
    removeRfScanListener() { listener = () => {}; },
    initialize(commandId) {
      assert.equal(commandId, id(0));
      onInitialize();
      emit({ kind: 'adapterSnapshotChanged', adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      complete(commandId);
    },
    startRfScan(commandId, session, uuid) {
      assert.deepEqual([commandId, session, uuid], [id(2), id(1), serviceUuid]);
      emit({ kind: 'scanStarted', commandId, scanSessionId: session });
      complete(commandId);
      emit({ kind: 'advertisementObserved', scanSessionId: session, peripheralId: '42', localName: 'BleHubAttHoldDK' });
    },
    stopRfScan(commandId, session) {
      assert.deepEqual([commandId, session], [id(3), id(1)]);
      emit({ kind: 'scanStopped', commandId, scanSessionId: session });
      complete(commandId);
    },
    connectRfPeripheral(commandId, connectionId, peripheral) {
      assert.deepEqual([commandId, connectionId, peripheral], [id(5), id(4), '42']);
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connecting' });
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connected' });
      complete(commandId);
    },
    discoverRfGatt(commandId, connectionId, session) {
      assert.deepEqual([commandId, connectionId, session], [id(7), id(4), id(6)]);
      emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId: session,
        gattServices: [{ id: '19', uuid: serviceUuid, primary: true,
          characteristics: uuids.map((_, index) => characteristic(index)) }] });
      complete(commandId);
    },
    readRfCharacteristic(commandId, connectionId, session, characteristicId) {
      assert.deepEqual([commandId, connectionId, session, characteristicId], [id(8), id(4), id(6), '20']);
      onRead();
      if (generation === 2) {
        if (staleRead) emit({ kind: 'characteristicRead', commandId: '12008',
          connectionId: '12004', gattSessionId: '12006', characteristicId, value: new Uint8Array([0]).buffer });
        else {
          emit({ kind: 'characteristicRead', commandId, connectionId, gattSessionId: session,
            characteristicId, value: new Uint8Array([0]).buffer });
          complete(commandId);
        }
      }
    },
  };
}

// No RF and no Jest dependency: schedule observation delays on the next tick.
const realTimeout = global.setTimeout;
async function waitForMarker(ledger, marker) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (ledger.some((line) => line.includes(marker))) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(`missing marker ${marker}: ${ledger.join(' | ')}`);
}

test('generation 1 does not await read terminal; generation 2 waits for the matching Linking URL', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const first = [];
    await runRfPendingReadReload(fakeProbe(1), serviceUuid, notificationName, 1, (line) => first.push(line),
      () => first.some((line) => line.includes(':read-dispatched:')));
    assert.match(first.at(-1), /^rf-pending-read-reload:generation=1:read-dispatched:commandId=12008:/);
    assert.ok(!first.some((line) => line.includes(':read:passed:') || line.includes(':failed:')));
    assert.equal(linkingListeners.size, 0);

    const second = [];
    let reads = 0;
    const result = runRfPendingReadReload(fakeProbe(2, false, () => { reads += 1; },
      () => assert.equal(linkingListeners.size, 1)),
      serviceUuid, notificationName, 2, (line) => second.push(line), () => false);
    await waitForMarker(second, 'generation=2:awaiting-arm:commandId=13008:');
    assert.equal(reads, 0);
    assert.ok(!second.some((line) => line.includes('generation=2:read-dispatched:')));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(reads, 0);
    deliverURL(armURL);
    await result;
    assert.ok(second.includes('rf-pending-read-reload:generation=2:arm-js-observed:source=linking:url=matched'));
    assert.equal(linkingListeners.size, 0);
    assert.equal(reads, 1);
    assert.ok(second.findIndex((line) => line.includes(':awaiting-arm:')) < second.findIndex((line) => line.includes(':read-dispatched:')));
    assert.match(second.at(-1), /^rf-pending-read-reload:generation=2:read:passed:commandId=13008:/);
    assert.ok(!second.some((line) => line.includes(':failed:')));
  } finally { global.setTimeout = realTimeout; }
});

test('wrong per-attempt URL fails closed without a read', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    let reads = 0;
    const result = runRfPendingReadReload(fakeProbe(2, false, () => { reads += 1; }),
      serviceUuid, notificationName, 2, (line) => ledger.push(line), () => false);
    await waitForMarker(ledger, 'generation=2:awaiting-arm:');
    deliverURL('blehub-rn-spike://arm/00000000-0000-4000-8000-000000000000');
    await result;
    assert.equal(reads, 0);
    assert.match(ledger.at(-1), /generation=2:failed:unexpected arm URL/);
    assert.equal(linkingListeners.size, 0);
  } finally { global.setTimeout = realTimeout; }
});

test('duplicate matching URL before read dispatch fails closed', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    let reads = 0;
    const result = runRfPendingReadReload(fakeProbe(2, false, () => { reads += 1; }),
      serviceUuid, notificationName, 2, (line) => ledger.push(line), () => false);
    await waitForMarker(ledger, 'generation=2:awaiting-arm:');
    deliverURL(armURL);
    deliverURL(armURL);
    await result;
    assert.equal(reads, 0);
    assert.match(ledger.at(-1), /generation=2:failed:duplicate arm URL/);
    assert.equal(linkingListeners.size, 0);
  } finally { global.setTimeout = realTimeout; }
});

test('generation 2 rejects a late generation 1 read rather than counting it as success', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    const result = runRfPendingReadReload(fakeProbe(2, true), serviceUuid, notificationName, 2, (line) => ledger.push(line), () => false);
    await waitForMarker(ledger, 'generation=2:awaiting-arm:');
    deliverURL(armURL);
    await result;
    assert.match(ledger.at(-1), /generation=2:failed:previous generation event leaked/);
    assert.ok(!ledger.some((line) => line.includes(':read:passed:')));
  } finally { global.setTimeout = realTimeout; }
});

test('stale generation 1 event during arm wait fails before generation 2 read dispatch', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    let reads = 0;
    const probe = fakeProbe(2, false, () => { reads += 1; });
    const result = runRfPendingReadReload(probe, serviceUuid, notificationName, 2, (line) => ledger.push(line), () => false);
    await waitForMarker(ledger, 'generation=2:awaiting-arm:');
    probe.inject({ kind: 'characteristicRead', commandId: '12008', connectionId: '12004' });
    await result;
    assert.equal(reads, 0);
    assert.match(ledger.at(-1), /generation=2:failed:previous generation event leaked/);
  } finally { global.setTimeout = realTimeout; }
});

test('generation 2 times out without a Linking event, even after the host accepts arm', async () => {
  const realNow = Date.now;
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    let reads = 0;
    const result = runRfPendingReadReload(fakeProbe(2, false, () => { reads += 1; }),
      serviceUuid, notificationName, 2, (line) => ledger.push(line), () => false);
    await waitForMarker(ledger, 'generation=2:awaiting-arm:');
    // A native acceptance marker alone is not an arm event in the JS engine.
    Date.now = () => realNow() + 31_000;
    await result;
    assert.equal(reads, 0);
    assert.match(ledger.at(-1), /generation=2:failed:read arm timed out after 30000ms/);
    assert.equal(linkingListeners.size, 0);
  } finally {
    Date.now = realNow;
    global.setTimeout = realTimeout;
  }
});

test('generation 2 rejects even the correct URL if it arrives before awaiting-arm', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    let reads = 0;
    await runRfPendingReadReload(fakeProbe(2, false, () => { reads += 1; }, () => deliverURL(armURL)),
      serviceUuid, notificationName, 2, (line) => ledger.push(line), () => false);
    assert.equal(reads, 0);
    assert.match(ledger.at(-1), /generation=2:failed:arm URL before awaiting-arm/);
    assert.ok(!ledger.some((line) => line.includes(':read:passed:')));
    assert.equal(linkingListeners.size, 0);
  } finally { global.setTimeout = realTimeout; }
});

test('generation 2 cancellation while unarmed never dispatches read', async () => {
  global.setTimeout = (fn) => realTimeout(fn, 0);
  try {
    const ledger = [];
    let cancelled = false;
    let reads = 0;
    const result = runRfPendingReadReload(fakeProbe(2, false, () => { reads += 1; }),
      serviceUuid, notificationName, 2, (line) => ledger.push(line), () => cancelled);
    await waitForMarker(ledger, 'generation=2:awaiting-arm:');
    cancelled = true;
    deliverURL(armURL);
    await result;
    assert.equal(reads, 0);
    assert.ok(!ledger.some((line) => line.includes(':read-dispatched:') || line.includes(':read:passed:')));
  } finally { global.setTimeout = realTimeout; }
});

test('missing per-attempt notification name fails without registering an arm listener', async () => {
  const ledger = [];
  await runRfPendingReadReload(fakeProbe(2), serviceUuid, undefined, 2,
    (line) => ledger.push(line), () => false);
  assert.match(ledger.at(-1), /generation=2:failed:rfReloadNotificationName must contain a canonical UUID/);
  assert.equal(linkingListeners.size, 0);
});

test('native gate retains observer after reload post; normal reload remains one-shot', () => {
  const swift = fs.readFileSync(path.join(__dirname, '../ios/BleHubNitroHost/AppDelegate.swift'), 'utf8');
  const handler = swift.slice(swift.indexOf('fileprivate func triggerExternalReload()'), swift.indexOf('  deinit {'));
  assert.match(handler, /case "reload":\s*Self\.writeRuntimeMarker\("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:notification-received"\)\s*RCTTriggerReloadCommandListeners/);
  assert.match(swift, /initialProperties\["rfReloadNotificationName"\] = pendingReadNotificationName/);
  assert.match(swift, /URL\(string: "blehub-rn-spike:\/\/arm\/\\\(uuid\.uuidString\.lowercased\(\)\)"\)/);
  assert.match(handler, /case "arm":\s*Self\.writeRuntimeMarker\("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:arm-notification-received"\)\s*guard let armURL = pendingReadArmURL,\s*RCTLinkingManager\.application\(UIApplication\.shared, open: armURL, options: \[:\]\) else/);
  assert.match(handler, /Self\.writeRuntimeMarker\("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:arm-linking-dispatched:accepted=true"\)\s*removeExternalReloadObserver\(\)/);
  assert.ok(!swift.includes('rfPendingReadReloadArmed'));
  assert.match(handler, /guard externalReloadEnabled else \{ return \}\s*removeExternalReloadObserver\(\)\s*let ready = reloadLock\.withLock \{ reloadReadyCount == 1 \}/);
  assert.match(handler, /RCTTriggerReloadCommandListeners\("BleHub host-triggered reload spike"\)/);
});
