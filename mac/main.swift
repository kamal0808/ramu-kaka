// Watch Mic: receives the Ramu Kaka watch app's audio (16 kHz mono PCM16 over TCP :5577),
// plays it into BlackHole 2ch (VoiceInk's input), and holds VoiceInk's shortcut while you talk.
import AVFoundation
import CoreAudio
import CoreGraphics
import Foundation
import Network

let shortcutKeyCode: CGKeyCode = 61   // right Option, VoiceInk's primary shortcut
// Let VoiceInk start recording before audio flows. Tune without rebuilding: defaults write com.topc.watch-mic preRoll 1.5
let preRoll = UserDefaults.standard.object(forKey: "preRoll") as? Double ?? 1.5

func log(_ s: String) { FileHandle.standardError.write("\(ISO8601DateFormatter().string(from: Date())) \(s)\n".data(using: .utf8)!) }

func setShortcut(down: Bool) {
    let e = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: shortcutKeyCode, keyDown: down)!
    e.type = .flagsChanged
    e.flags = down ? .maskAlternate : []
    e.post(tap: .cghidEventTap)
}

func blackHoleID() -> AudioDeviceID? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    for id in ids {
        var nameAddr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var name: CFString = "" as CFString
        var nsize = UInt32(MemoryLayout<CFString>.size)
        AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &nsize, &name)
        if (name as String).contains("BlackHole") { return id }
    }
    return nil
}

final class Session {
    let engine = AVAudioEngine()
    let player = AVAudioPlayerNode()
    let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
    var pending = 0
    var ended = false
    var leftover = Data()

    init(device: AudioDeviceID) throws {
        try engine.outputNode.auAudioUnit.setDeviceID(device)
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        engine.prepare()
        try engine.start()
        // VoiceInk opening BlackHole as its input can reconfigure the device and stop this engine; restart it.
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            guard let self, !self.released else { return }
            log("audio config changed, restarting")
            try? self.engine.start()
            self.player.play()
        }
        log("engine running: \(engine.isRunning), output: \(engine.outputNode.outputFormat(forBus: 0))")
    }

    func feed(_ data: Data) {
        leftover.append(data)
        let frames = leftover.count / 2
        guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else { return }
        buf.frameLength = AVAudioFrameCount(frames)
        leftover.withUnsafeBytes { raw in
            let src = raw.bindMemory(to: Int16.self)
            for i in 0..<frames { buf.floatChannelData![0][i] = Float(Int16(littleEndian: src[i])) / 32768 }
        }
        leftover = leftover.count % 2 == 1 ? leftover.suffix(1) : Data()
        pending += 1
        player.scheduleBuffer(buf) { DispatchQueue.main.async { self.pending -= 1; self.finishIfDone() } }
    }

    var released = false
    func finishIfDone() {
        guard ended, pending == 0, !released else { return }
        released = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            setShortcut(down: false)
            self.engine.stop()
            log("released shortcut")
        }
    }
}

var current: Session?

func handle(_ conn: NWConnection) {
    guard let dev = blackHoleID() else { log("BlackHole not found"); conn.cancel(); return }
    guard let s = try? Session(device: dev) else { log("audio engine failed"); conn.cancel(); return }
    current = s
    log("watch connected, holding shortcut")
    setShortcut(down: true)
    DispatchQueue.main.asyncAfter(deadline: .now() + preRoll) {
        if !s.engine.isRunning { try? s.engine.start() }
        s.player.play()
        log("playing: \(s.player.isPlaying), engine running: \(s.engine.isRunning)")
    }
    func receive() {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 8192) { data, _, done, err in
            DispatchQueue.main.async {
                if let data, !data.isEmpty { s.feed(data) }
                if done || err != nil {
                    log("watch stopped")
                    conn.cancel()
                    s.ended = true
                    log("pending buffers: \(s.pending)")
                    s.finishIfDone()
                    // never leave VoiceInk's key held, even if playback stalls
                    DispatchQueue.main.asyncAfter(deadline: .now() + 5) { s.pending = 0; s.finishIfDone() }
                } else { receive() }
            }
        }
    }
    conn.start(queue: .main)
    receive()
}

let listener = try! NWListener(using: .tcp, on: 5577)
listener.newConnectionHandler = handle
listener.start(queue: .main)
let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
if !AXIsProcessTrustedWithOptions(opts) { log("needs Accessibility permission to press VoiceInk's shortcut") }
log("Watch Mic listening on :5577, BlackHole: \(blackHoleID() != nil)")
RunLoop.main.run()
