// Renders Calliope.icns from shared/mark.swift.
//
// Build-time rather than runtime, because an .icns is what Finder, the Dock
// and Gatekeeper's own dialogs read, and none of them will call code.
//
//   swiftc -O shared/mark.swift bundle/make-icon.swift -o /tmp/make-icon
//   /tmp/make-icon <output.icns>

import AppKit

@main
struct MakeIcon {
  static func main() {

    let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Calliope.icns"
    let iconset = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("calliope-\(getpid()).iconset")
    try? FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

    // The names iconutil expects. Every size is drawn, not scaled from one bitmap:
    // the swell is a 2.2-unit stroke, and a 16pt icon downsampled from 1024 loses it.
    let wanted: [(String, CGFloat)] = [
        ("icon_16x16", 16), ("icon_16x16@2x", 32),
        ("icon_32x32", 32), ("icon_32x32@2x", 64),
        ("icon_128x128", 128), ("icon_128x128@2x", 256),
        ("icon_256x256", 256), ("icon_256x256@2x", 512),
        ("icon_512x512", 512), ("icon_512x512@2x", 1024),
    ]

    for (name, size) in wanted {
        let pixels = Int(size)
        guard let context = CGContext(data: nil, width: pixels, height: pixels,
                                      bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { fatalError("could not make a \(pixels)px context") }
        context.setAllowsAntialiasing(true)
        Mark.draw(in: context, size: size, monochrome: false)

        guard let image = context.makeImage(),
              let data = NSBitmapImageRep(cgImage: image)
                  .representation(using: .png, properties: [:])
        else { fatalError("could not encode \(name)") }
        try! data.write(to: iconset.appendingPathComponent("\(name).png"))
    }

    let iconutil = Process()
    iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    iconutil.arguments = ["-c", "icns", iconset.path, "-o", out]
    try! iconutil.run()
    iconutil.waitUntilExit()
    try? FileManager.default.removeItem(at: iconset)
    exit(iconutil.terminationStatus)
  }
}
