import { encodeActionLedger } from './actionLedger'

describe('the action-ledger projection', () => {
  it('states the omission count even when nothing was recorded', () => {
    expect(encodeActionLedger([], 0)).toBe('omitted=0')
  })

  it('writes one line per entry, in order, with an explicit empty detail', () => {
    expect(encodeActionLedger([
      { sequence: 1, action: 'start', result: 'succeeded' },
      { sequence: 2, action: 'notify-pull', result: 'succeeded', error: 'ended: disabled' },
    ], 0)).toBe('omitted=0\n1|start|succeeded|\n2|notify-pull|succeeded|ended: disabled')
  })

  it('reports how many earlier entries the bounded window dropped', () => {
    expect(encodeActionLedger([{ sequence: 41, action: 'close', result: 'succeeded' }], 8))
      .toBe('omitted=8\n41|close|succeeded|')
  })

  it('strips separators so one entry can never become two', () => {
    const text = encodeActionLedger([
      { sequence: 1, action: 'read', result: 'failed', error: 'bad | line\nbreak' },
    ], 0)
    expect(text.split('\n')).toEqual(['omitted=0', '1|read|failed|bad   line break'])
  })

  it('carries the failure sentence the recorder actually stored', () => {
    // The regression: the encoder used to read a `detail` field the recorder never wrote,
    // so every failed action reached the machine ledger with an empty outcome.
    const text = encodeActionLedger([
      {
        sequence: 14,
        action: 'gatt-read-wait',
        result: 'failed',
        error: 'operation/bleng.cancelled: BLeng operation was cancelled',
      },
    ], 0)
    expect(text).toBe(
      'omitted=0\n14|gatt-read-wait|failed|'
        + 'operation/bleng.cancelled: BLeng operation was cancelled',
    )
  })

  it('never emits a negative or fractional omission count', () => {
    expect(encodeActionLedger([], -3)).toBe('omitted=0')
    expect(encodeActionLedger([], 2.9)).toBe('omitted=2')
  })
})
