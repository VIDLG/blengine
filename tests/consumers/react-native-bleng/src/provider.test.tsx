import { Text } from 'react-native'
import { render } from '@testing-library/react-native'
import type { BlengCentral } from '@blengine/react-native-bleng'
import { BlengProvider, useBlengCentral } from '@blengine/react-native-bleng'

const central = {} as BlengCentral

function Probe() {
  const current = useBlengCentral()
  return <Text>{current === central ? 'same-central' : 'wrong-central'}</Text>
}

describe('BlengProvider', () => {
  it('provides the explicit central without touching native construction', () => {
    const screen = render(
      <BlengProvider central={central}>
        <Probe />
      </BlengProvider>,
    )

    expect(screen.getByText('same-central')).toBeTruthy()
  })

  it('refuses a consumer outside the provider', () => {
    expect(() => render(<Probe />)).toThrow('useBlengCentral must be used within')
  })
})
