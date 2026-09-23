import AVFoundation
import Foundation


func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

struct SpeechRequest: Decodable {
    let id: String
    let output: String
    let text: String
    let voice: String
    let rate: Float
    let pitch: Float
}

struct SpeechResponse: Encodable {
    let id: String
    let ok: Bool
    let error: String?
}

func renderSpeech(
    outputPath: String,
    text: String,
    voiceIdentifier: String,
    rate: Float,
    pitch: Float
) -> String? {
    guard !text.isEmpty else { return "empty speech text" }
    guard let voice = AVSpeechSynthesisVoice(identifier: voiceIdentifier) else {
        return "voice is not installed: \(voiceIdentifier)"
    }

    let outputURL = URL(fileURLWithPath: outputPath)
    let synthesizer = AVSpeechSynthesizer()
    let utterance = AVSpeechUtterance(string: text)
    utterance.voice = voice
    utterance.rate = max(AVSpeechUtteranceMinimumSpeechRate,
                         min(AVSpeechUtteranceMaximumSpeechRate, rate))
    utterance.pitchMultiplier = max(0.7, min(1.4, pitch))
    utterance.volume = 1.0

    let stateLock = NSLock()
    var outputFile: AVAudioFile?
    var failure: String?
    var didFinish = false

    synthesizer.write(utterance) { buffer in
        stateLock.lock()
        defer { stateLock.unlock() }
        guard !didFinish else { return }
        guard let pcm = buffer as? AVAudioPCMBuffer else {
            failure = "AVSpeechSynthesizer returned a non-PCM buffer"
            didFinish = true
            return
        }
        if pcm.frameLength == 0 {
            didFinish = true
            return
        }
        do {
            if outputFile == nil {
                outputFile = try AVAudioFile(
                    forWriting: outputURL,
                    settings: pcm.format.settings
                )
            }
            try outputFile?.write(from: pcm)
        } catch {
            failure = "failed to write synthesized audio: \(error)"
            didFinish = true
        }
    }

    let deadline = Date(timeIntervalSinceNow: 45)
    while Date() < deadline {
        stateLock.lock()
        let complete = didFinish
        stateLock.unlock()
        if complete { break }
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.02))
    }
    stateLock.lock()
    let completed = didFinish
    let finalFailure = failure
    stateLock.unlock()
    if !completed { return "AVSpeechSynthesizer timed out" }
    return finalFailure
}

func writeResponse(_ response: SpeechResponse) {
    let encoder = JSONEncoder()
    guard let payload = try? encoder.encode(response) else { return }
    FileHandle.standardOutput.write(payload + Data("\n".utf8))
}

if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--list" {
    for voice in AVSpeechSynthesisVoice.speechVoices()
        .filter({ $0.language == "zh-CN" }) {
        print("\(voice.name)|\(voice.identifier)|\(voice.quality.rawValue)")
    }
    exit(0)
}

if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--server" {
    let decoder = JSONDecoder()
    while let line = readLine() {
        guard let payload = line.data(using: .utf8),
              let request = try? decoder.decode(SpeechRequest.self, from: payload) else {
            writeResponse(SpeechResponse(id: "", ok: false, error: "invalid request"))
            continue
        }
        let error = renderSpeech(
            outputPath: request.output,
            text: request.text,
            voiceIdentifier: request.voice,
            rate: request.rate,
            pitch: request.pitch
        )
        writeResponse(SpeechResponse(id: request.id, ok: error == nil, error: error))
    }
    exit(0)
}

guard CommandLine.arguments.count == 5 else {
    fail("usage: mac_tts_helper OUTPUT_CAF VOICE_IDENTIFIER RATE PITCH")
}

let voiceIdentifier = CommandLine.arguments[2]
guard let rate = Float(CommandLine.arguments[3]),
      let pitch = Float(CommandLine.arguments[4]) else {
    fail("invalid rate or pitch")
}
let input = FileHandle.standardInput.readDataToEndOfFile()
guard let text = String(data: input, encoding: .utf8), !text.isEmpty else {
    fail("empty speech text")
}
if let error = renderSpeech(
    outputPath: CommandLine.arguments[1],
    text: text,
    voiceIdentifier: voiceIdentifier,
    rate: rate,
    pitch: pitch
) {
    fail(error)
}
