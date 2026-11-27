import React from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import { createBlengCentral } from '@blehub/react-native-bleng'
import type {
  BlengAdapterControlSubmission, BlengAdapterSnapshot, BlengCentral, BlengFailureInfo,
  BlengReceipt,
} from '@blehub/react-native-bleng'
import App from './App'

// Only the native factory is replaced, so the App's own dispatch, ledger and projection are the
// ones that ship.
jest.mock('@blehub/react-native-bleng', () => ({
  ...jest.requireActual('@blehub/react-native-bleng'),
  createBlengCentral: jest.fn(),
}))
const createCentral = jest.mocked(createBlengCentral)
afterEach(() => { closeTrace.mockRestore() })

const snapshot: BlengAdapterSnapshot = {
  state: 'poweredOn', scanAuthorization: 'authorized', connectionAuthorization: 'authorized',
}
/** The refusal both platforms give on the devices this project tests. */
const unsupported: BlengFailureInfo = {
  domain: 'submission', code: 'bleng.dispatch_rejected.not_supported',
  message: 'CoreBluetooth cannot change the radio power state',
}

function fixture() {
  const beginAdapterControl = jest.fn<BlengAdapterControlSubmission, [boolean]>()
  const central = {
    initialize: jest.fn().mockResolvedValue({ snapshot }),
    close: jest.fn().mockResolvedValue({ closed: true }),
    beginAdapterControl,
  }
  return { central, beginAdapterControl }
}

let current: ReturnType<typeof fixture>
let closeTrace: jest.SpyInstance
beforeEach(() => {
  jest.clearAllMocks()
  closeTrace = jest.spyOn(console, 'info').mockImplementation(() => {})
  current = fixture()
  createCentral.mockReturnValue(current.central as unknown as BlengCentral)
})

async function press(id: string) {
  await act(async () => { fireEvent.press(screen.getByTestId(id)) })
}

const fact = (key: string): string | null => {
  const text = screen.getByTestId('run-status').props.children as string
  const field = text.split(';').find(item => item.startsWith(`${key}=`))
  return field === undefined ? null : field.slice(key.length + 1)
}

/** The Probe page, on a session that never scanned: radio control needs the owner, not a link. */
async function probe() {
  render(<App />)
  await press('start-session')
  await press('workbench-probe')
  expect(fact('page')).toBe('Probe')
}

it('reports a refused radio change as the platform answer, not as a change', async () => {
  current.beginAdapterControl.mockReturnValue({ failure: unsupported })
  await probe()
  expect(fact('radio')).toBe('none')
  expect(fact('link')).toBe('none')
  await press('radio-off')
  expect(current.beginAdapterControl).toHaveBeenCalledWith(false)
  expect(fact('radio')).toBe('unsupported')
  expect(fact('radioTarget')).toBe('off')
  expect(fact('failure')).toBe('submission/bleng.dispatch_rejected.not_supported')
  // Nothing else moved: the App must not claim a radio state it was never told.
  expect(fact('scan')).toBe('idle')
  expect(fact('link')).toBe('none')
})

it('asks for the direction the operator selected, and keeps the two apart', async () => {
  current.beginAdapterControl.mockReturnValue({ failure: unsupported })
  await probe()
  await press('radio-off')
  expect(fact('radioTarget')).toBe('off')
  await press('radio-on')
  expect(current.beginAdapterControl).toHaveBeenLastCalledWith(true)
  // The second refusal is a new answer about a different request, not a carried-over one.
  expect(fact('radio')).toBe('unsupported')
  expect(fact('radioTarget')).toBe('on')
})

it('reports a platform that really accepts the request as done', async () => {
  // A device/profile owner (or a system app) is exempt from the API 33+ refusal, so this is the
  // other legitimate answer. It is the platform's receipt, never the App's own say-so.
  const wait = jest.fn<Promise<BlengReceipt>, [number]>().mockResolvedValue({})
  current.beginAdapterControl.mockReturnValue({
    control: { wait, cancel: jest.fn() },
  } as unknown as BlengAdapterControlSubmission)
  await probe()
  await press('radio-off')
  expect(wait).toHaveBeenCalledWith(5000)
  expect(fact('radio')).toBe('done')
  expect(fact('radioTarget')).toBe('off')
  expect(fact('failure')).toBe('none')
  await press('workbench-evidence')
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/adapter-control: succeeded/)
  expect(screen.getByTestId('lifecycle-evidence')).toHaveTextContent(/adapter-control-wait: succeeded/)
})

it('offers no radio control before there is an owner', async () => {
  render(<App />)
  await press('workbench-probe')
  expect(screen.getByTestId('radio-off')).toBeDisabled()
  expect(screen.getByTestId('radio-on')).toBeDisabled()
})

it('treats an operation-boundary refusal as the same platform answer', async () => {
  // Android answers the adapter power request asynchronously, so the refusal is the
  // *operation's* terminal and the supervisor composes it as `bleng.operation.not_supported`
  // rather than `bleng.dispatch_rejected.not_supported`. One answer, two shapes.
  const wait = jest.fn<Promise<BlengReceipt>, [number]>()
  current.beginAdapterControl.mockReturnValue({
    control: { wait, cancel: jest.fn() },
  } as unknown as BlengAdapterControlSubmission)
  const { BlengOperationError } = jest.requireActual('@blehub/react-native-bleng')
  wait.mockRejectedValue(new BlengOperationError({
    domain: 'operation', code: 'bleng.operation.not_supported',
    message: 'Android refused the adapter power request; it may require user confirmation',
  }))
  await probe()
  await press('radio-off')
  expect(fact('radio')).toBe('unsupported')
  expect(fact('radioTarget')).toBe('off')
  expect(fact('failure')).toBe('operation/bleng.operation.not_supported')
})
