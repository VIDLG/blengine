use std::{
    sync::{Arc, Condvar, Mutex},
    time::Duration,
};

#[doc(hidden)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WindowsHilCallback {
    CharacteristicRead,
    CharacteristicWrite,
    CccdWrite,
    NotificationValue,
}

#[doc(hidden)]
pub struct WindowsHilCallbackGate {
    generation: Arc<CallbackGateGeneration>,
}

impl WindowsHilCallbackGate {
    pub fn generation(&self) -> u64 {
        self.generation.generation
    }

    pub fn wait_entered(&self, timeout: Duration) -> bool {
        self.generation.wait_entered(timeout)
    }

    pub fn release(&self) {
        self.generation.release();
    }
}

impl Drop for WindowsHilCallbackGate {
    fn drop(&mut self) {
        self.release();
    }
}

pub(crate) struct CallbackGateBarrier {
    state: Mutex<CallbackGateBarrierState>,
}

struct CallbackGateBarrierState {
    next_generation: u64,
    current: Option<Arc<CallbackGateGeneration>>,
    closed: bool,
}

struct CallbackGateGeneration {
    generation: u64,
    callback: WindowsHilCallback,
    state: Mutex<CallbackGateGenerationState>,
    changed: Condvar,
}

struct CallbackGateGenerationState {
    entered: bool,
    released: bool,
}

impl CallbackGateBarrier {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(CallbackGateBarrierState {
                next_generation: 1,
                current: None,
                closed: false,
            }),
        }
    }

    pub(crate) fn arm(&self, callback: WindowsHilCallback) -> WindowsHilCallbackGate {
        let (generation, stale, closed) = {
            let mut state = self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            let generation = state.next_generation;
            state.next_generation = state.next_generation.wrapping_add(1).max(1);
            let generation = Arc::new(CallbackGateGeneration {
                generation,
                callback,
                state: Mutex::new(CallbackGateGenerationState {
                    entered: false,
                    released: false,
                }),
                changed: Condvar::new(),
            });
            let stale = if state.closed {
                None
            } else {
                state.current.replace(Arc::clone(&generation))
            };
            (generation, stale, state.closed)
        };

        if let Some(stale) = stale {
            stale.release();
        }
        if closed {
            generation.release();
        }
        WindowsHilCallbackGate { generation }
    }

    pub(crate) fn enter(&self, callback: WindowsHilCallback) {
        let generation = {
            let state = self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if state.closed {
                return;
            }
            state
                .current
                .as_ref()
                .filter(|generation| generation.callback == callback)
                .cloned()
        };
        if let Some(generation) = generation {
            generation.enter_and_wait();
        }
    }

    pub(crate) fn close(&self) {
        let current = {
            let mut state = self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            state.closed = true;
            state.current.take()
        };
        if let Some(current) = current {
            current.release();
        }
    }
}

impl Drop for CallbackGateBarrier {
    fn drop(&mut self) {
        self.close();
    }
}

impl CallbackGateGeneration {
    fn enter_and_wait(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.entered || state.released {
            return;
        }
        state.entered = true;
        self.changed.notify_all();
        while !state.released {
            state = self
                .changed
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
    }

    fn wait_entered(&self, timeout: Duration) -> bool {
        let state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (state, _) = self
            .changed
            .wait_timeout_while(state, timeout, |state| !state.entered && !state.released)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.entered
    }

    fn release(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.released = true;
        self.changed.notify_all();
    }
}

#[cfg(test)]
mod tests {
    use std::{
        sync::{Arc, mpsc},
        thread,
        time::Duration,
    };

    use super::*;

    const ENTER_TIMEOUT: Duration = Duration::from_secs(1);
    const NO_EXIT_WAIT: Duration = Duration::from_millis(50);

    #[test]
    fn generations_are_per_barrier_and_cover_all_callback_kinds() {
        let first = CallbackGateBarrier::new();
        let second = CallbackGateBarrier::new();
        let callbacks = [
            WindowsHilCallback::CharacteristicRead,
            WindowsHilCallback::CharacteristicWrite,
            WindowsHilCallback::CccdWrite,
            WindowsHilCallback::NotificationValue,
        ];

        for (index, callback) in callbacks.into_iter().enumerate() {
            let gate = first.arm(callback);
            assert_eq!(gate.generation(), index as u64 + 1);
            gate.release();
        }
        assert_eq!(
            second
                .arm(WindowsHilCallback::CharacteristicRead)
                .generation(),
            1
        );
    }

    #[test]
    fn stale_generation_release_does_not_open_the_current_gate() {
        let barrier = Arc::new(CallbackGateBarrier::new());
        let stale = barrier.arm(WindowsHilCallback::CharacteristicRead);
        let stale_barrier = Arc::clone(&barrier);
        let stale_thread = thread::spawn(move || {
            stale_barrier.enter(WindowsHilCallback::CharacteristicRead);
        });
        assert!(stale.wait_entered(ENTER_TIMEOUT));

        let current = barrier.arm(WindowsHilCallback::CharacteristicWrite);
        stale_thread.join().unwrap();
        let (exited_sender, exited_receiver) = mpsc::channel();
        let current_barrier = Arc::clone(&barrier);
        let current_thread = thread::spawn(move || {
            current_barrier.enter(WindowsHilCallback::CharacteristicWrite);
            exited_sender.send(()).unwrap();
        });
        assert!(current.wait_entered(ENTER_TIMEOUT));

        stale.release();
        assert!(exited_receiver.recv_timeout(NO_EXIT_WAIT).is_err());
        current.release();
        exited_receiver.recv_timeout(ENTER_TIMEOUT).unwrap();
        current_thread.join().unwrap();
    }

    #[test]
    fn wait_entered_times_out_and_gate_drop_releases_an_entered_callback() {
        let barrier = Arc::new(CallbackGateBarrier::new());
        let gate = barrier.arm(WindowsHilCallback::CccdWrite);
        assert!(!gate.wait_entered(Duration::from_millis(10)));

        let (exited_sender, exited_receiver) = mpsc::channel();
        let callback_barrier = Arc::clone(&barrier);
        let callback_thread = thread::spawn(move || {
            callback_barrier.enter(WindowsHilCallback::CccdWrite);
            exited_sender.send(()).unwrap();
        });
        assert!(gate.wait_entered(ENTER_TIMEOUT));
        drop(gate);

        exited_receiver.recv_timeout(ENTER_TIMEOUT).unwrap();
        callback_thread.join().unwrap();
    }
}
