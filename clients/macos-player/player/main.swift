// calliope-player — reads a text file aloud through Kokoro, in a floating Liquid Glass capsule
// that can grow into a reader: the selection as it was selected, with an underline sweeping
// across each word as it is spoken.
//
// Build and install: ../install.sh
// Usage: calliope-player <text-file>   (the file is deleted once read)
//
// Speaks the same contract as services/tts: POST /v1/audio/speech with response_format "pcm",
// to the local server (server/server.py), which it starts on demand.
import AppKit
import AVFoundation
import NaturalLanguage

let runtimeURL = URL(fileURLWithPath: NSString(string: "~/.local/share/calliope").expandingTildeInPath)
let settings = playerDefaults()
// THE PLAYER NEVER TALKS TO THE CALLIOPE STACK, AND THE SERVER IS NOT CONFIGURABLE. It reads
// text you selected on this Mac. A remote host would mean a URL to keep right, a key to hold,
// a network that can be down and a second place for a bug to live, and it buys nothing:
// Kokoro's full model on this M2's own CPU measures about 4.9× realtime, so synthesis is never
// what you wait for — first sound is about 0.3 s with the server warm. Anything that puts a
// server URL back in UserDefaults brings all four costs back for that same nothing.
let localServerURL = "http://127.0.0.1:47815"
let audioFormat = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!
let speedSteps: [Float] = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
let appleEase = CAMediaTimingFunction(controlPoints: 0.32, 0.72, 0, 1)

struct Voice {
    let name: String
    let badge: String
}

// Detected language -> Kokoro voice; Kokoro derives the language from the voice's first letter.
// All voices: https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md
let voices: [NLLanguage: Voice] = [
    .english: Voice(name: "af_heart", badge: "EN"),
    .portuguese: Voice(name: "pf_dora", badge: "PT"),
    .spanish: Voice(name: "ef_dora", badge: "ES"),
    .french: Voice(name: "ff_siwis", badge: "FR"),
    .italian: Voice(name: "if_sara", badge: "IT"),
    .hindi: Voice(name: "hf_alpha", badge: "HI"),
    .japanese: Voice(name: "jf_alpha", badge: "JA"),
    .simplifiedChinese: Voice(name: "zf_xiaobei", badge: "ZH"),
]

var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

// MARK: - Text

/// One synthesis request. Its words keep their place in the full text, so the reader can
/// underline them where they sit — paragraphs, line breaks and all.
struct Chunk {
    let text: String
    /// Whitespace-separated, exactly as the server splits `input` for X-Word-Timings.
    let words: [String]
    /// UTF-16 ranges of `words` in the full text.
    let wordRanges: [NSRange]
}

struct WordTiming {
    let start: Double
    let end: Double
}

func detectLanguage(_ text: String) -> NLLanguage {
    let recognizer = NLLanguageRecognizer()
    recognizer.languageConstraints = Array(voices.keys)
    recognizer.processString(text)
    guard let best = recognizer.languageHypotheses(withMaximum: 1).first, best.value >= 0.5 else {
        return .english
    }
    return best.key
}

/// Sentences, with long ones broken at clause punctuation: the first audio arrives sooner and
/// no request carries a whole paragraph.
func splitIntoChunks(_ text: String, language: NLLanguage) -> [Chunk] {
    let full = text as NSString
    let tokenizer = NLTokenizer(unit: .sentence)
    tokenizer.string = text
    tokenizer.setLanguage(language)
    var chunks: [Chunk] = []
    tokenizer.enumerateTokens(in: text.startIndex..<text.endIndex) { range, _ in
        chunks.append(contentsOf: groupWords(wordRanges(in: full, range: NSRange(range, in: text)), of: full))
        return true
    }
    return chunks
}

func wordRanges(in text: NSString, range: NSRange) -> [NSRange] {
    var ranges: [NSRange] = []
    var start: Int?
    for offset in range.location..<NSMaxRange(range) {
        let isSpace = Unicode.Scalar(text.character(at: offset)).map { CharacterSet.whitespacesAndNewlines.contains($0) } ?? false
        if isSpace {
            if let wordStart = start { ranges.append(NSRange(location: wordStart, length: offset - wordStart)) }
            start = nil
        } else if start == nil {
            start = offset
        }
    }
    if let wordStart = start { ranges.append(NSRange(location: wordStart, length: NSMaxRange(range) - wordStart)) }
    return ranges
}

func groupWords(_ ranges: [NSRange], of text: NSString, limit: Int = 140) -> [Chunk] {
    var chunks: [Chunk] = []
    var current: [NSRange] = []
    func flush() {
        guard let first = current.first, let last = current.last else { return }
        let words = current.map { text.substring(with: $0) }
        if words.contains(where: { $0.rangeOfCharacter(from: .alphanumerics) != nil }) {
            let span = NSRange(location: first.location, length: NSMaxRange(last) - first.location)
            chunks.append(Chunk(text: text.substring(with: span), words: words, wordRanges: current))
        }
        current = []
    }
    for range in ranges {
        current.append(range)
        let length = NSMaxRange(range) - current[0].location
        let atClauseEnd = text.substring(with: range).last.map { ",;:".contains($0) } ?? false
        if (atClauseEnd && length >= limit / 2) || length >= limit { flush() }
    }
    flush()
    return chunks
}

/// The fallback for a reply whose X-Word-Timings the player cannot use — absent, or with a pair
/// count that does not match the words, which is what parseTimings returning nil means. The
/// underline has to land somewhere regardless, so spread the words over the audio by length with
/// a little extra time after punctuation. The local server always sends the header, so this is
/// the safety net and not the path.
func estimateTimings(_ words: [String], duration: Double) -> [WordTiming] {
    let weights = words.map { word -> Double in
        let pause = word.last.map { ".,;:!?".contains($0) } ?? false
        return Double(word.count) + (pause ? 4 : 1)
    }
    let total = max(weights.reduce(0, +), 1)
    var cursor = 0.0
    return weights.map { weight in
        let start = cursor / total * duration
        cursor += weight
        return WordTiming(start: start, end: cursor / total * duration)
    }
}

func parseTimings(_ header: String?, count: Int) -> [WordTiming]? {
    guard let data = header?.data(using: .utf8),
          let pairs = try? JSONSerialization.jsonObject(with: data) as? [[Double]],
          pairs.count == count, pairs.allSatisfy({ $0.count == 2 }) else { return nil }
    return pairs.map { WordTiming(start: $0[0], end: $0[1]) }
}

func formatSpeed(_ speed: Float) -> String {
    var text = String(format: "%.2f", speed)
    while text.hasSuffix("0") { text.removeLast() }
    if text.hasSuffix(".") { text.removeLast() }
    return text + "×"
}

func symbolImage(_ name: String, size: CGFloat) -> NSImage? {
    NSImage(systemSymbolName: name, accessibilityDescription: nil)?
        .withSymbolConfiguration(.init(pointSize: size, weight: .semibold))
}

// MARK: - Server

func checkHealth(timeout: TimeInterval, completion: @escaping (Bool) -> Void) {
    var request = URLRequest(url: URL(string: localServerURL + "/health")!)
    request.timeoutInterval = timeout
    URLSession.shared.dataTask(with: request) { _, response, _ in
        let ok = (response as? HTTPURLResponse)?.statusCode == 200
        DispatchQueue.main.async { completion(ok) }
    }.resume()
}

func launchLocalServer() {
    let logURL = runtimeURL.appendingPathComponent("server.log")
    FileManager.default.createFile(atPath: logURL.path, contents: nil)
    let process = Process()
    process.executableURL = runtimeURL.appendingPathComponent(".venv/bin/python")
    process.arguments = [runtimeURL.appendingPathComponent("server.py").path]
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = try? FileHandle(forWritingTo: logURL)
    process.standardError = process.standardOutput
    try? process.run()
}

struct Speech {
    let buffer: AVAudioPCMBuffer
    let timings: [WordTiming]
}

/// One chunk -> 24 kHz mono float buffer plus word timings, via the OpenAI-shaped speech route.
func synthesise(_ chunk: Chunk, voice: Voice, completion: @escaping (Speech?) -> Void) {
    var request = URLRequest(url: URL(string: localServerURL + "/v1/audio/speech")!)
    request.httpMethod = "POST"
    request.timeoutInterval = 120
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: [
        "model": "kokoro", "input": chunk.text, "voice": voice.name, "response_format": "pcm",
    ])
    URLSession.shared.dataTask(with: request) { data, response, _ in
        var speech: Speech?
        let http = response as? HTTPURLResponse
        if let data, http?.statusCode == 200 {
            let frames = data.count / MemoryLayout<Int16>.size
            if frames > 0, let pcm = AVAudioPCMBuffer(pcmFormat: audioFormat, frameCapacity: AVAudioFrameCount(frames)) {
                pcm.frameLength = AVAudioFrameCount(frames)
                let samples = pcm.floatChannelData![0]
                data.withUnsafeBytes { raw in
                    for frame in 0..<frames {
                        let value = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: frame * 2, as: Int16.self))
                        samples[frame] = Float(value) / 32_768
                    }
                }
                let duration = Double(frames) / audioFormat.sampleRate
                let timings = parseTimings(http?.value(forHTTPHeaderField: "X-Word-Timings"), count: chunk.words.count)
                    ?? estimateTimings(chunk.words, duration: duration)
                speech = Speech(buffer: pcm, timings: timings)
            }
        }
        DispatchQueue.main.async { completion(speech) }
    }.resume()
}

// MARK: - Playback

struct ReadingPosition {
    let chunk: Int
    /// Index of the word being spoken in `chunk`, or -1 before its first word.
    let word: Int
    /// How long that word takes at the current speed, for the underline sweep.
    let wordDuration: Double
}

final class Player {
    let text: String
    let chunks: [Chunk]
    let voice: Voice
    private(set) var isPaused = false
    private(set) var speed: Float
    weak var panel: ControlPanel?

    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private let timePitch = AVAudioUnitTimePitch()
    private var speeches: [Int: Speech] = [:]
    private var failed = Set<Int>()
    private var index = 0
    private var generation = 0  // bumped on every (re)schedule so callbacks of cut-off buffers are ignored
    private var fetchInFlight = false
    private var waitingForAudio = true
    private var serverReady = false
    private let prefetchAhead = 3
    private var bufferStartSample: AVAudioFramePosition = 0  // node timeline position where the current chunk began
    private var spokenWord = -1

    init(text: String, chunks: [Chunk], voice: Voice) {
        self.text = text
        self.chunks = chunks
        self.voice = voice
        let saved = settings.float(forKey: "speed")
        speed = speedSteps.contains(saved) ? saved : 1.0
        engine.attach(node)
        engine.attach(timePitch)
        engine.connect(node, to: timePitch, format: audioFormat)
        engine.connect(timePitch, to: engine.mainMixerNode, format: audioFormat)
        timePitch.rate = speed
        // Headphones plugged or unplugged: the engine stops; restart the current sentence.
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            guard let self else { return }
            try? self.engine.start()
            self.jump(to: self.index)
        }
    }

    func start() {
        do {
            try engine.start()
        } catch {
            return fail("Audio output failed: \(error.localizedDescription)")
        }
        refreshPanel()
        checkHealth(timeout: 0.5) { [self] ok in
            if ok { return becomeReady() }
            launchLocalServer()
            waitForServer(until: Date().addingTimeInterval(45))
        }
    }

    func togglePause() {
        isPaused.toggle()
        if isPaused { node.pause() } else { node.play() }
        refreshPanel()
    }

    func skip(by offset: Int) {
        jump(to: index + offset)
    }

    func play(chunk: Int) {
        jump(to: chunk)
    }

    func changeSpeed(by step: Int) {
        let current = speedSteps.firstIndex(of: speed) ?? 1
        speed = speedSteps[max(0, min(speedSteps.count - 1, current + step))]
        timePitch.rate = speed
        settings.set(speed, forKey: "speed")
        refreshPanel()
    }

    /// The node's sample time counts source frames, so the position stays right at any speed.
    func readingPosition() -> ReadingPosition {
        let shown = min(index, chunks.count - 1)
        if !waitingForAudio, shown == index, let speech = speeches[index], let renderTime = node.lastRenderTime,
           let playerTime = node.playerTime(forNodeTime: renderTime) {
            let seconds = Double(playerTime.sampleTime - bufferStartSample) / audioFormat.sampleRate
            spokenWord = speech.timings.lastIndex(where: { $0.start <= seconds }) ?? -1
        }
        var duration = 0.3
        if spokenWord >= 0, let timings = speeches[shown]?.timings, spokenWord < timings.count {
            duration = max(0.08, (timings[spokenWord].end - timings[spokenWord].start) / Double(speed))
        }
        return ReadingPosition(chunk: shown, word: spokenWord, wordDuration: duration)
    }

    private func waitForServer(until deadline: Date) {
        checkHealth(timeout: 2) { [self] ok in
            if ok { return becomeReady() }
            guard Date() < deadline else {
                return fail("Kokoro server did not start — see ~/.local/share/calliope/server.log")
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForServer(until: deadline) }
        }
    }

    private func becomeReady() {
        serverReady = true
        playCurrent()
    }

    private func jump(to newIndex: Int) {
        guard serverReady else { return }
        generation += 1
        node.stop()
        index = max(0, min(newIndex, chunks.count - 1))
        playCurrent()
    }

    private func playCurrent() {
        guard index < chunks.count else { return finish() }
        if failed.contains(index) {
            index += 1
            return playCurrent()
        }
        spokenWord = -1
        guard let speech = speeches[index] else {
            waitingForAudio = true
            refreshPanel()
            return fetchIfNeeded()
        }
        waitingForAudio = false
        generation += 1
        let token = generation
        // A stopped node has no player time and restarts its timeline at zero on play().
        if let renderTime = node.lastRenderTime, let playerTime = node.playerTime(forNodeTime: renderTime) {
            bufferStartSample = playerTime.sampleTime
        } else {
            bufferStartSample = 0
        }
        node.scheduleBuffer(speech.buffer, at: nil, options: [], completionCallbackType: .dataPlayedBack) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, token == self.generation else { return }
                self.index += 1
                self.playCurrent()
            }
        }
        if !isPaused { node.play() }
        refreshPanel()
        fetchIfNeeded()
    }

    /// Fetches the first missing chunk in [index, index + prefetchAhead], one request at a time.
    private func fetchIfNeeded() {
        guard !fetchInFlight else { return }
        let window = index..<min(chunks.count, index + prefetchAhead + 1)
        guard let target = window.first(where: { speeches[$0] == nil && !failed.contains($0) }) else { return }
        fetchInFlight = true
        synthesise(chunks[target], voice: voice) { [self] speech in
            fetchInFlight = false
            if let speech { speeches[target] = speech } else { failed.insert(target) }
            if waitingForAudio && target == index { playCurrent() } else { fetchIfNeeded() }
        }
    }

    private func refreshPanel() {
        let position = "\(min(index + 1, chunks.count))/\(chunks.count)"
        let sentence = index < chunks.count ? chunks[index].text : ""
        if !serverReady {
            panel?.update(status: "starting", detail: "Starting Kokoro…", busy: true)
        } else {
            panel?.update(status: position, detail: sentence, busy: waitingForAudio)
        }
    }

    private func finish() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { NSApp.terminate(nil) }
    }

    private func fail(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8))
        panel?.update(status: "error", detail: message, busy: false)
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { NSApp.terminate(nil) }
    }
}

// MARK: - Reader

final class ReaderTextView: NSTextView {
    var onClick: ((Int) -> Void)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        onClick?(characterIndexForInsertion(at: convert(event.locationInWindow, from: nil)))
    }
}

/// The selection exactly as it was selected — paragraphs and line breaks intact — at one size.
/// Words already read are bright and the rest dimmed; an underline sweeps across the word being
/// spoken; the current line is kept in the middle; clicking a sentence jumps to it.
final class ReaderView: NSView {
    private static let font = NSFont.systemFont(ofSize: 17, weight: .medium)

    var onJump: ((Int) -> Void)?
    private let chunks: [Chunk]
    private let scrollView = NSScrollView()
    private let textView = ReaderTextView(usingTextLayoutManager: false)
    private let underline = NSView()
    private let fade = CAGradientLayer()
    private var brightUpTo = 0  // UTF-16 offset: text before it is painted as read
    private var shownChunk = -1
    private var shownWord = Int.min
    private var followedLine: CGFloat = -1

    init(text: String, chunks: [Chunk], width: CGFloat, height: CGFloat) {
        self.chunks = chunks
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        wantsLayer = true

        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = 3
        paragraph.paragraphSpacing = 8
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
        shadow.shadowBlurRadius = 3
        textView.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: [
            .font: Self.font, .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: paragraph, .shadow: shadow,
        ]))
        textView.isEditable = false
        textView.isSelectable = false
        textView.drawsBackground = false
        textView.wantsLayer = true
        // Room above and below so the first and last lines can still reach the middle.
        textView.textContainerInset = NSSize(width: 26, height: height / 2 - 12)
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainer?.widthTracksTextView = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.frame = NSRect(x: 0, y: 0, width: width, height: height)
        textView.minSize = NSSize(width: width, height: height)
        textView.maxSize = NSSize(width: width, height: .greatestFiniteMagnitude)
        textView.onClick = { [weak self] character in self?.jump(toCharacter: character) }
        if let container = textView.textContainer { textView.layoutManager?.ensureLayout(for: container) }
        textView.sizeToFit()

        underline.wantsLayer = true
        underline.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        underline.layer?.cornerRadius = 1
        underline.isHidden = true
        textView.addSubview(underline)

        scrollView.documentView = textView
        scrollView.drawsBackground = false
        scrollView.hasVerticalScroller = false
        scrollView.frame = bounds
        scrollView.autoresizingMask = [.width, .height]
        addSubview(scrollView)

        // Soft top and bottom edges instead of a hard clip.
        fade.colors = [NSColor.clear.cgColor, NSColor.black.cgColor, NSColor.black.cgColor, NSColor.clear.cgColor]
        fade.locations = [0, 0.18, 0.82, 1]
        layer?.mask = fade
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func layout() {
        super.layout()
        fade.frame = bounds
    }

    /// Forget what is on screen, so the next `show` repaints and re-centres without animating.
    func reset() {
        shownChunk = -1
        shownWord = Int.min
        followedLine = -1
    }

    func show(_ position: ReadingPosition) {
        guard position.chunk != shownChunk || position.word != shownWord else { return }
        let firstPaint = shownChunk < 0
        shownChunk = position.chunk
        shownWord = position.word
        let chunk = chunks[position.chunk]
        if position.word >= 0, position.word < chunk.wordRanges.count {
            let word = chunk.wordRanges[position.word]
            paintRead(upTo: NSMaxRange(word))
            let geometry = lineGeometry(for: word)
            sweep(under: geometry, duration: position.wordDuration)
            follow(geometry.lineMidY, animated: !firstPaint)
        } else if let first = chunk.wordRanges.first {
            paintRead(upTo: first.location)
            underline.isHidden = true
            follow(lineGeometry(for: first).lineMidY, animated: !firstPaint)
        }
    }

    private func paintRead(upTo offset: Int) {
        guard offset != brightUpTo, let storage = textView.textStorage else { return }
        let low = min(offset, brightUpTo)
        let range = NSRange(location: low, length: max(offset, brightUpTo) - low)
        storage.addAttribute(.foregroundColor, value: offset > brightUpTo ? NSColor.labelColor : NSColor.secondaryLabelColor, range: range)
        brightUpTo = offset
    }

    private struct LineGeometry {
        let wordRect: NSRect
        let baseline: CGFloat
        let lineMidY: CGFloat
    }

    /// Where a word sits in the (flipped) text view.
    private func lineGeometry(for range: NSRange) -> LineGeometry {
        guard let layoutManager = textView.layoutManager, let container = textView.textContainer else {
            return LineGeometry(wordRect: .zero, baseline: 0, lineMidY: 0)
        }
        let glyphs = layoutManager.glyphRange(forCharacterRange: range, actualCharacterRange: nil)
        let origin = textView.textContainerOrigin
        let line = layoutManager.lineFragmentRect(forGlyphAt: glyphs.location, effectiveRange: nil)
        let rect = layoutManager.boundingRect(forGlyphRange: glyphs, in: container).offsetBy(dx: origin.x, dy: origin.y)
        let baseline = line.minY + layoutManager.location(forGlyphAt: glyphs.location).y + origin.y
        return LineGeometry(wordRect: rect, baseline: baseline, lineMidY: line.midY + origin.y)
    }

    private func sweep(under geometry: LineGeometry, duration: Double) {
        let start = NSRect(x: geometry.wordRect.minX, y: geometry.baseline + 3, width: 0, height: 2)
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0
            underline.frame = start
        }
        underline.isHidden = false
        NSAnimationContext.runAnimationGroup { context in
            context.duration = reduceMotion ? 0 : duration
            context.timingFunction = CAMediaTimingFunction(name: .linear)
            underline.animator().setFrameSize(NSSize(width: geometry.wordRect.width, height: 2))
        }
    }

    /// Scrolls only when the reading moves to another line, keeping that line in the middle.
    private func follow(_ lineMidY: CGFloat, animated: Bool) {
        guard abs(lineMidY - followedLine) > 1 else { return }
        followedLine = lineMidY
        let clip = scrollView.contentView
        let maxY = max(0, textView.frame.height - clip.bounds.height)
        let target = NSPoint(x: 0, y: min(max(0, lineMidY - clip.bounds.height / 2), maxY))
        guard animated, !reduceMotion else {
            clip.setBoundsOrigin(target)
            scrollView.reflectScrolledClipView(clip)
            return
        }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.35
            context.timingFunction = appleEase
            clip.animator().setBoundsOrigin(target)
        }
    }

    private func jump(toCharacter offset: Int) {
        guard let chunk = chunks.firstIndex(where: { ($0.wordRanges.last.map(NSMaxRange) ?? 0) >= offset }) else { return }
        onJump?(chunk)
    }
}

// MARK: - Controls

/// The Liquid Glass capsule: language · position · ◀◀ ❙❙ ▶▶ · − speed + · reader ✕
/// With the reader on, the window grows upwards and outwards into a box around the controls row,
/// which keeps its width and its place on screen — nothing moves under the pointer.
final class ControlPanel: NSPanel {
    static let rowHeight: CGFloat = 44
    static let readerHeight: CGFloat = 170
    static let expandedWidth: CGFloat = 560
    private static let expandedRadius: CGFloat = 28

    private weak var player: Player?
    private let glass = NSGlassEffectView()
    private let playPauseButton = NSButton()
    private let readerButton = NSButton()
    private let speedLabel = NSTextField(labelWithString: "1×")
    private let badgeLabel = NSTextField(labelWithString: "")
    private let positionLabel = NSTextField(labelWithString: "")
    private let spinner = NSProgressIndicator()
    private let reader: ReaderView
    private let separator = NSBox()
    private var rowWidth: CGFloat = 0
    private var readerTimer: Timer?
    private var isExpanded = false

    init(player: Player) {
        self.player = player
        reader = ReaderView(text: player.text, chunks: player.chunks, width: Self.expandedWidth, height: Self.readerHeight)
        super.init(contentRect: NSRect(x: 0, y: 0, width: 320, height: Self.rowHeight),
                   styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        isMovableByWindowBackground = true
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true

        glass.style = .clear  // .regular reads as a dark smoky pill; .clear shows the refraction
        glass.cornerRadius = Self.rowHeight / 2
        contentView = glass

        badgeLabel.stringValue = player.voice.badge
        badgeLabel.font = .systemFont(ofSize: 11, weight: .bold)
        badgeLabel.textColor = .secondaryLabelColor
        positionLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        positionLabel.textColor = .secondaryLabelColor
        positionLabel.alignment = .left
        positionLabel.widthAnchor.constraint(equalToConstant: 34).isActive = true
        spinner.style = .spinning
        spinner.controlSize = .mini
        spinner.isDisplayedWhenStopped = false
        speedLabel.font = .monospacedDigitSystemFont(ofSize: 13, weight: .semibold)
        speedLabel.alignment = .center
        speedLabel.widthAnchor.constraint(equalToConstant: 38).isActive = true
        configure(playPauseButton, symbol: "pause.fill", tip: "Pause / resume", size: 17, action: #selector(togglePausePressed))
        configure(readerButton, symbol: "quote.bubble", tip: "Follow the text", action: #selector(readerPressed))

        let row = NSStackView(views: [
            badgeLabel,
            positionLabel,
            spinner,
            makeButton("backward.fill", tip: "Previous sentence", action: #selector(previousPressed)),
            playPauseButton,
            makeButton("forward.fill", tip: "Next sentence", action: #selector(nextPressed)),
            makeDivider(),
            makeButton("minus", tip: "Slower", action: #selector(slowerPressed)),
            speedLabel,
            makeButton("plus", tip: "Faster", action: #selector(fasterPressed)),
            makeDivider(),
            readerButton,
            makeButton("xmark", tip: "Stop", action: #selector(stopPressed)),
        ])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 2
        row.setCustomSpacing(6, after: badgeLabel)
        row.edgeInsets = NSEdgeInsets(top: 0, left: 18, bottom: 0, right: 10)
        rowWidth = row.fittingSize.width

        reader.onJump = { [weak self] chunk in self?.player?.play(chunk: chunk) }
        reader.alphaValue = 0
        separator.boxType = .separator
        separator.alphaValue = 0

        // The row is pinned to the bottom centre at a fixed width, and the reader sits above it at
        // the expanded width, so resizing the window never moves or reflows either of them.
        let container = NSView()
        for view in [row, reader, separator] as [NSView] {
            view.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(view)
        }
        NSLayoutConstraint.activate([
            row.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            row.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            row.widthAnchor.constraint(equalToConstant: rowWidth),
            row.heightAnchor.constraint(equalToConstant: Self.rowHeight),
            separator.bottomAnchor.constraint(equalTo: row.topAnchor),
            separator.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            separator.widthAnchor.constraint(equalToConstant: Self.expandedWidth - 32),
            reader.bottomAnchor.constraint(equalTo: separator.topAnchor),
            reader.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            reader.widthAnchor.constraint(equalToConstant: Self.expandedWidth),
            reader.heightAnchor.constraint(equalToConstant: Self.readerHeight),
        ])
        glass.contentView = container

        setContentSize(NSSize(width: rowWidth, height: Self.rowHeight))
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main!
        setFrameOrigin(NSPoint(x: screen.visibleFrame.midX - rowWidth / 2, y: screen.visibleFrame.minY + 90))
    }

    /// Opens the reader straight away if it was left open last time.
    func restoreReader() {
        if settings.bool(forKey: "karaoke") { setExpanded(true, animated: false) }
    }

    /// `status` is the short text in the capsule; `detail` (the current sentence or an error) is the tooltip.
    func update(status: String, detail: String, busy: Bool) {
        guard let player else { return }
        playPauseButton.image = symbolImage(player.isPaused ? "play.fill" : "pause.fill", size: 17)
        speedLabel.stringValue = formatSpeed(player.speed)
        positionLabel.stringValue = busy ? "" : status
        busy ? spinner.startAnimation(nil) : spinner.stopAnimation(nil)
        spinner.isHidden = !busy
        positionLabel.isHidden = busy
        glass.toolTip = detail
        glass.tintColor = status == "error" ? NSColor.systemRed.withAlphaComponent(0.25) : nil
    }

    private func setExpanded(_ expanded: Bool, animated: Bool) {
        isExpanded = expanded
        settings.set(expanded, forKey: "karaoke")
        readerButton.contentTintColor = expanded ? .controlAccentColor : .labelColor
        readerTimer?.invalidate()
        readerTimer = nil
        if expanded {
            reader.reset()
            tickReader()
            let timer = Timer(timeInterval: 1.0 / 30, repeats: true) { [weak self] _ in self?.tickReader() }
            RunLoop.main.add(timer, forMode: .common)
            readerTimer = timer
        }

        // Grow from the bottom edge and the horizontal centre, which is where the row is pinned.
        let width = expanded ? Self.expandedWidth : rowWidth
        let height = expanded ? Self.rowHeight + Self.readerHeight : Self.rowHeight
        let target = NSRect(x: frame.midX - width / 2, y: frame.minY, width: width, height: height)
        if !expanded { glass.cornerRadius = Self.rowHeight / 2 }
        let finish = { [weak self] in
            guard let self, self.isExpanded == expanded else { return }
            self.glass.cornerRadius = expanded ? Self.expandedRadius : Self.rowHeight / 2
        }
        guard animated, !reduceMotion else {
            setFrame(target, display: true)
            reader.alphaValue = expanded ? 1 : 0
            separator.alphaValue = expanded ? 1 : 0
            return finish()
        }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.42
            context.timingFunction = appleEase
            animator().setFrame(target, display: true)
            reader.animator().alphaValue = expanded ? 1 : 0
            separator.animator().alphaValue = expanded ? 1 : 0
        }, completionHandler: finish)
    }

    private func tickReader() {
        guard let player else { return }
        reader.show(player.readingPosition())
    }

    private func configure(_ button: NSButton, symbol: String, tip: String, size: CGFloat = 13, action: Selector) {
        button.image = symbolImage(symbol, size: size)
        button.imagePosition = .imageOnly
        button.isBordered = false
        button.bezelStyle = .regularSquare
        button.contentTintColor = .labelColor
        button.toolTip = tip
        button.target = self
        button.action = action
        button.widthAnchor.constraint(equalToConstant: 30).isActive = true
        button.heightAnchor.constraint(equalToConstant: 30).isActive = true
    }

    private func makeButton(_ symbol: String, tip: String, action: Selector) -> NSButton {
        let button = NSButton()
        configure(button, symbol: symbol, tip: tip, action: action)
        return button
    }

    private func makeDivider() -> NSView {
        let divider = NSBox()
        divider.boxType = .separator
        divider.heightAnchor.constraint(equalToConstant: 18).isActive = true
        let holder = NSStackView(views: [divider])
        holder.edgeInsets = NSEdgeInsets(top: 0, left: 6, bottom: 0, right: 6)
        return holder
    }

    @objc private func togglePausePressed() { player?.togglePause() }
    @objc private func previousPressed() { player?.skip(by: -1) }
    @objc private func nextPressed() { player?.skip(by: 1) }
    @objc private func slowerPressed() { player?.changeSpeed(by: -1) }
    @objc private func fasterPressed() { player?.changeSpeed(by: 1) }
    @objc private func readerPressed() { setExpanded(!isExpanded, animated: true) }
    @objc private func stopPressed() { NSApp.terminate(nil) }
}

// MARK: - Main

// A SESSION OF ITS OWN, WHOEVER STARTED IT. Stopping playback means signalling
// the whole process group -- the player plus the audio work it spawned -- and
// killpg(pid) only reaches it if this process is the group LEADER. That used to
// be arranged by the caller: the OpenClip script passes start_new_session=True.
// The daemon spawns players too, and Process has no equivalent, so a player
// started by the hotkey was not a leader and the script's stop silently found
// nothing to signal. Owning it here makes the two coordinators interchangeable
// instead of making each of them remember.
//
// EPERM means somebody already did it, which is success. Mirrors what
// server.py does for the same reason.
_ = setsid()

let arguments = CommandLine.arguments
guard arguments.count >= 2, let rawText = try? String(contentsOfFile: arguments[1], encoding: .utf8) else {
    FileHandle.standardError.write(Data("usage: calliope-player <text-file>\n".utf8))
    exit(2)
}
// Selections can arrive decomposed ("e" + U+0301); espeak drops a lone combining mark,
// which turns "é" into "e" and changes the word. Precompose before anything else sees it.
let text = rawText.precomposedStringWithCanonicalMapping
try? FileManager.default.removeItem(atPath: arguments[1])

let language = detectLanguage(text)
let voice = voices[language] ?? voices[.english]!
let chunks = splitIntoChunks(text, language: language)
guard !chunks.isEmpty else { exit(0) }

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let player = Player(text: text, chunks: chunks, voice: voice)
let panel = ControlPanel(player: player)
player.panel = panel
panel.restoreReader()
panel.orderFrontRegardless()
player.start()
app.run()
