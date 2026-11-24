// Renders the VD Plus app icon (1024x1024 PNG) to argv[1].
// White macOS tile, dark-orange rounded "play button" whose triangle points DOWN
// (download), and a small VD+ wordmark.
import Cocoa

let S: CGFloat = 1024
let img = NSImage(size: NSSize(width: S, height: S))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

// Tile: macOS grid (824pt body, 100pt margin), soft white gradient + subtle drop shadow.
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 28, color: NSColor.black.withAlphaComponent(0.28).cgColor)
NSColor.white.setFill()
tilePath.fill()
ctx.restoreGState()
NSGradient(colors: [NSColor(white: 1.0, alpha: 1), NSColor(red: 0.965, green: 0.955, blue: 0.945, alpha: 1)])!
    .draw(in: tilePath, angle: -90)

// Orange play-button body (YouTube-like proportions), centred slightly above middle.
let body = NSRect(x: 222, y: 380, width: 580, height: 410)
let bodyPath = NSBezierPath(roundedRect: body, xRadius: 118, yRadius: 118)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 22,
              color: NSColor(red: 0.6, green: 0.18, blue: 0.02, alpha: 0.35).cgColor)
NSColor(red: 0.761, green: 0.255, blue: 0.047, alpha: 1).setFill()
bodyPath.fill()
ctx.restoreGState()
NSGradient(colors: [NSColor(red: 0.949, green: 0.420, blue: 0.114, alpha: 1),   // #F26B1D
                    NSColor(red: 0.761, green: 0.255, blue: 0.047, alpha: 1)])!  // #C2410C
    .draw(in: bodyPath, angle: -90)

// White triangle pointing down, with rounded corners.
let cx = body.midX, top = body.maxY - 112, bottom = body.minY + 96, half: CGFloat = 132
let tri = NSBezierPath()
tri.move(to: NSPoint(x: cx - half, y: top))
tri.line(to: NSPoint(x: cx + half, y: top))
tri.line(to: NSPoint(x: cx, y: bottom))
tri.close()
tri.lineJoinStyle = .round
tri.lineWidth = 36
NSColor.white.setFill()
NSColor.white.setStroke()
tri.fill()
tri.stroke()

// VD+ wordmark.
let font = NSFont.systemFont(ofSize: 150, weight: .heavy)
let para = NSMutableParagraphStyle(); para.alignment = .center
let attrs: [NSAttributedString.Key: Any] = [
    .font: font,
    .foregroundColor: NSColor(red: 0.761, green: 0.255, blue: 0.047, alpha: 1),
    .kern: -4,
    .paragraphStyle: para,
]
NSAttributedString(string: "VD+", attributes: attrs).draw(in: NSRect(x: 100, y: 160, width: 824, height: 190))

img.unlockFocus()
let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
