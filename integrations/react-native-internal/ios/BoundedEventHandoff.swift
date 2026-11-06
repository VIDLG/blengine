import Foundation

/// A finite native handoff between a synchronous sink call and its serialized
/// state queue. A rejected submission closes acceptance rather than silently
/// dropping more events. Downstream dispatch (such as Nitro's JS queue) is not
/// owned or drained by this handoff.
final class BoundedEventHandoff<Event>: @unchecked Sendable {
  private let lock = NSLock()
  private let stateQueue: DispatchQueue
  private let capacity: Int
  private let deliver: (Event) -> Void
  private var accepting = true
  private var discardPending = false
  private var pending = 0
  private var drained: [() -> Void] = []

  // A lock-stable count of accepted handlers, including the executing handler.
  // It says nothing about downstream Nitro/JS queues.
  var diagnosticPendingCount: Int { lock.withLock { pending } }

  init(
    stateQueue: DispatchQueue,
    capacity: Int,
    deliver: @escaping (Event) -> Void
  ) {
    self.stateQueue = stateQueue
    self.capacity = capacity
    self.deliver = deliver
  }

  func submit(_ event: Event) -> Bool {
    let accepted = lock.withLock { () -> Bool in
      guard accepting else { return false }
      guard pending < capacity else {
        accepting = false
        return false
      }
      pending += 1
      return true
    }
    guard accepted else { return false }

    // Delivery runs inline on the supplied state queue, not on a second
    // unbounded downstream queue.
    stateQueue.async { [weak self] in
      guard let self else { return }
      defer {
        // Occupy the slot until synchronous downstream submission has returned.
        let completions = self.lock.withLock { () -> [() -> Void] in
          self.pending -= 1
          guard self.pending == 0 else { return [] }
          let completions = self.drained
          self.drained.removeAll()
          return completions
        }
        for completion in completions {
          self.stateQueue.async(execute: DispatchWorkItem(block: completion))
        }
      }
      guard !self.lock.withLock({ self.discardPending }) else { return }
      self.deliver(event)
    }
    return true
  }

  // No queue.sync, including when called from a Rust sink or an executing
  // delivery. Acceptance closes under the same lock as submit's reservation,
  // so a reserved-but-not-yet-enqueued handler cannot escape this accounting.
  func closeAndDrain(_ completion: @escaping () -> Void) {
    let alreadyDrained = lock.withLock { () -> Bool in
      accepting = false
      discardPending = true
      guard pending != 0 else { return true }
      drained.append(completion)
      return false
    }
    if alreadyDrained {
      stateQueue.async(execute: DispatchWorkItem(block: completion))
    }
  }

  func close() {
    lock.withLock {
      accepting = false
      discardPending = true
    }
  }
}
