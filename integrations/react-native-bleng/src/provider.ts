import { createContext, createElement, useContext, useMemo, type ReactNode } from 'react'
import type { BlengCentral } from '../specs/BlengCentral.nitro'
import { getBlengCentral } from './central'

const BlengCentralContext = createContext<BlengCentral | null>(null)

/**
 * Provides one central to a React subtree.
 *
 * Without the `central` prop this resolves the JS-runtime-scoped central, so mounting the provider
 * is not a lifecycle event: it never starts or closes BLE. Unmounting is likewise inert. Use
 * `closeBlengCentral()` only when the app intentionally ends its BLE session.
 */
export function BlengProvider({
  central,
  children,
}: {
  central?: BlengCentral
  children?: ReactNode
}) {
  const value = useMemo(() => central ?? getBlengCentral(), [central])
  return createElement(BlengCentralContext.Provider, { value }, children)
}

/** The central provided by the nearest `BlengProvider`. */
export function useBlengCentral(): BlengCentral {
  const central = useContext(BlengCentralContext)
  if (central === null) {
    throw new Error('useBlengCentral must be used within <BlengProvider>')
  }
  return central
}
