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
        self.lock.withLock { self.pending -= 1 }
      }
      guard !self.lock.withLock({ self.discardPending }) else { return }
      self.deliver(event)
    }
    return true
  }

  func close() {
    lock.withLock {
      accepting = false
      discardPending = true
    }
  }
}
