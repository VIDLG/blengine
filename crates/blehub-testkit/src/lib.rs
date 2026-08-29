mod fake_backend;
mod projection;
mod recording_sink;
mod scenario;

pub use fake_backend::{BackendContractViolation, FakeBackend, FakeBackendHandle};
pub use projection::{EventDisposition, ReferenceProjection};
pub use recording_sink::{RecordingSink, SinkBehavior};
pub use scenario::{EventScenario, ScenarioDispatch};
