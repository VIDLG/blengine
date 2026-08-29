#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum CollisionError {
    #[error("failed: {message}")]
    Failed { message: String },
    #[error("caused by: {cause}")]
    Caused { cause: String },
}

#[uniffi::export]
pub fn fail_with_message() -> Result<(), CollisionError> {
    Err(CollisionError::Failed {
        message: "expected collision".to_owned(),
    })
}

uniffi::setup_scaffolding!();
