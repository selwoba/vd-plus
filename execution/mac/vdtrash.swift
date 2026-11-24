// vdtrash: move files to the user's Trash (recoverable) via FileManager.trashItem.
// Used by the engine instead of scripting Finder, which needs Automation permission
// and fails silently without it. Prints one line per path: "OK <path>" or "FAIL <path>".
import Foundation

for p in CommandLine.arguments.dropFirst() {
    do {
        try FileManager.default.trashItem(at: URL(fileURLWithPath: p), resultingItemURL: nil)
        print("OK \(p)")
    } catch {
        print("FAIL \(p)")
    }
}
