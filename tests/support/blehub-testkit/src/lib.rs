pub mod catalog;
mod conformance;
mod fake_backend;
mod fixtures;
mod projection;
mod recording_sink;
mod runner;
mod scenario;

pub use conformance::{
    BackendConformanceTracker, BackendContractViolation, CommandEventKind, CompletionExpectation,
};
pub use fake_backend::{FakeBackend, FakeBackendHandle};
pub use fixtures::{
    advertisement_observation_fixture, duplicate_gatt_database_fixture,
    unified_event_catalog_fixture,
};
pub use projection::{EventDisposition, ProjectedCommandOutcome, ReferenceProjection};
pub use recording_sink::{RecordingSink, SinkBehavior};
pub use scenario::{EventScenario, RecordedCommand, ScenarioDispatch};
