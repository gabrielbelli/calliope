// The Calliope mark, drawn rather than shipped as an asset.
//
// ONE DRAWING, AND IT IS ALREADY ON SCREEN. The page carries this same glyph
// inline in its <head> and in its masthead, with a comment saying "it will be
// the app icon" -- and then the app shipped with no icon at all and an SF
// Symbol waveform in the menu bar, which is somebody else's drawing.
//
// Taken from that favicon's data URI, in its own 32-unit coordinate space:
//   a rounded rectangle          #1B1512   the dark glass
//   a swell, stroked             #EBE5DA   the pressure that makes the voice
//   a disc                       #FF4438   the lamp, the one saturated thing
//
// The calliope is a steam organ: a machine that makes a voice out of pressure.
// A mark that has to be explained is a mark that was invented; this one was
// already the favicon, so the app icon is the same drawing at another size
// rather than a second identity to keep in step.
//
// Code rather than a PNG because there is no SVG rasteriser on this machine,
// because the shape is three primitives, and because a drawing that scales is
// one that cannot be shipped at the wrong size.

import CoreGraphics

enum Mark {
    /// The glyph's own coordinate space, from the favicon's viewBox.
    private static let grid: CGFloat = 32

    /// The ink's own bounds in the 32-unit space: the swell from its round
    /// caps outward, the lamp from its edge. Not the plate.
    ///
    /// WHY THIS EXISTS: with the plate drawn, the glyph fills its square. With
    /// the plate dropped -- which is what a template image is -- the swell and
    /// the lamp occupy the middle 60 per cent and nothing else, so a menu bar
    /// icon rendered from the same numbers came out small and adrift with a
    /// ring of empty space no other status item has. Monochrome therefore fits
    /// the ink to the canvas rather than the grid.
    private static let ink = (x: CGFloat(4.9), y: CGFloat(8.7),
                              width: CGFloat(22.2), height: CGFloat(16.9))

    /// The mark, filling the canvas: rounded plate, swell, lamp.
    ///
    /// `monochrome` drops the plate and the colour and leaves black shapes
    /// with alpha, which is what a menu bar template image is: macOS tints it
    /// for light, for dark, and for the moment it is clicked. A coloured
    /// status item ignores all three. It also refits, per `ink` above.
    static func draw(in context: CGContext, size: CGFloat, monochrome: Bool) {
        let unit = monochrome
            ? min(size / ink.width, size / ink.height) * 0.94   // a hair of margin
            : size / grid
        let originX = monochrome ? (size - ink.width * unit) / 2 - ink.x * unit : 0
        let originY = monochrome
            ? (size - ink.height * unit) / 2 - (grid - ink.y - ink.height) * unit
            : 0
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            // The SVG's origin is top-left and CoreGraphics' is bottom-left.
            CGPoint(x: originX + x * unit, y: originY + (grid - y) * unit)
        }

        if !monochrome {
            let plate = CGPath(roundedRect: CGRect(x: 0, y: 0, width: size, height: size),
                               cornerWidth: 7 * unit, cornerHeight: 7 * unit,
                               transform: nil)
            context.addPath(plate)
            context.setFillColor(CGColor(red: 0x1B / 255, green: 0x15 / 255,
                                         blue: 0x12 / 255, alpha: 1))
            context.fillPath()
        }

        let swell = CGMutablePath()
        swell.move(to: point(6, 19.5))
        swell.addCurve(to: point(16, 24.5),
                       control1: point(10.5, 19.5), control2: point(11.8, 24.5))
        swell.addCurve(to: point(26, 19.5),
                       control1: point(20.2, 24.5), control2: point(21.5, 19.5))
        context.addPath(swell)
        context.setStrokeColor(monochrome
            ? CGColor(gray: 0, alpha: 1)
            : CGColor(red: 0xEB / 255, green: 0xE5 / 255, blue: 0xDA / 255, alpha: 1))
        context.setLineWidth(2.2 * unit)
        context.setLineCap(.round)
        context.strokePath()

        let lampCentre = point(16, 12.5)
        let lamp = CGRect(x: lampCentre.x - 3.8 * unit, y: lampCentre.y - 3.8 * unit,
                          width: 7.6 * unit, height: 7.6 * unit)
        context.addEllipse(in: lamp)
        context.setFillColor(monochrome
            ? CGColor(gray: 0, alpha: 1)
            : CGColor(red: 0xFF / 255, green: 0x44 / 255, blue: 0x38 / 255, alpha: 1))
        context.fillPath()
    }
}
