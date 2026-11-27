import BleHubUniFfi
import NitroModules

/// One bounded companion-association receipt; no native handle or device identity is retained.
final class HybridBlengCompanionAssociation: HybridBlengCompanionAssociationSpec {
  private let association: SupervisedCompanionAssociation

  init(association: SupervisedCompanionAssociation) {
    self.association = association
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let association = self.association
    return blengPromise {
      BlengFailureMapping.command(await association.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let association = self.association
    return blengPromise { BlengFailureMapping.command(await association.cancelResult()) }
  }
}

/// One bounded adapter-power receipt; no native handle or device identity is retained here.
final class HybridBlengAdapterControl: HybridBlengAdapterControlSpec {
  private let control: SupervisedAdapterControl

  init(control: SupervisedAdapterControl) {
    self.control = control
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let control = self.control
    return blengPromise { BlengFailureMapping.command(await control.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let control = self.control
    return blengPromise { BlengFailureMapping.command(await control.cancelResult()) }
  }
}
