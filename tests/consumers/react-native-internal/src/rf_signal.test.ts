import { Linking } from 'react-native';
import { parseRfControl, parseRfControls, RfSignalBarrier, rfSignalUri, type RfControl } from './rf_signal';

const control: RfControl = { session: '12345678-1234-4234-8234-123456789abc', flow: 10, mode: 'rf-write-reject', token: '11111111-1111-4111-8111-111111111111' };
let listener: (event: { url: string }) => void;
beforeEach(() => {
  jest.spyOn(Linking, 'addEventListener').mockImplementation((_type, callback) => { listener = callback; return { remove: jest.fn() } as unknown as ReturnType<typeof Linking.addEventListener>; });
});
afterEach(() => jest.restoreAllMocks());

test('standalone pair is exact and continuous10 remains exactly the original two controls', () => {
  expect(parseRfControl(JSON.stringify(control), 'rf-write-reject')).toEqual(control);
  const cancellation = { ...control, flow: 8, mode: 'rf-cancellation' };
  const watchdog = { ...control, flow: 9, mode: 'rf-watchdog', token: '22222222-2222-4222-8222-222222222222' };
  expect(parseRfControls(JSON.stringify({ controls: [cancellation, watchdog] }))).toEqual([cancellation, watchdog]);
  for (const value of [{ ...control, flow: 8 }, { ...control, flow: '10' }, { ...control, token: 'BAD' },
    { ...control, session: control.session.toUpperCase() }, { ...control, extra: true }, { ...control, mode: 'rf-watchdog' }]) {
    expect(() => parseRfControl(JSON.stringify(value), 'rf-write-reject')).toThrow();
  }
  for (const controls of [[control, watchdog], [cancellation, control], [cancellation, watchdog, control]]) {
    expect(() => parseRfControls(JSON.stringify({ controls }))).toThrow();
  }
  expect(() => parseRfControl(JSON.stringify(control))).toThrow();
});

test('CCC admits only its standalone flow11 metadata and exact seven request/ACK pairs', async () => {
  const ccc: RfControl = { ...control, flow: 11, mode: 'rf-ccc-enable-reject' };
  expect(parseRfControl(JSON.stringify(ccc), ccc.mode)).toEqual(ccc);
  for (const invalid of [{ ...ccc, flow: 10 }, { ...ccc, flow: '11' }, { ...ccc, extra: true },
    { ...ccc, mode: control.mode }, { ...ccc, token: 'BAD' }]) {
    expect(() => parseRfControl(JSON.stringify(invalid), ccc.mode)).toThrow();
  }
  expect(() => parseRfControl(JSON.stringify(ccc), control.mode)).toThrow();
  expect(() => parseRfControls(JSON.stringify({ controls: [ccc, control] }))).toThrow();
  const pairs = [['arm', 'armed'], ['enable-dispatched', 'held'], ['reject', 'rejected'],
    ['recovery-enabled', 'value-sent'], ['recovery-notified', 'disable-allowed'], ['disabled', 'silence-started'], ['silence-done', 'close-allowed']] as const;
  for (let stop = 0; stop < pairs.length; stop++) {
    for (const fault of ['early', 'duplicate', 'foreign', 'wrong-phase', 'query', 'fragment', 'slash', 'reload']) {
      const barrier = new RfSignalBarrier(ccc, () => {});
      for (const [request, ack] of pairs.slice(0, stop)) {
        barrier.expect(ack); barrier.ready(request); listener({ url: rfSignalUri(ccc, ack) });
        await barrier.wait(() => false, () => {});
      }
      const [request, ack] = pairs[stop];
      barrier.expect(ack);
      // An old write/read/suite request cannot open any CCC boundary.
      for (const alias of ['write-dispatched', 'read-dispatched', 'recovery-done', 'release', 'recover'] as const) {
        expect(() => barrier.ready(alias)).toThrow();
      }
      if (fault !== 'early') barrier.ready(request);
      const url = rfSignalUri(ccc, ack);
      if (fault === 'duplicate') listener({ url });
      listener({ url: fault === 'foreign' ? url.replace('/11/', '/10/') : fault === 'wrong-phase' ? rfSignalUri(ccc, pairs[(stop + 1) % pairs.length][1])
        : fault === 'query' ? url + '?x=1' : fault === 'fragment' ? url + '#x' : fault === 'slash' ? url + '/'
          : fault === 'reload' ? 'blehub-rn-spike://reload' : url });
      await expect(barrier.wait(() => false, () => {})).rejects.toThrow();
      expect(() => barrier.remove()).toThrow();
    }
  }
});

test('four explicit requests and ACKs retain ordered markers; read request aliases cannot authorize a write', async () => {
  const report = jest.fn();
  const barrier = new RfSignalBarrier(control, report);
  for (const [request, ack] of [['arm', 'armed'], ['write-dispatched', 'held'], ['reject', 'rejected'], ['recovery-done', 'close-allowed']] as const) {
    barrier.expect(ack);
    if (ack === 'held') expect(() => barrier.ready('read-dispatched')).toThrow();
    barrier.ready(request);
    listener({ url: rfSignalUri(control, ack) });
    await barrier.wait(() => false, () => {});
  }
  expect(report.mock.calls.map(([line]) => line.split(':phase=')[1])).toEqual([
    'registered', 'arm', 'armed', 'write-dispatched', 'held', 'reject', 'rejected', 'recovery-done', 'close-allowed',
  ]);
  barrier.remove();
});

test.each(['early', 'duplicate', 'foreign-token', 'wrong-phase', 'query', 'fragment', 'slash', 'reload'])('%s signal fails closed', async (fault) => {
  const barrier = new RfSignalBarrier(control, () => {});
  barrier.expect('armed');
  if (fault !== 'early') barrier.ready('arm');
  const url = rfSignalUri(control, 'armed');
  if (fault === 'duplicate') listener({ url });
  listener({ url: fault === 'query' ? url + '?x=1' : fault === 'fragment' ? url + '#x' : fault === 'slash' ? url + '/'
    : fault === 'foreign-token' ? url.replace(control.token, control.session) : fault === 'wrong-phase' ? rfSignalUri(control, 'held')
      : fault === 'reload' ? 'blehub-rn-spike://reload' : url });
  await expect(barrier.wait(() => false, () => {})).rejects.toThrow();
  expect(() => barrier.remove()).toThrow();
});
