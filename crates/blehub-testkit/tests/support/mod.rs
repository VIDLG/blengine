#![allow(dead_code)]

use blehub::{
    BleErrorCategory, BleEvent, BleEventError, CommandId, EventIngressStatus, ScanSessionId,
};
use blehub_testkit::{CompletionExpectation, FakeBackendHandle};

pub(crate) fn scan_started(command_id: CommandId, scan_session_id: ScanSessionId) -> BleEvent {
    BleEvent::ScanStarted {
        command_id,
        scan_id: scan_session_id,
    }
}

pub(crate) fn succeeded(command_id: CommandId) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: None,
    }
}

pub(crate) fn failed(command_id: CommandId) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: Some(BleEventError {
            category: BleErrorCategory::Backend,
            message: "scripted completion failure".to_owned(),
            platform_details: None,
        }),
    }
}

pub(crate) fn advance_success(handle: &FakeBackendHandle, command_id: CommandId) {
    assert_eq!(handle.advance_all(), vec![EventIngressStatus::Delivered; 2]);
    handle.finish_completion(command_id, CompletionExpectation::SucceededWithDomain);
}
