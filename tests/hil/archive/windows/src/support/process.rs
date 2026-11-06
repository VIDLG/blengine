use std::{
    collections::VecDeque,
    io::{self, Read},
    os::windows::io::AsRawHandle,
    process::{Child, ChildStderr, ChildStdout, Command, ExitStatus, Output, Stdio},
    sync::{Arc, Mutex, mpsc},
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
const CAPTURE_STREAM_LIMIT: usize = 64 * 1024;
const CAPTURE_HEAD_LIMIT: usize = CAPTURE_STREAM_LIMIT / 2;

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
    Closed(OutputStream),
    Failed(OutputStream, String),
}

#[derive(Default)]
struct BoundedStreamCapture {
    head: Vec<u8>,
    tail: VecDeque<u8>,
    total_bytes: usize,
}

impl BoundedStreamCapture {
    fn extend(&mut self, mut bytes: &[u8]) {
        self.total_bytes = self.total_bytes.saturating_add(bytes.len());

        let head_remaining = CAPTURE_HEAD_LIMIT.saturating_sub(self.head.len());
        let head_count = head_remaining.min(bytes.len());
        self.head.extend_from_slice(&bytes[..head_count]);
        bytes = &bytes[head_count..];

        let tail_limit = CAPTURE_STREAM_LIMIT - CAPTURE_HEAD_LIMIT;
        if bytes.len() >= tail_limit {
            self.tail.clear();
            self.tail
                .extend(bytes[bytes.len() - tail_limit..].iter().copied());
            return;
        }

        let overflow = self
            .tail
            .len()
            .saturating_add(bytes.len())
            .saturating_sub(tail_limit);
        self.tail.drain(..overflow);
        self.tail.extend(bytes.iter().copied());
    }

    fn render(&self) -> RenderedStream {
        if self.total_bytes <= CAPTURE_STREAM_LIMIT {
            let mut bytes = Vec::with_capacity(self.total_bytes);
            bytes.extend_from_slice(&self.head);
            bytes.extend(self.tail.iter().copied());
            return RenderedStream {
                bytes,
                dropped_bytes: 0,
            };
        }

        let mut dropped_bytes = self.total_bytes - CAPTURE_STREAM_LIMIT;
        let (marker, data_limit) = loop {
            let marker = truncation_marker(dropped_bytes);
            let data_limit = CAPTURE_STREAM_LIMIT - marker.len();
            let adjusted_dropped_bytes = self.total_bytes - data_limit;
            if adjusted_dropped_bytes == dropped_bytes {
                break (marker, data_limit);
            }
            dropped_bytes = adjusted_dropped_bytes;
        };
        let head_count = data_limit / 2;
        let tail_count = data_limit - head_count;
        let mut bytes = Vec::with_capacity(CAPTURE_STREAM_LIMIT);
        bytes.extend_from_slice(&self.head[..head_count]);
        bytes.extend_from_slice(marker.as_bytes());
        bytes.extend(self.tail.iter().skip(self.tail.len() - tail_count).copied());
        debug_assert_eq!(bytes.len(), CAPTURE_STREAM_LIMIT);

        RenderedStream {
            bytes,
            dropped_bytes,
        }
    }
}

struct RenderedStream {
    bytes: Vec<u8>,
    dropped_bytes: usize,
}

fn truncation_marker(dropped_bytes: usize) -> String {
    format!("\n...[blehub capture truncated: {dropped_bytes} bytes dropped]...\n")
}

struct BoundedOutputCapture {
    receiver: mpsc::Receiver<CaptureEvent>,
    stdout: Arc<Mutex<BoundedStreamCapture>>,
    stderr: Arc<Mutex<BoundedStreamCapture>>,
    stdout_closed: bool,
    stderr_closed: bool,
    failures: Vec<String>,
}

impl BoundedOutputCapture {
    fn new(stdout: ChildStdout, stderr: ChildStderr) -> Self {
        let (sender, receiver) = mpsc::sync_channel(2);
        let stdout_capture = Arc::new(Mutex::new(BoundedStreamCapture::default()));
        let stderr_capture = Arc::new(Mutex::new(BoundedStreamCapture::default()));
        capture_stream(
            OutputStream::Stdout,
            stdout,
            Arc::clone(&stdout_capture),
            sender.clone(),
        );
        capture_stream(
            OutputStream::Stderr,
            stderr,
            Arc::clone(&stderr_capture),
            sender,
        );
        Self {
            receiver,
            stdout: stdout_capture,
            stderr: stderr_capture,
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

        let stdout = self
            .stdout
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .render();
        let stderr = self
            .stderr
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .render();
        if stdout.dropped_bytes != 0 {
            self.failures.push(format!(
                "stdout capture truncated: {} bytes dropped; captured output contains a truncation marker",
                stdout.dropped_bytes
            ));
        }
        if stderr.dropped_bytes != 0 {
            self.failures.push(format!(
                "stderr capture truncated: {} bytes dropped; captured output contains a truncation marker",
                stderr.dropped_bytes
            ));
        }

        CapturedOutput {
            stdout: stdout.bytes,
            stderr: stderr.bytes,
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
    capture: Arc<Mutex<BoundedStreamCapture>>,
    sender: mpsc::SyncSender<CaptureEvent>,
) {
    thread::spawn(move || {
        let mut buffer = [0_u8; 8 * 1024];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => {
                    let _ = sender.send(CaptureEvent::Closed(stream));
                    return;
                }
                Ok(count) => capture
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .extend(&buffer[..count]),
                Err(error) => {
                    let _ = sender.send(CaptureEvent::Failed(stream, error.to_string()));
                    return;
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_at_limit_is_complete_and_unmarked() {
        let input = patterned_bytes(CAPTURE_STREAM_LIMIT);
        let mut capture = BoundedStreamCapture::default();
        capture.extend(&input[..17]);
        capture.extend(&input[17..]);

        let rendered = capture.render();

        assert_eq!(rendered.bytes, input);
        assert_eq!(rendered.dropped_bytes, 0);
        assert!(!contains_bytes(
            &rendered.bytes,
            b"blehub capture truncated"
        ));
    }

    #[test]
    fn capture_over_limit_keeps_head_and_tail_with_counted_marker() {
        let input = patterned_bytes(CAPTURE_STREAM_LIMIT + 257);
        let mut capture = BoundedStreamCapture::default();
        for chunk in input.chunks(997) {
            capture.extend(chunk);
        }

        let rendered = capture.render();
        let marker = truncation_marker(rendered.dropped_bytes);
        let marker_start = rendered
            .bytes
            .windows(marker.len())
            .position(|window| window == marker.as_bytes())
            .expect("truncation marker must be present");
        let tail_start = marker_start + marker.len();

        assert_eq!(rendered.bytes.len(), CAPTURE_STREAM_LIMIT);
        assert!(rendered.dropped_bytes > 257);
        assert_eq!(&rendered.bytes[..marker_start], &input[..marker_start]);
        assert_eq!(
            &rendered.bytes[tail_start..],
            &input[input.len() - (rendered.bytes.len() - tail_start)..]
        );
        assert_eq!(
            rendered.dropped_bytes,
            input.len() - marker_start - (rendered.bytes.len() - tail_start)
        );
        assert!(marker.contains("truncated"));
        assert!(marker.contains("dropped"));
    }

    #[test]
    fn first_byte_over_limit_is_explicitly_truncated() {
        let input = patterned_bytes(CAPTURE_STREAM_LIMIT + 1);
        let mut capture = BoundedStreamCapture::default();
        capture.extend(&input);

        let rendered = capture.render();

        assert_eq!(rendered.bytes.len(), CAPTURE_STREAM_LIMIT);
        assert!(rendered.dropped_bytes > 1);
        assert!(contains_bytes(
            &rendered.bytes,
            b"...[blehub capture truncated: "
        ));
    }

    fn patterned_bytes(len: usize) -> Vec<u8> {
        (0..len).map(|index| (index % 251) as u8).collect()
    }

    fn contains_bytes(haystack: &[u8], needle: &[u8]) -> bool {
        haystack
            .windows(needle.len())
            .any(|window| window == needle)
    }
}
