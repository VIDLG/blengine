use std::{
    io::{self, Read},
    os::windows::io::AsRawHandle,
    process::{Child, ChildStderr, ChildStdout, Command, ExitStatus, Output, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

use windows::{
    Win32::{
        Foundation::HANDLE,
        System::JobObjects::{AssignProcessToJobObject, CreateJobObjectW, TerminateJobObject},
    },
    core::{Error as WindowsError, Owned},
};

use super::ExampleError;

const PROCESS_POLL_INTERVAL: Duration = Duration::from_millis(50);
const CAPTURE_GRACE: Duration = Duration::from_secs(2);
const REAP_GRACE: Duration = Duration::from_secs(5);
const TASKKILL_TIMEOUT: Duration = Duration::from_secs(5);

pub(super) fn run_fixture_recipe(
    recipe: &str,
    arguments: &[&str],
    timeout: Duration,
) -> Result<Output, ExampleError> {
    let mut child = Command::new("pixi")
        .args(["run", "just", "peripheral", recipe])
        .args(arguments)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let process_id = child.id();
    let job = ProcessJob::attach(&child);
    let stdout = child
        .stdout
        .take()
        .ok_or("fixture subprocess stdout was not captured")?;
    let stderr = child
        .stderr
        .take()
        .ok_or("fixture subprocess stderr was not captured")?;
    let mut capture = BoundedOutputCapture::new(stdout, stderr);
    let deadline = Instant::now() + timeout;

    loop {
        if let Some(status) = child.try_wait()? {
            capture.drain_for(CAPTURE_GRACE);
            if capture.streams_closed() {
                return capture.into_output(status).map_err(Into::into);
            }

            let tree_cleanup = match &job {
                Ok(job) => job.terminate().map_err(|error| error.to_string()),
                Err(error) => Err(format!("process Job Object was unavailable: {error}")),
            };
            capture.drain_for(CAPTURE_GRACE);
            let captured = capture.finish();
            return Err(format!(
                "fixture recipe {recipe} exited with {status}, but its output pipes did not close within {CAPTURE_GRACE:?}; a descendant likely retained inherited pipe handles; process-tree cleanup: {}; {}",
                format_result(&tree_cleanup),
                captured.diagnostic(),
            )
            .into());
        }
        if Instant::now() >= deadline {
            let tree_termination = terminate_process_tree(process_id, job.as_ref());
            let reap_result = reap_child(&mut child);
            capture.drain_for(CAPTURE_GRACE);
            let captured = capture.finish();
            return Err(format!(
                "fixture recipe {recipe} timed out after {timeout:?}; process-tree termination: {}; direct-child reap: {}; {}",
                format_result(&tree_termination),
                format_result(&reap_result),
                captured.diagnostic(),
            )
            .into());
        }
        sleep_until_poll(deadline);
    }
}

fn terminate_process_tree(
    process_id: u32,
    job: Result<&ProcessJob, &WindowsError>,
) -> Result<(), String> {
    match job {
        Ok(job) => match job.terminate() {
            Ok(()) => Ok(()),
            Err(job_error) => run_taskkill(process_id).map_err(|taskkill_error| {
                format!(
                    "Job Object termination failed: {job_error}; bounded taskkill fallback also failed: {taskkill_error}"
                )
            }),
        },
        Err(job_setup_error) => run_taskkill(process_id).map_err(|taskkill_error| {
            format!(
                "Job Object assignment failed: {job_setup_error}; bounded taskkill fallback also failed: {taskkill_error}"
            )
        }),
    }
}

fn reap_child(child: &mut Child) -> Result<(), String> {
    match wait_for_exit(child, REAP_GRACE).map_err(|error| error.to_string())? {
        Some(_) => Ok(()),
        None => {
            let kill_result = child.kill().map_err(|error| error.to_string());
            let status = wait_for_exit(child, REAP_GRACE).map_err(|error| error.to_string())?;
            match (kill_result, status) {
                (_, Some(_)) => Ok(()),
                (Err(error), None) => Err(format!(
                    "direct child did not exit within {REAP_GRACE:?}; direct kill also failed: {error}"
                )),
                (Ok(()), None) => Err(format!(
                    "direct child did not exit within {REAP_GRACE:?} after direct kill"
                )),
            }
        }
    }
}

fn run_taskkill(process_id: u32) -> Result<(), String> {
    let mut child = Command::new("taskkill")
        .args(["/PID", &process_id.to_string(), "/T", "/F"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("could not start taskkill: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or("taskkill stdout was not captured")?;
    let stderr = child
        .stderr
        .take()
        .ok_or("taskkill stderr was not captured")?;
    let mut capture = BoundedOutputCapture::new(stdout, stderr);

    let status = match wait_for_exit(&mut child, TASKKILL_TIMEOUT)
        .map_err(|error| error.to_string())?
    {
        Some(status) => status,
        None => {
            let kill_result = child.kill().map_err(|error| error.to_string());
            let reap_result = match wait_for_exit(&mut child, REAP_GRACE) {
                Ok(Some(_)) => Ok(()),
                Ok(None) => Err(format!(
                    "taskkill did not exit within the {REAP_GRACE:?} reap grace"
                )),
                Err(error) => Err(error.to_string()),
            };
            capture.drain_for(CAPTURE_GRACE);
            let captured = capture.finish();
            return Err(format!(
                "taskkill exceeded {TASKKILL_TIMEOUT:?}; direct taskkill termination: {}; taskkill reap: {}; {}",
                format_result(&kill_result),
                format_result(&reap_result),
                captured.diagnostic(),
            ));
        }
    };
    capture.drain_for(CAPTURE_GRACE);
    let captured = capture.finish();
    if status.success() && captured.is_complete() {
        Ok(())
    } else {
        Err(format!(
            "taskkill exited with {status}; {}",
            captured.diagnostic()
        ))
    }
}

fn wait_for_exit(child: &mut Child, timeout: Duration) -> io::Result<Option<ExitStatus>> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }
        if Instant::now() >= deadline {
            return Ok(None);
        }
        sleep_until_poll(deadline);
    }
}

fn sleep_until_poll(deadline: Instant) {
    thread::sleep(PROCESS_POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())));
}

fn format_result<T>(result: &Result<T, String>) -> String {
    match result {
        Ok(_) => "succeeded".to_owned(),
        Err(error) => format!("failed ({error})"),
    }
}

struct ProcessJob {
    handle: Owned<HANDLE>,
}

impl ProcessJob {
    fn attach(child: &Child) -> Result<Self, WindowsError> {
        // SAFETY: Null attributes and name request a fresh unnamed Job Object. The returned handle
        // has unique ownership and is transferred directly into `Owned`.
        let handle = unsafe { Owned::new(CreateJobObjectW(None, None)?) };
        // SAFETY: Both handles are live for the duration of the call. `Owned` closes the Job Object
        // handle, while `Child` retains ownership of the process handle.
        unsafe {
            AssignProcessToJobObject(*handle, HANDLE(child.as_raw_handle()))?;
        }
        Ok(Self { handle })
    }

    fn terminate(&self) -> Result<(), WindowsError> {
        // SAFETY: `self.handle` remains valid through the call; exit code 1 is diagnostic only.
        unsafe { TerminateJobObject(*self.handle, 1) }
    }
}

#[derive(Clone, Copy)]
enum OutputStream {
    Stdout,
    Stderr,
}

impl OutputStream {
    fn name(self) -> &'static str {
        match self {
            Self::Stdout => "stdout",
            Self::Stderr => "stderr",
        }
    }
}

enum CaptureEvent {
    Chunk(OutputStream, Vec<u8>),
    Closed(OutputStream),
    Failed(OutputStream, String),
}

struct BoundedOutputCapture {
    receiver: mpsc::Receiver<CaptureEvent>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    stdout_closed: bool,
    stderr_closed: bool,
    failures: Vec<String>,
}

impl BoundedOutputCapture {
    fn new(stdout: ChildStdout, stderr: ChildStderr) -> Self {
        let (sender, receiver) = mpsc::channel();
        capture_stream(OutputStream::Stdout, stdout, sender.clone());
        capture_stream(OutputStream::Stderr, stderr, sender);
        Self {
            receiver,
            stdout: Vec::new(),
            stderr: Vec::new(),
            stdout_closed: false,
            stderr_closed: false,
            failures: Vec::new(),
        }
    }

    fn drain_for(&mut self, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !self.streams_closed() {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return;
            }
            match self.receiver.recv_timeout(remaining) {
                Ok(event) => self.record(event),
                Err(mpsc::RecvTimeoutError::Timeout) => return,
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    self.failures.push(
                        "capture channel disconnected before both output streams closed".to_owned(),
                    );
                    return;
                }
            }
        }
    }

    fn record(&mut self, event: CaptureEvent) {
        match event {
            CaptureEvent::Chunk(OutputStream::Stdout, bytes) => self.stdout.extend(bytes),
            CaptureEvent::Chunk(OutputStream::Stderr, bytes) => self.stderr.extend(bytes),
            CaptureEvent::Closed(OutputStream::Stdout) => self.stdout_closed = true,
            CaptureEvent::Closed(OutputStream::Stderr) => self.stderr_closed = true,
            CaptureEvent::Failed(stream, error) => {
                self.failures
                    .push(format!("{} capture failed: {error}", stream.name()));
                match stream {
                    OutputStream::Stdout => self.stdout_closed = true,
                    OutputStream::Stderr => self.stderr_closed = true,
                }
            }
        }
    }

    fn streams_closed(&self) -> bool {
        self.stdout_closed && self.stderr_closed
    }

    fn finish(mut self) -> CapturedOutput {
        if !self.stdout_closed {
            self.failures
                .push("stdout capture did not close before its bounded deadline".to_owned());
        }
        if !self.stderr_closed {
            self.failures
                .push("stderr capture did not close before its bounded deadline".to_owned());
        }
        CapturedOutput {
            stdout: self.stdout,
            stderr: self.stderr,
            failures: self.failures,
        }
    }

    fn into_output(self, status: ExitStatus) -> Result<Output, String> {
        let captured = self.finish();
        if captured.is_complete() {
            Ok(Output {
                status,
                stdout: captured.stdout,
                stderr: captured.stderr,
            })
        } else {
            Err(captured.diagnostic())
        }
    }
}

struct CapturedOutput {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    failures: Vec<String>,
}

impl CapturedOutput {
    fn is_complete(&self) -> bool {
        self.failures.is_empty()
    }

    fn diagnostic(&self) -> String {
        let capture = if self.failures.is_empty() {
            "output capture completed".to_owned()
        } else {
            format!("output capture issues: {}", self.failures.join("; "))
        };
        format!(
            "{capture}; stdout: {}; stderr: {}",
            String::from_utf8_lossy(&self.stdout),
            String::from_utf8_lossy(&self.stderr),
        )
    }
}

fn capture_stream(
    stream: OutputStream,
    mut reader: impl Read + Send + 'static,
    sender: mpsc::Sender<CaptureEvent>,
) {
    thread::spawn(move || {
        let mut buffer = [0_u8; 8 * 1024];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    let _ = sender.send(CaptureEvent::Closed(stream));
                    return;
                }
                Ok(count) => {
                    if sender
                        .send(CaptureEvent::Chunk(stream, buffer[..count].to_vec()))
                        .is_err()
                    {
                        return;
                    }
                }
                Err(error) => {
                    let _ = sender.send(CaptureEvent::Failed(stream, error.to_string()));
                    return;
                }
            }
        }
    });
}
