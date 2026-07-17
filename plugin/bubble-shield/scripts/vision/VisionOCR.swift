// VisionOCR.swift — standalone Apple Vision OCR for scanned PDFs.
// Reads a PDF, renders each page to a CGImage, runs VNRecognizeTextRequest
// (accurate, fr-FR, usesLanguageCorrection=false), prints recognized text with
// pages separated by \n\f\n. One process per document (invoked as subprocess).
//
// Build: swiftc -O VisionOCR.swift -o visionocr
// Usage: ./visionocr <pdf> [scale]   (scale default 2.0)
//
// Reading order: Vision returns normalized bboxes (origin bottom-left). We sort
// top-to-bottom then left-to-right, bucketing into line-bands by a y tolerance,
// matching the Python bench's ordering so CER is comparable.

import Foundation
import Vision
import CoreGraphics
import ImageIO

func die(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(2)
}

let args = CommandLine.arguments
guard args.count >= 2 else { die("usage: visionocr <pdf> [scale]") }
let pdfPath = args[1]
let scale = args.count >= 3 ? (Double(args[2]) ?? 2.0) : 2.0

let url = URL(fileURLWithPath: pdfPath)
guard let pdfDoc = CGPDFDocument(url as CFURL) else { die("cannot open PDF: \(pdfPath)") }
let nPages = pdfDoc.numberOfPages

func renderPage(_ pageNo: Int) -> CGImage? {
    guard let page = pdfDoc.page(at: pageNo) else { return nil }
    let box = page.getBoxRect(.mediaBox)
    let w = Int(box.width * CGFloat(scale))
    let h = Int(box.height * CGFloat(scale))
    guard w > 0, h > 0 else { return nil }
    let cs = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: cs,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.setFillColor(red: 1, green: 1, blue: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: CGFloat(scale), y: CGFloat(scale))
    ctx.drawPDFPage(page)
    return ctx.makeImage()
}

func ocrImage(_ cg: CGImage) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["fr-FR"]
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try handler.perform([request])
    guard let results = request.results else { return "" }

    // (topEdgeY, leftX, string)
    var obs: [(Double, Double, String)] = []
    for o in results {
        guard let top = o.topCandidates(1).first else { continue }
        let bb = o.boundingBox
        obs.append((Double(bb.origin.y + bb.size.height), Double(bb.origin.x), top.string))
    }
    obs.sort { $0.0 != $1.0 ? $0.0 > $1.0 : $0.1 < $1.1 }
    let yTol = 0.012
    var lines: [[(Double, Double, String)]] = []
    for item in obs {
        if var last = lines.last, let f = last.first, abs(f.0 - item.0) <= yTol {
            last.append(item); lines[lines.count - 1] = last
        } else {
            lines.append([item])
        }
    }
    var pageLines: [String] = []
    for var line in lines {
        line.sort { $0.1 < $1.1 }
        pageLines.append(line.map { $0.2 }.joined(separator: " "))
    }
    return pageLines.joined(separator: "\n")
}

var pageTexts: [String] = []
for p in 1...nPages {
    guard let cg = renderPage(p) else { die("render failed page \(p)") }
    do {
        pageTexts.append(try ocrImage(cg))
    } catch {
        die("OCR failed page \(p): \(error)")
    }
}
print(pageTexts.joined(separator: "\n\u{0C}\n"))
