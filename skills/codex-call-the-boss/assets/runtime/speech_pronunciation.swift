import Foundation

// Preserve tones. This normalizes ASR homophones, not speech or task contents.
let data = FileHandle.standardInput.readDataToEndOfFile()
let strings = try JSONDecoder().decode([String].self, from: data)
if CommandLine.arguments.dropFirst().contains("--simplified") {
    // Orthography only; caller words and generated audio are never rewritten.
    let output = try strings.map { text -> String in
        guard let normalized = text.applyingTransform(
                StringTransform("Traditional-Simplified"), reverse: false) else {
            throw NSError(domain: "PhoneSpeechQA", code: 1)
        }
        return normalized
    }
    FileHandle.standardOutput.write(try JSONEncoder().encode(output))
} else {
    let output = strings.map { text -> [String] in
        let latin = text.applyingTransform(.mandarinToLatin, reverse: false) ?? text
        return latin.lowercased().components(separatedBy:
            CharacterSet.letters.union(.decimalDigits).inverted).filter { !$0.isEmpty }
    }
    FileHandle.standardOutput.write(try JSONEncoder().encode(output))
}
