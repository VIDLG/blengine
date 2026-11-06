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
// Same synthetic control identity as App.rf-lab.test.tsx; never an RF attestation.
const cancellationControl = Object.freeze({
  session: '12345678-1234-4234-8234-123456789abc',
  flow: 8,
  mode: 'rf-cancellation',
  token: '11111111-1111-4111-8111-111111111111',
});
function deliverURL(url) {
  for (const listener of linkingListeners) listener({ url });
}
let signalExports;
const appModule = new Module(path.join(__dirname, 'App.tsx'), module);
appModule.filename = path.join(__dirname, 'App.tsx');
appModule.paths = module.paths;
appModule.require = (name) => {
  if (name === 'react') return {};
  if (name === 'react-native') return {
    Platform: { OS: 'android' },
    Linking: { addEventListener: (type, listener) => {
      assert.equal(type, 'url');
      linkingListeners.add(listener);
      return { remove: () => { linkingListeners.delete(listener); } };
    } },
    StyleSheet: { create: (styles) => styles },
  };
  if (name === '@blehub/react-native-internal') return {};
  if (['./rf_recovery_ten', './rf_full'].includes(name)) return {};
  if (name === './rf_signal') {
    if (!signalExports) {
      const filename = path.join(__dirname, 'rf_signal.ts');
      const signalModule = new Module(filename, module);
      signalModule.filename = filename;
      signalModule.require = appModule.require;
      signalModule._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS },
      }).outputText, filename);
      signalExports = signalModule.exports;
    }
    return signalExports;
  }
  if (name === './rf_runtime') return { RF_BOOT_NONCE: 'ledger-unit-boot' };
  if (name === './pending_reload_runtime') return { bindPendingReloadProbe: () => {} };
  if (name === './rf_watchdog') return { RfWatchdogLab: () => null };
  if (name === './multi_rf') return { runRfMulti: async () => {}, runRfMultiSoak: async () => {} };
  if (name === './multi_fault') return { runRfMultiFault: async () => {} };
  if (name === './rf_contract') return contractModule.exports;
  return require(name);
};
appModule._compile(compiled, appModule.filename);
const { runRfLabFlow } = appModule.exports;
function runRfPendingReadReload(...args) {
  // Each old ledger case is an independent boot; same-boot rerun/nonce guards are
  // covered by the RN Jest tests. Keep the locked Android ID route in these cases.
  appModule._compile(compiled, appModule.filename);
  args[6] ??= notificationName.slice('dev.blehub.rn.reload.'.length);
  args[7] ??= 'BleHubAttHoldDK';
  const report = args[4];
  args[4] = (line) => {
    report(line);
    if (args[3] === 1 && line.includes(':awaiting-arm:')) deliverURL(armURL);
  };
  return appModule.exports.runRfPendingReadReload(...args);
}
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
    awaitPendingReloadReady() { return Promise.resolve(); },
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

function fakeRfProbe(passive = false, cancellation = false) {
  let listener;
  let cancellationQueued = false;
  let cancellationActiveCompleted = false;
  let cancellationNativeEntered = false;
  let cancellationRead;
  let cancellationQueuedDispatched = false;
  const emit = (event) => listener?.(event);
  const complete = (commandId, error) => emit({ kind: 'commandCompleted', commandId, error });
  const characteristic = (index) => ({
    id: String(20 + index), uuid: `${uuids[index]}-0000-4000-8000-000000000000`,
    readable: props[index][0], writableWithResponse: props[index][1],
    writableWithoutResponse: props[index][2], notifiable: props[index][3],
    indicatable: props[index][4],
    descriptors: index === 1 || index === 2
      ? [{ id: String(30 + index), uuid: '00002902-0000-1000-8000-00805f9b34fb' }] : [],
  });
  return {
    enterHold() {
      assert.ok(cancellationRead, 'held signal requires an already-dispatched active read');
      assert.equal(cancellationNativeEntered, false);
      cancellationNativeEntered = true;
    },
    releaseHold() {
      assert.equal(cancellationNativeEntered, true);
      assert.equal(cancellationQueued, true, 'active read cannot complete before queued cancellation');
      assert.equal(cancellationActiveCompleted, false);
      cancellationActiveCompleted = true;
      emit({ kind: 'characteristicRead', ...cancellationRead, value: new Uint8Array([0]).buffer });
      complete(cancellationRead.commandId);
    },
    setRfScanListener(callback) { listener = callback; },
    removeRfScanListener() { listener = () => {}; },
    initialize(commandId) {
      emit({ kind: 'adapterSnapshotChanged', adapterState: 'PoweredOn', scanAuthorization: 'Authorized' });
      complete(commandId);
    },
    startRfScan(commandId, session) {
      emit({ kind: 'scanStarted', commandId, scanSessionId: session });
      complete(commandId);
      emit({ kind: 'advertisementObserved', scanSessionId: session, peripheralId: '42', localName: 'BleHubNrf52840' });
    },
    stopRfScan(commandId, session) {
      emit({ kind: 'scanStopped', commandId, scanSessionId: session });
      complete(commandId);
    },
    connectRfPeripheral(commandId, connectionId) {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connecting' });
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Connected' });
      complete(commandId);
    },
    discoverRfGatt(commandId, connectionId, session) {
      emit({ kind: 'gattDiscovered', commandId, connectionId, gattSessionId: session,
        gattServices: [{ id: '19', uuid: serviceUuid, primary: true,
          characteristics: uuids.map((_, index) => characteristic(index)) }] });
      complete(commandId);
    },
    readRfRssi(commandId, connectionId) {
      emit({ kind: 'rssiRead', commandId, connectionId, rssi: -42 });
      complete(commandId);
    },
    readRfCharacteristic(commandId, connectionId, session, characteristicId) {
      if (cancellation && commandId === '9027') {
        assert.equal(cancellationRead, undefined);
        cancellationRead = { commandId, connectionId, gattSessionId: session, characteristicId };
        return;
      }
      if (cancellation && commandId === '9028') {
        assert.equal(cancellationNativeEntered, true);
        cancellationQueuedDispatched = true;
        return;
      }
      const value = commandId === '9012' ? [0x10] : commandId === '9014' ? [0xab] : [0x00];
      emit({ kind: 'characteristicRead', commandId, connectionId, gattSessionId: session,
        characteristicId, value: new Uint8Array(value).buffer });
      complete(commandId);
    },
    writeRfCharacteristic(commandId, connectionId, session, characteristicId, value, writeType) {
      // Canonical success projection; no-response is local submission, not remote ACK.
      emit({ kind: 'characteristicWritten', commandId, connectionId, gattSessionId: session,
        characteristicId, value });
      complete(commandId);
    },
    setRfNotifications(commandId, connectionId, session, subscriptionId, characteristicId, enabled, mode) {
      if (commandId === '9018') {
        complete(commandId, { category: 'not_supported', message: 'strict Indication unavailable' });
        return;
      }
      const actualMode = mode === 'require_indication' ? 'Indication' : 'Notification';
      emit({ kind: 'notificationStateChanged', commandId, connectionId, gattSessionId: session,
        subscriptionId, characteristicId, notificationState: enabled ? 'Enabled' : 'Disabled',
        actualNotificationMode: actualMode });
      complete(commandId);
      if (enabled) {
        emit({ kind: 'notificationReceived', connectionId, gattSessionId: session, subscriptionId,
          characteristicId, value: new Uint8Array([0xef]).buffer });
        if (passive && commandId === '9015') {
          emit({ kind: 'connectionStateChanged', commandId: undefined, connectionId: '9007', connectionState: null,
            error: { category: 'connection', message: 'simulated passive disconnect' } });
        }
      }
    },
    disconnectRfPeripheral(commandId, connectionId) {
      emit({ kind: 'connectionStateChanged', commandId, connectionId, connectionState: 'Disconnected' });
      complete(commandId);
    },
    cancelOperation(targetCommandId) {
      if (!cancellation) return;
      if (targetCommandId === '9027') {
        return cancellationActiveCompleted
          ? { category: 'operation_not_found', detail: 'operation is not active' }
          : cancellationNativeEntered ? { category: 'operation_not_cancellable', detail: 'active operation is not cancellable' } : undefined;
      }
      if (targetCommandId === '9028') {
        if (cancellationQueued) return { category: 'operation_not_found', detail: 'operation is not active' };
        assert.equal(cancellationQueuedDispatched, true);
        cancellationQueued = true;
        // Like the Jest mock, queued cancellation does not release the held native read.
        complete('9028', { category: 'cancelled', message: 'queued operation cancelled' });
        return;
      }
      return { category: 'operation_not_found', detail: 'operation is not active' };
    },
    close() { return Promise.resolve(); },
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

for (const mode of [
  'rf',
  'rf-connect',
  'rf-gatt',
  'rf-io',
  'rf-rssi',
  'rf-notification',
  'rf-indication',
  'rf-passive-disconnect',
  'rf-cancellation',
]) {
  test(`rf-lab ${mode} completes the simulated RN/Nitro event ledger`, async () => {
    global.setTimeout = (fn) => realTimeout(fn, 0);
    try {
      const ledger = [];
      const cancellation = mode === 'rf-cancellation';
      const probe = fakeRfProbe(mode === 'rf-passive-disconnect', cancellation);
      const controlPrefix = `rf-control:ready:session=${cancellationControl.session}:flow=8:mode=rf-cancellation:token=${cancellationControl.token}:phase=`;
      const report = (line) => {
        ledger.push(line);
        if (!cancellation || !line.startsWith('rf-control:ready:')) return;
        assert.ok(line.startsWith(controlPrefix), 'control readiness must match the exact synthetic identity');
        const phase = line.slice(controlPrefix.length);
        const signalPhase = { arm: 'armed', 'read-dispatched': 'held', release: 'released' }[phase];
        assert.ok(signalPhase, 'unexpected cancellation control phase');
        setTimeout(() => {
          if (phase === 'read-dispatched') probe.enterHold();
          if (phase === 'release') probe.releaseHold();
          deliverURL(signalExports.rfSignalUri(cancellationControl, signalPhase));
        }, 0);
      };
      await runRfLabFlow(
        mode,
        probe,
        serviceUuid,
        report,
        () => false,
        undefined,
        cancellation ? JSON.stringify(cancellationControl) : undefined,
      );
      assert.ok(ledger.some((line) => line.startsWith(`${mode}:passed:`)), ledger.join(' | '));
      assert.ok(ledger.some((line) => line.includes(':step:initialize:passed:')), ledger.join(' | '));
      assert.ok(ledger.some((line) => line.includes(':step:close:passed:')), ledger.join(' | '));
      assert.ok(!ledger.some((line) => line.includes(':failed:')), ledger.join(' | '));
      if (cancellation) {
        assert.deepEqual(ledger.filter((line) => line.startsWith('rf-control:observed:'))
          .map((line) => line.split(':phase=')[1]), ['armed', 'held', 'released']);
        const stepIndex = (step) => ledger.findIndex((line) => line.startsWith(step));
        assert.ok(stepIndex('rf:step:cancellation-terminal:passed:') < stepIndex(controlPrefix + 'release'));
        assert.ok(stepIndex('rf-control:observed:') >= 0);
        assert.ok(ledger.findIndex((line) => line.startsWith('rf-control:observed:') && line.endsWith(':phase=released')) <
          stepIndex('rf:step:cancellation-active-completion:passed:'));
        assert.equal(linkingListeners.size, 0);
      }
    } finally {
      global.setTimeout = realTimeout;
    }
  });
}

test('rf-cancellation without control metadata still fails closed before initialization', async () => {
  const ledger = [];
  const probe = fakeRfProbe(false, true);
  probe.initialize = () => assert.fail('missing control metadata must not initialize');
  await runRfLabFlow('rf-cancellation', probe, serviceUuid, (line) => ledger.push(line), () => false);
  assert.ok(ledger.some((line) => line.startsWith('rf-cancellation:failed:cancellation requires live-attested multifunction control metadata')));
  assert.ok(!ledger.some((line) => line.startsWith('rf-cancellation:passed:')));
  assert.equal(linkingListeners.size, 0);
});

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

test('pending payload URL only requests RN reload; generic Darwin reload remains one-shot', () => {
  const swift = fs.readFileSync(path.join(__dirname, '../ios/BleHubNitroHost/AppDelegate.swift'), 'utf8');
  const controls = swift.slice(swift.indexOf('func application(_ application: UIApplication, open url:'), swift.indexOf('  private func observeRecoveryMarker'));
  assert.ok(controls.indexOf('BleHubPendingReloadHost.acceptControl') < controls.indexOf('guard recoveryIdentity'));
  assert.match(controls, /if action == "reload"[\s\S]*RCTTriggerReloadCommandListeners/);
  assert.ok(!controls.includes('.close('));
  assert.ok(!swift.includes('UserDefaults'));
  const handler = swift.slice(swift.indexOf('fileprivate func triggerExternalReload()'), swift.indexOf('  deinit {'));
  assert.match(handler, /guard externalReloadEnabled else \{ return \}\s*removeExternalReloadObserver\(\)/);
  assert.match(handler, /RCTTriggerReloadCommandListeners\("BleHub host-triggered reload spike"\)/);
});
