import AppKit
import Foundation

// The pipe is the ownership lease: even SIGKILL of the Node parent produces EOF.
// Keep all NSWorkspace/NSRunningApplication access on the main run loop.
final class ApplicationOwner {
    private var application: NSRunningApplication?
    private var launchPending = true
    private var stopping = false
    private var failed = false
    private var finished = false
    private var signals: [DispatchSourceSignal] = []
    private var monitor: Timer?

    private func emit(_ event: String, _ fields: [String: Any] = [:]) {
        var message = fields
        message["event"] = event
        if let data = try? JSONSerialization.data(withJSONObject: message, options: [.sortedKeys]) {
            // A dead parent also closes stdout. Ignore EPIPE so EOF cleanup can
            // finish instead of Foundation raising on a broken output pipe.
            let line = data + Data([10])
            line.withUnsafeBytes { bytes in
                _ = Darwin.write(STDOUT_FILENO, bytes.baseAddress!, bytes.count)
            }
        }
    }

    private func fail(_ message: String) {
        failed = true
        emit("error", ["message": message])
    }

    private func finish() {
        guard !finished else { return }
        finished = true
        monitor?.invalidate()
        exit(failed ? 1 : 0)
    }

    func stop() {
        guard !stopping else { return }
        stopping = true
        // NSWorkspace cannot cancel a pending launch. Its completion must hand
        // us the actual instance before we can clean it, including a late launch.
        guard !launchPending else { return }
        guard let app = application else { finish(); return }
        if app.isTerminated { finish(); return }
        _ = app.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [self] in
            guard !finished, !app.isTerminated else { return }
            _ = app.forceTerminate()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [self] in
            guard !finished else { return }
            if !app.isTerminated {
                fail("Application PID \(app.processIdentifier) did not terminate after forceTerminate")
            }
            finish()
        }
    }

    func start(bundle: URL, port: String, arguments: [String]) {
        signal(SIGPIPE, SIG_IGN)
        for number in [SIGTERM, SIGINT, SIGHUP] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [self] in stop() }
            source.resume()
            signals.append(source)
        }
        DispatchQueue.global().async { [self] in
            // The only command is "stop\n". Any input, or EOF, releases ownership.
            _ = FileHandle.standardInput.readData(ofLength: 1)
            DispatchQueue.main.async { [self] in stop() }
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.createsNewApplicationInstance = true
        configuration.activates = true
        configuration.arguments = arguments
        configuration.environment = ProcessInfo.processInfo.environment.merging([
            "TAURI_WEBDRIVER_PORT": port,
            "WDIO_EMBEDDED_SERVER": "true",
        ]) { _, supplied in supplied }

        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [self] in
            guard launchPending else { return }
            fail("NSWorkspace launch exceeded 30 seconds; awaiting completion to clean any late instance")
            stop()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [self] in
            guard launchPending else { return }
            fail("NSWorkspace never completed; application identity/cleanup cannot be confirmed")
            finish()
        }
        NSWorkspace.shared.openApplication(at: bundle, configuration: configuration) { [self] app, error in
            DispatchQueue.main.async { [self] in
                launchPending = false
                application = app
                if let app {
                    emit("launched", ["pid": app.processIdentifier])
                    monitor = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [self] _ in
                        guard app.isTerminated else { return }
                        emit("terminated", ["pid": app.processIdentifier])
                        if !stopping { fail("Application exited before ownership was released") }
                        finish()
                    }
                }
                if let error { fail("NSWorkspace launch failed: \(error.localizedDescription)") }
                if app == nil, error == nil { fail("NSWorkspace returned no running application") }
                if stopping || failed {
                    stopping = false
                    stop()
                }
            }
        }
    }
}

let args = CommandLine.arguments
if args.count < 3 || UInt16(args[2]) == nil || args[2] == "0" {
    FileHandle.standardError.write(Data("usage: launch-app APP_BUNDLE PORT [APP_ARGS...]\n".utf8))
    exit(2)
}
let owner = ApplicationOwner()
owner.start(bundle: URL(fileURLWithPath: args[1], isDirectory: true), port: args[2], arguments: Array(args.dropFirst(3)))
RunLoop.main.run()
