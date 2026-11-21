// Ramu Listen: say "Ramu Kaka" and it taps VoiceInk's record shortcut for you,
// then taps it again when you go quiet. Uses Apple's on-device speech, so it costs little CPU.
import AVFoundation
import Speech
import CoreGraphics
import Foundation

let shortcutKeyCode: CGKeyCode = 61          // right Option, VoiceInk's primary shortcut
let silenceToStop: TimeInterval = 2.5
let wake = try! NSRegularExpression(
    pattern: #"\b(ra+m+[ouaeh]*|rom+[ou]*|rah?m+o+)\s*,?\s*(k+a+h?k+h?a+|c+a+c+a+|car\s*car|kak+e)\b"#,
    options: [.caseInsensitive])

func log(_ s: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(s)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
}

// Hold the key like push-to-talk: VoiceInk ignored a second tap as "stop", but a release always stops it.
func setShortcut(down: Bool) {
    let e = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: shortcutKeyCode, keyDown: down)!
    e.type = .flagsChanged
    e.flags = down ? .maskAlternate : []
    e.post(tap: .cghidEventTap)
}

final class Listener {
    let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-IN"))!
    let engine = AVAudioEngine()
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    var recording = false
    var startedAt = Date()
    var lastChange = Date()
    var lastText = ""
    var taskStarted = Date()

    func start() {
        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { [weak self] buf, _ in
            self?.request?.append(buf)
        }
        try! engine.start()
        newTask()
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.tick() }
        log("listening for \"Ramu Kaka\" (on-device: \(recognizer.supportsOnDeviceRecognition))")
    }

    func newTask() {
        task?.cancel()
        request?.endAudio()
        let r = SFSpeechAudioBufferRecognitionRequest()
        r.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { r.requiresOnDeviceRecognition = true }
        r.contextualStrings = ["Ramu Kaka"]
        request = r
        lastText = ""
        taskStarted = Date()
        task = recognizer.recognitionTask(with: r) { [weak self] result, _ in
            guard let self, let text = result?.bestTranscription.formattedString else { return }
            DispatchQueue.main.async { self.heard(text) }
        }
    }

    func heard(_ text: String) {
        if text != lastText { lastText = text; lastChange = Date() }
        guard !recording else { return }
        let tail = text.split(separator: " ").suffix(6).joined(separator: " ")
        if wake.firstMatch(in: tail, range: NSRange(tail.startIndex..., in: tail)) != nil {
            log("wake: \(tail)")
            // VoiceInk records silence if we hold the mic when it starts, so let go first.
            engine.stop()
            setShortcut(down: true)
            recording = true
            startedAt = Date()
            lastChange = Date()
            newTask()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { try? self.engine.start() }
        }
    }

    func tick() {
        let now = Date()
        if recording, now.timeIntervalSince(startedAt) > 2.5, now.timeIntervalSince(lastChange) > silenceToStop {
            log("quiet, stopping")
            setShortcut(down: false)
            recording = false
            newTask()
        } else if !recording, now.timeIntervalSince(taskStarted) > 50 {
            newTask()   // Apple ends a recognition task after about a minute
        }
    }
}

SFSpeechRecognizer.requestAuthorization { status in
    guard status == .authorized else { log("speech recognition not allowed: \(status.rawValue)"); exit(1) }
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        guard ok else { log("microphone not allowed"); exit(1) }
        DispatchQueue.main.async {
            let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            if !AXIsProcessTrustedWithOptions(opts) { log("needs Accessibility permission to press the shortcut") }
            listener.start()
        }
    }
}
let listener = Listener()
RunLoop.main.run()
